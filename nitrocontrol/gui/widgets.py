"""Custom-drawn widgets: gauges, history graphs, fan-curve editor, keyboard preview."""

import colorsys
import math
import time

import cairo
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GObject, Gtk, Pango, PangoCairo  # noqa: E402

ACCENT = (0.93, 0.11, 0.20)       # Nitro red
CYAN = (0.0, 0.85, 0.95)
AMBER = (1.0, 0.70, 0.10)
GRID = (1, 1, 1, 0.07)
TEXT = (0.92, 0.94, 0.97)
MUTED = (0.62, 0.66, 0.72)


def heat_color(frac):
    """0 -> cyan, 0.6 -> amber, 1 -> red."""
    frac = max(0.0, min(1.0, frac))
    if frac < 0.6:
        t = frac / 0.6
        a, b = CYAN, AMBER
    else:
        t = (frac - 0.6) / 0.4
        a, b = AMBER, ACCENT
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def draw_text(cr, text, x, y, size=10, bold=False, color=TEXT, align="center", alpha=1.0, family="Sans"):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string("%s %s %d" % (family, "Bold" if bold else "", size))
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    w, h = layout.get_pixel_size()
    if align == "center":
        x -= w / 2
    elif align == "right":
        x -= w
    cr.set_source_rgba(color[0], color[1], color[2], alpha)
    cr.move_to(x, y - h / 2)
    PangoCairo.show_layout(cr, layout)
    return w, h


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class _Animated:
    """Runs a frame-clock tick only while something is actually moving."""

    def _animate(self):
        if getattr(self, "_tick_id", None) is None:
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _w, _clock):
        still_moving = self.step()
        self.queue_draw()
        if not still_moving:
            self._tick_id = None
            return False
        return True


class Gauge(Gtk.DrawingArea, _Animated):
    START, SWEEP = math.radians(135), math.radians(270)

    def __init__(self, title, unit, lo, hi, fmt="{:.0f}", size=150):
        super().__init__()
        self.title, self.unit, self.lo, self.hi, self.fmt = title, unit, lo, hi, fmt
        self.value = None
        self.shown = lo
        self.subtitle = ""
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def set_value(self, value, subtitle=None):
        self.value = value
        if subtitle is not None:
            self.subtitle = subtitle
        self._animate()

    def step(self):
        target = self.value if self.value is not None else self.lo
        diff = target - self.shown
        if abs(diff) < (self.hi - self.lo) * 0.002:
            self.shown = target
            return False
        self.shown += diff * 0.18
        return True

    def _draw(self, _area, cr, w, h):
        size = min(w, h)
        cx, cy = w / 2, h / 2 + size * 0.04
        r = size / 2 - 12
        frac = (self.shown - self.lo) / float(self.hi - self.lo)
        frac = max(0.0, min(1.0, frac))
        col = heat_color(frac)

        cr.set_line_cap(1)  # round
        cr.set_line_width(10)
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.arc(cx, cy, r, self.START, self.START + self.SWEEP)
        cr.stroke()

        # tick marks
        cr.set_line_width(1.5)
        for i in range(11):
            a = self.START + self.SWEEP * i / 10
            inner = r - 14 if i % 5 == 0 else r - 10
            cr.set_source_rgba(1, 1, 1, 0.18 if i % 5 == 0 else 0.08)
            cr.move_to(cx + math.cos(a) * inner, cy + math.sin(a) * inner)
            cr.line_to(cx + math.cos(a) * (r - 7), cy + math.sin(a) * (r - 7))
            cr.stroke()

        if self.value is not None and frac > 0.001:
            end = self.START + self.SWEEP * frac
            cr.set_line_width(18)
            cr.set_source_rgba(col[0], col[1], col[2], 0.16)
            cr.arc(cx, cy, r, self.START, end)
            cr.stroke()
            cr.set_line_width(10)
            cr.set_source_rgb(*col)
            cr.arc(cx, cy, r, self.START, end)
            cr.stroke()
            cr.set_source_rgb(1, 1, 1)
            cr.arc(cx + math.cos(end) * r, cy + math.sin(end) * r, 3, 0, 2 * math.pi)
            cr.fill()

        text = self.fmt.format(self.shown) if self.value is not None else "--"
        draw_text(cr, text, cx, cy - 4, size=max(14, int(size / 7.5)), bold=True)
        draw_text(cr, self.unit, cx, cy + size * 0.14, size=max(8, int(size / 17)), color=MUTED)
        draw_text(cr, self.title.upper(), cx, cy + r * 0.78, size=max(8, int(size / 18)), bold=True, color=col if self.value is not None else MUTED)
        if self.subtitle:
            draw_text(cr, self.subtitle, cx, cy + r * 0.78 + size * 0.1, size=max(7, int(size / 21)), color=MUTED)


class HistoryGraph(Gtk.DrawingArea):
    def __init__(self, series, lo, hi, unit, length=150, height=170, auto_hi=False):
        super().__init__()
        self.series = series            # [(key, label, (r,g,b))]
        self.lo, self.hi, self.unit, self.auto_hi = lo, hi, unit, auto_hi
        self.data = {k: deque(maxlen=length) for k, _, _ in series}
        self.length = length
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def push(self, values):
        for key in self.data:
            self.data[key].append(values.get(key))
        self.queue_draw()

    def _draw(self, _area, cr, w, h):
        pad_l, pad_r, pad_t, pad_b = 38, 10, 24, 14
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        hi = self.hi
        if self.auto_hi:
            peak = max([v for d in self.data.values() for v in d if v is not None] or [0])
            hi = max(self.hi, math.ceil(peak / 1000.0) * 1000)
        span = float(hi - self.lo) or 1.0

        cr.set_line_width(1)
        for i in range(5):
            y = pad_t + gh * i / 4
            cr.set_source_rgba(*GRID)
            cr.move_to(pad_l, y)
            cr.line_to(pad_l + gw, y)
            cr.stroke()
            val = hi - span * i / 4
            label = ("%.0fk" % (val / 1000)) if hi >= 2000 else "%.0f" % val
            draw_text(cr, label, pad_l - 6, y, size=7, color=MUTED, align="right")

        lx = pad_l
        for key, label, col in self.series:
            d = self.data[key]
            last = next((v for v in reversed(d) if v is not None), None)
            cr.set_source_rgb(*col)
            cr.arc(lx + 4, 11, 3.5, 0, 2 * math.pi)
            cr.fill()
            txt = "%s %s" % (label, ("%.0f%s" % (last, self.unit)) if last is not None else "--")
            tw, _ = draw_text(cr, txt, lx + 12, 11, size=8, color=TEXT, align="left")
            lx += tw + 28

        step = gw / float(max(1, self.length - 1))
        for key, _label, col in self.series:
            d = list(self.data[key])
            if len(d) < 2:
                continue
            x0 = pad_l + gw - step * (len(d) - 1)
            pts = [(x0 + i * step, pad_t + gh - (max(self.lo, min(hi, v)) - self.lo) / span * gh)
                   for i, v in enumerate(d) if v is not None]
            if len(pts) < 2:
                continue
            cr.move_to(*pts[0])
            for pt in pts[1:]:
                cr.line_to(*pt)
            cr.set_source_rgb(*col)
            cr.set_line_width(2)
            cr.stroke_preserve()
            cr.line_to(pts[-1][0], pad_t + gh)
            cr.line_to(pts[0][0], pad_t + gh)
            cr.close_path()

            grad = cairo.LinearGradient(0, pad_t, 0, pad_t + gh)
            grad.add_color_stop_rgba(0, col[0], col[1], col[2], 0.28)
            grad.add_color_stop_rgba(1, col[0], col[1], col[2], 0.0)
            cr.set_source(grad)
            cr.fill()


class CurveEditor(Gtk.DrawingArea):
    """Drag points to shape a fan curve. Click empty space to add, right-click to remove."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}
    T_MIN, T_MAX = 20, 100

    def __init__(self):
        super().__init__()
        self.points = [[40, 20], [60, 45], [75, 70], [85, 100]]
        self.color = ACCENT
        self.current_temp = None
        self.current_pct = None
        self.floor = []
        self._drag = None
        self._hover = None
        self.set_content_height(300)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", self._right_click)
        self.add_controller(right)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)

    # geometry
    def _box(self):
        w, h = self.get_width(), self.get_height()
        return 44, 14, w - 44 - 16, h - 14 - 32

    def _to_px(self, t, pct):
        x, y, w, h = self._box()
        return x + (t - self.T_MIN) / float(self.T_MAX - self.T_MIN) * w, y + h - pct / 100.0 * h

    def _from_px(self, px, py):
        x, y, w, h = self._box()
        t = self.T_MIN + (px - x) / float(w) * (self.T_MAX - self.T_MIN)
        pct = (y + h - py) / float(h) * 100
        return t, pct

    def _nearest(self, px, py, radius=14):
        best, dist = None, radius
        for i, (t, pct) in enumerate(self.points):
            x, y = self._to_px(t, pct)
            d = math.hypot(x - px, y - py)
            if d <= dist:
                best, dist = i, d
        return best

    def set_points(self, points):
        self.points = [list(p) for p in points]
        self.queue_draw()

    def _set_hover(self, idx):
        if idx != self._hover:
            self._hover = idx
            self.queue_draw()

    def _motion(self, _c, x, y):
        self._set_hover(self._nearest(x, y))

    def _drag_begin(self, gesture, x, y):
        idx = self._nearest(x, y)
        if idx is None and len(self.points) < 8:
            t, pct = self._from_px(x, y)
            t = int(round(max(self.T_MIN, min(self.T_MAX, t))))
            if all(abs(p[0] - t) >= 2 for p in self.points):
                self.points.append([t, int(round(max(0, min(100, pct))))])
                self.points.sort(key=lambda p: p[0])
                idx = next(i for i, p in enumerate(self.points) if p[0] == t)
                self._constrain(idx, t, pct)
                self.emit("changed")
        self._drag = (idx, x, y) if idx is not None else None
        self.queue_draw()

    def _constrain(self, idx, t, pct):
        lo_t = self.points[idx - 1][0] + 1 if idx > 0 else self.T_MIN
        hi_t = self.points[idx + 1][0] - 1 if idx < len(self.points) - 1 else self.T_MAX
        lo_p = self.points[idx - 1][1] if idx > 0 else 0
        hi_p = self.points[idx + 1][1] if idx < len(self.points) - 1 else 100
        self.points[idx] = [int(round(max(lo_t, min(hi_t, t)))), int(round(max(lo_p, min(hi_p, pct))))]

    def _drag_update(self, _g, dx, dy):
        if not self._drag:
            return
        idx, sx, sy = self._drag
        t, pct = self._from_px(sx + dx, sy + dy)
        self._constrain(idx, t, pct)
        self.queue_draw()

    def _drag_end(self, *_):
        if self._drag:
            self._drag = None
            self.emit("changed")
            self.queue_draw()

    def _right_click(self, _g, _n, x, y):
        idx = self._nearest(x, y)
        if idx is not None and len(self.points) > 2:
            del self.points[idx]
            self.emit("changed")
            self.queue_draw()

    def _draw(self, _area, cr, _w, _h):
        x, y, w, h = self._box()
        cr.set_line_width(1)
        for pct in range(0, 101, 20):
            _, py = self._to_px(self.T_MIN, pct)
            cr.set_source_rgba(*GRID)
            cr.move_to(x, py)
            cr.line_to(x + w, py)
            cr.stroke()
            draw_text(cr, "%d%%" % pct, x - 6, py, size=7, color=MUTED, align="right")
        for t in range(self.T_MIN, self.T_MAX + 1, 10):
            px, _ = self._to_px(t, 0)
            cr.set_source_rgba(*GRID)
            cr.move_to(px, y)
            cr.line_to(px, y + h)
            cr.stroke()
            draw_text(cr, "%d°" % t, px, y + h + 12, size=7, color=MUTED)

        # safety floor area
        if self.floor:
            cr.set_source_rgba(1, 0.69, 0.13, 0.06)
            cr.move_to(*self._to_px(self.floor[0][0], 0))
            prev_pct = 0
            for t, pct in self.floor:
                cr.line_to(*self._to_px(t, prev_pct))
                cr.line_to(*self._to_px(t, pct))
                prev_pct = pct
            cr.line_to(*self._to_px(self.T_MAX, prev_pct))
            cr.line_to(*self._to_px(self.T_MAX, 0))
            cr.close_path()
            cr.fill()
            draw_text(cr, "safety floor", x + w - 4, y + h - 10, size=7, color=MUTED, align="right", alpha=0.7)

        pts = [self._to_px(t, pct) for t, pct in self.points]
        first, last = pts[0], pts[-1]
        col = self.color

        cr.move_to(x, first[1])
        for pt in pts:
            cr.line_to(*pt)
        cr.line_to(x + w, last[1])
        cr.line_to(x + w, y + h)
        cr.line_to(x, y + h)
        cr.close_path()
        grad = cairo.LinearGradient(0, y, 0, y + h)
        grad.add_color_stop_rgba(0, col[0], col[1], col[2], 0.30)
        grad.add_color_stop_rgba(1, col[0], col[1], col[2], 0.02)
        cr.set_source(grad)
        cr.fill()

        cr.move_to(x, first[1])
        for pt in pts:
            cr.line_to(*pt)
        cr.line_to(x + w, last[1])
        cr.set_source_rgb(*col)
        cr.set_line_width(2.5)
        cr.stroke()

        for i, (px, py) in enumerate(pts):
            active = self._drag and self._drag[0] == i or self._hover == i
            cr.set_source_rgba(col[0], col[1], col[2], 0.25)
            cr.arc(px, py, 11 if active else 8, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.arc(px, py, 5, 0, 2 * math.pi)
            cr.fill()
            if active:
                t, pct = self.points[i]
                draw_text(cr, "%d°C → %d%%" % (t, pct), px, py - 20, size=8, bold=True)

        if self.current_temp is not None:
            px, _ = self._to_px(max(self.T_MIN, min(self.T_MAX, self.current_temp)), 0)
            cr.set_source_rgba(*CYAN, 0.7)
            cr.set_line_width(1.5)
            cr.set_dash([4, 4])
            cr.move_to(px, y)
            cr.line_to(px, y + h)
            cr.stroke()
            cr.set_dash([])
            if self.current_pct is not None:
                _, py = self._to_px(0, self.current_pct)
                cr.set_source_rgb(*CYAN)
                cr.arc(px, py, 4.5, 0, 2 * math.pi)
                cr.fill()
            draw_text(cr, "now %.0f°C" % self.current_temp, px + 4, y + 8, size=7, color=CYAN, align="left")


class KeyboardPreview(Gtk.DrawingArea):
    """Stylised 4-zone keyboard that previews colours and (roughly) the effects."""

    ROWS = [15, 15, 14, 13, 12, 9]

    def __init__(self):
        super().__init__()
        self.zones = ["#ff1a3c"] * 4
        self.brightness = 100
        self.effect = None      # None = static zones, else effect index
        self.effect_color = "#ff1a3c"
        self.speed = 4
        self.direction = 2
        self._tick_id = None
        self._t0 = time.monotonic()
        self.set_content_height(190)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.connect("map", lambda *_: self._update_anim())
        self.connect("unmap", lambda *_: self._stop())

    def set_static(self, zones, brightness):
        self.zones, self.brightness, self.effect = list(zones), brightness, None
        self._update_anim()
        self.queue_draw()

    def set_effect(self, effect, color, brightness, speed, direction):
        self.effect, self.effect_color = effect, color
        self.brightness, self.speed, self.direction = brightness, speed, direction
        self._update_anim()
        self.queue_draw()

    def _update_anim(self):
        animated = self.effect not in (None, 0) and self.get_mapped()
        if animated and self._tick_id is None:
            self._tick_id = self.add_tick_callback(lambda *_: (self.queue_draw(), True)[1])
        elif not animated:
            self._stop()

    def _stop(self):
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def _key_color(self, fx, t):
        """fx: horizontal position 0..1. Returns (r, g, b, intensity)."""
        b = self.brightness / 100.0
        if self.effect is None:
            zone = min(3, int(fx * 4))
            return hex_to_rgb(self.zones[zone]) + (b,)
        speed = 0.25 + self.speed * 0.2
        base = hex_to_rgb(self.effect_color)
        pos = fx if self.direction == 2 else 1 - fx
        e = self.effect
        if e == 0:
            return base + (b,)
        if e == 1:  # breathing
            return base + (b * (0.15 + 0.85 * (0.5 + 0.5 * math.sin(t * speed * 2))),)
        if e == 2:  # neon: whole board cycles hue
            return colorsys.hsv_to_rgb((t * speed * 0.15) % 1, 1, 1) + (b,)
        if e == 3:  # wave: rainbow travelling across
            return colorsys.hsv_to_rgb((pos - t * speed * 0.25) % 1, 1, 1) + (b,)
        if e == 4:  # shifting: a band of colour sliding across
            c = (t * speed * 0.3) % 1.4 - 0.2
            return base + (b * max(0.08, 1 - abs(pos - c) * 4),)
        if e == 5:  # zoom: from the centre outwards
            c = (t * speed * 0.3) % 1
            return base + (b * max(0.08, 1 - abs(abs(fx - 0.5) * 2 - c) * 4),)
        if e == 6:  # meteor
            c = (t * speed * 0.35) % 1.3
            d = c - pos
            return base + (b * (max(0.05, 1 - d * 3) if 0 <= d else 0.05),)
        if e == 7:  # twinkling
            v = 0.5 + 0.5 * math.sin(t * speed * 3 + fx * 37.0)
            return base + (b * (0.1 + 0.9 * v ** 6),)
        return base + (b,)

    def _draw(self, _area, cr, w, h):
        t = time.monotonic() - self._t0
        kw = min(w - 20, (h - 20) * 3.1)
        kh = kw / 3.1
        ox, oy = (w - kw) / 2, (h - kh) / 2
        rounded_rect(cr, ox - 8, oy - 8, kw + 16, kh + 16, 14)
        cr.set_source_rgba(1, 1, 1, 0.04)
        cr.fill()
        rows = len(self.ROWS)
        gap = kw * 0.008
        rh = (kh - gap * (rows - 1)) / rows
        for r, count in enumerate(self.ROWS):
            ry = oy + r * (rh + gap)
            if r == rows - 1:
                widths = [1, 1, 1, 1, 5.5, 1, 1, 1, 1]
            else:
                widths = [1.0] * count
                widths[0] += 0.25 * (r % 3)
                widths[-1] += 0.25 * (r % 3)
            total = sum(widths)
            kx = ox
            unit = (kw - gap * (len(widths) - 1)) / total
            for wu in widths:
                kwid = unit * wu
                cx = (kx + kwid / 2 - ox) / kw
                cr_r, cg, cb, inten = self._key_color(cx, t)
                rounded_rect(cr, kx, ry, kwid, rh, 4)
                cr.set_source_rgba(0.08, 0.09, 0.11, 1)
                cr.fill_preserve()
                cr.set_source_rgba(cr_r, cg, cb, 0.12 + 0.75 * inten)
                cr.set_line_width(1.3)
                cr.stroke()
                rounded_rect(cr, kx + 3, ry + rh - 5, kwid - 6, 2.2, 1)
                cr.set_source_rgba(cr_r, cg, cb, inten)
                cr.fill()
                kx += kwid + gap
        if self.effect is None:
            for z in range(4):
                draw_text(cr, "ZONE %d" % (z + 1), ox + kw * (z + 0.5) / 4, oy + kh + 16, size=7, color=MUTED)
