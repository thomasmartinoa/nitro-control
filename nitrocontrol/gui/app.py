"""Nitro Control GTK4 / libadwaita front end."""

import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, __version__  # noqa: E402
from .. import hw as hwmod  # noqa: E402
from ..client import Client, DaemonUnavailable, NitroError  # noqa: E402
from ..fancurve import CURVES, SAFETY_FLOOR  # noqa: E402
from ..sysinfo import SysInfo  # noqa: E402
from .widgets import ACCENT, AMBER, CYAN, CurveEditor, Gauge, HistoryGraph, KeyboardPreview  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_URL = "https://github.com/thomasmartinoa/nitro-control"
LINUWU_URL = "https://github.com/0x7375646F/Linuwu-Sense"

PROFILE_DESC = {
    "low-power": "Longest battery life",
    "cool": "Keep the chassis cool",
    "quiet": "Whisper-quiet fans",
    "balanced": "Everyday use",
    "balanced-performance": "Extra power for games",
    "performance": "Maximum power, AC only",
    "custom": "Vendor custom",
}
FAN_MODES = [("auto", "Auto"), ("max", "Max"), ("custom", "Custom"), ("curve", "Curve")]
PALETTES = [
    ("Nitro", ["#ed1c34"] * 4),
    ("Ice", ["#00d9f2", "#0099ff", "#3355ff", "#7a2cff"]),
    ("Sunset", ["#ff2a6d", "#ff6b35", "#ffb020", "#ffe066"]),
    ("Rainbow", ["#ff0040", "#ffcc00", "#00ff88", "#0088ff"]),
    ("Matrix", ["#00ff41"] * 4),
    ("Vapor", ["#ff00c8", "#b300ff", "#5a00ff", "#00e5ff"]),
    ("White", ["#ffffff"] * 4),
]


# ------------------------------------------------------------------- helpers
class Debounce:
    def __init__(self, ms, fn):
        self.ms, self.fn, self.id = ms, fn, None

    def __call__(self, *args):
        if self.id:
            GLib.source_remove(self.id)
        self.id = GLib.timeout_add(self.ms, self._fire, args)

    def _fire(self, args):
        self.id = None
        self.fn(*args)
        return False


def label(text="", css=None, xalign=0.0, wrap=False, selectable=False):
    lbl = Gtk.Label(label=text, xalign=xalign)
    if wrap:
        lbl.set_wrap(True)
    if selectable:
        lbl.set_selectable(True)
    for c in (css or "").split():
        lbl.add_css_class(c)
    return lbl


def card(child=None, css="card-n", spacing=10):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    for c in css.split():
        box.add_css_class(c)
    if child is not None:
        box.append(child)
    return box


def stat_tile(caption):
    box = card(spacing=2)
    value = label("--", "stat-value")
    sub = label("", "stat-label dim")
    box.append(label(caption.upper(), "stat-label"))
    box.append(value)
    box.append(sub)
    box.value, box.sub = value, sub
    return box


def flow(max_per_line=4, min_per_line=1, spacing=12):
    fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                     max_children_per_line=max_per_line, min_children_per_line=min_per_line,
                     column_spacing=spacing, row_spacing=spacing)
    return fb


def color_button(hexstr, on_change):
    rgba = Gdk.RGBA()
    rgba.parse(hexstr)
    if hasattr(Gtk, "ColorDialogButton"):
        dlg = Gtk.ColorDialog()
        dlg.set_with_alpha(False)
        btn = Gtk.ColorDialogButton(dialog=dlg)
        btn.set_rgba(rgba)
        btn.connect("notify::rgba", lambda *_: on_change())
    else:
        btn = Gtk.ColorButton()
        btn.set_use_alpha(False)
        btn.set_rgba(rgba)
        btn.connect("color-set", lambda *_: on_change())
    return btn


def rgba_hex(btn):
    c = btn.get_rgba()
    return "#%02x%02x%02x" % (int(round(c.red * 255)), int(round(c.green * 255)), int(round(c.blue * 255)))


def set_btn_color(btn, hexstr):
    rgba = Gdk.RGBA()
    rgba.parse(hexstr)
    btn.set_rgba(rgba)


def nitro_scale(lo, hi, step, value, digits=0):
    adj = Gtk.Adjustment(value=value, lower=lo, upper=hi, step_increment=step, page_increment=step * 5)
    sc = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj, digits=digits, hexpand=True)
    sc.set_draw_value(True)
    sc.set_value_pos(Gtk.PositionType.RIGHT)
    sc.add_css_class("nitro")
    return sc


def segmented(options, on_select):
    """options: [(key, label)] -> (box, {key: ToggleButton})"""
    box = Gtk.Box(css_classes=["linked", "seg"])
    buttons, first = {}, None
    for key, text in options:
        b = Gtk.ToggleButton(label=text)
        if first:
            b.set_group(first)
        else:
            first = b
        b.connect("toggled", lambda btn, k=key: btn.get_active() and on_select(k))
        box.append(b)
        buttons[key] = b
    return box, buttons


def fmt_hours(h):
    if h is None:
        return ""
    m = int(round(h * 60))
    return "%dh %02dm" % (m // 60, m % 60)


def esc(text):
    """Adw rows parse titles as Pango markup."""
    return GLib.markup_escape_text(str(text)) if text else ""


def switch_row(title, subtitle, on_toggle):
    row = Adw.ActionRow(title=esc(title), subtitle=esc(subtitle))
    sw = Gtk.Switch(valign=Gtk.Align.CENTER)
    sw.connect("notify::active", lambda s, _p: on_toggle(s.get_active()))
    row.add_suffix(sw)
    row.set_activatable_widget(sw)
    row.switch = sw
    return row


def combo_row(title, subtitle, items, on_select):
    row = Adw.ComboRow(title=esc(title), subtitle=esc(subtitle))
    row.set_model(Gtk.StringList.new(items))
    row.items = list(items)
    row.connect("notify::selected", lambda r, _p: on_select(r.get_selected()))
    return row


# ---------------------------------------------------------------------- pages
class Page(Gtk.ScrolledWindow):
    title = ""
    icon = ""
    subtitle = ""

    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win = win
        self.caps = win.caps
        self.quiet = False
        clamp = Adw.Clamp(maximum_size=1180, tightening_threshold=900)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                           margin_top=22, margin_bottom=36, margin_start=26, margin_end=26)
        clamp.set_child(self.box)
        self.set_child(clamp)
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head.append(label(self.title, "page-title"))
        if self.subtitle:
            head.append(label(self.subtitle, "page-sub", wrap=True))
        self.box.append(head)

    def section(self, text):
        lbl = label(text.upper(), "section-title")
        lbl.set_margin_top(8)
        self.box.append(lbl)
        return lbl

    def silently(self, fn, *args):
        self.quiet = True
        try:
            fn(*args)
        finally:
            self.quiet = False

    def update(self, st, si):
        pass

    def on_state(self, state):
        pass


class DashboardPage(Page):
    title, icon = "Dashboard", "nc-dashboard-symbolic"
    subtitle = "Live temperatures, fan speeds and one-click modes"

    def __init__(self, win):
        super().__init__(win)
        gauges = flow(4, 2)
        self.g_cpu = Gauge("CPU", "°C", 20, 100, size=132)
        self.g_gpu = Gauge("GPU", "°C", 20, 100, size=132)
        self.g_fcpu = Gauge("CPU fan", "RPM", 0, 6000, size=132)
        self.g_fgpu = Gauge("GPU fan", "RPM", 0, 6000, size=132)
        for g in (self.g_cpu, self.g_gpu, self.g_fcpu, self.g_fgpu):
            c = card(g, "card-n glow")
            g.set_halign(Gtk.Align.CENTER)
            gauges.append(c)
        self.box.append(gauges)

        self.section("Thermal mode")
        self.mode_box = flow(6, 2, 10)
        self.mode_buttons = {}
        first = None
        for prof in self.caps["profiles"]:
            b = Gtk.ToggleButton(css_classes=["mode-btn"])
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.append(label(hwmod.PROFILE_LABELS.get(prof, prof), "mode-name", xalign=0.5))
            inner.append(label(PROFILE_DESC.get(prof, prof), "mode-desc", xalign=0.5))
            b.set_child(inner)
            if first:
                b.set_group(first)
            else:
                first = b
            b.connect("toggled", self._on_profile, prof)
            self.mode_buttons[prof] = b
            self.mode_box.append(b)
        if not self.caps["profiles"]:
            self.box.append(label("No ACPI platform profiles exposed by this kernel.", "dim"))
        else:
            self.box.append(self.mode_box)

        row = Gtk.Box(spacing=12)
        fan_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        fan_col.append(label("FANS", "section-title"))
        self.fan_seg, self.fan_btns = segmented(FAN_MODES, self._on_fan)
        fan_col.append(self.fan_seg)
        row.append(fan_col)
        self.box.append(row)
        if not self.caps["fan_control"]:
            self.fan_seg.set_sensitive(False)

        self.section("History")
        graphs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.temp_graph = HistoryGraph([("cpu", "CPU", ACCENT), ("gpu", "GPU", CYAN), ("sys", "System", AMBER)],
                                       20, 100, "°")
        self.fan_graph = HistoryGraph([("cpu", "CPU fan", ACCENT), ("gpu", "GPU fan", CYAN)], 0, 6000, "",
                                      height=140, auto_hi=True)
        graphs.append(card(self.temp_graph))
        graphs.append(card(self.fan_graph))
        self.box.append(graphs)

        self.section("System")
        stats = flow(4, 2)
        self.s_load = stat_tile("CPU load")
        self.s_clock = stat_tile("CPU clock")
        self.s_mem = stat_tile("Memory")
        self.s_bat = stat_tile("Battery")
        self.s_power = stat_tile("Power draw")
        self.s_dgpu = stat_tile("Discrete GPU")
        self.s_boost = stat_tile("CPU boost")
        self.s_up = stat_tile("Uptime")
        for t in (self.s_load, self.s_clock, self.s_mem, self.s_bat, self.s_power, self.s_dgpu, self.s_boost, self.s_up):
            stats.append(t)
        self.box.append(stats)

    def _on_profile(self, btn, prof):
        if self.quiet or not btn.get_active():
            return
        self.win.run_cmd("set_profile", "Mode: %s" % hwmod.PROFILE_LABELS.get(prof, prof), profile=prof)

    def _on_fan(self, mode):
        if self.quiet:
            return
        self.win.run_cmd("set_fan", "Fans: %s" % mode, mode=mode)

    def update(self, st, si):
        t, f = st.get("temps", {}), st.get("fans", {})
        self.g_cpu.set_value(t.get("cpu"), "pkg %.0f°" % t["cpu_pkg"] if t.get("cpu_pkg") else "")
        self.g_gpu.set_value(t.get("gpu"), "system %.0f°" % t["sys"] if t.get("sys") else "")
        manual = st.get("fan_mode") in ("custom", "curve")
        for g, key in ((self.g_fcpu, "cpu"), (self.g_fgpu, "gpu")):
            fan = f.get(key, {})
            pct = fan.get("pct")
            g.set_value(fan.get("rpm"), ("%d%% duty" % pct) if manual and pct is not None else (st.get("fan_mode") or "").upper())
        self.temp_graph.push(t)
        self.fan_graph.push({k: v.get("rpm") for k, v in f.items()})

        prof = st.get("profile")
        if prof in self.mode_buttons and not self.mode_buttons[prof].get_active():
            self.silently(self.mode_buttons[prof].set_active, True)
        mode = st.get("fan_mode")
        if mode in self.fan_btns and not self.fan_btns[mode].get_active():
            self.silently(self.fan_btns[mode].set_active, True)
        daemon = st.get("daemon")
        self.mode_box.set_sensitive(bool(daemon))
        self.fan_seg.set_sensitive(bool(daemon and self.caps["fan_control"]))

        if si.get("cpu_usage") is not None:
            self.s_load.value.set_text("%.0f %%" % si["cpu_usage"])
        cf = si.get("cpu_freq")
        if cf:
            self.s_clock.value.set_text("%.2f GHz" % cf["avg"])
            self.s_clock.sub.set_text("peak %.2f GHz" % cf["max"])
        mem = si.get("memory")
        if mem:
            self.s_mem.value.set_text("%.1f GB" % (mem["used"] / 1e9))
            self.s_mem.sub.set_text("%.0f%% of %.0f GB" % (mem["pct"], mem["total"] / 1e9))
        b = st.get("battery")
        if b:
            self.s_bat.value.set_text("%s %%" % b.get("capacity"))
            self.s_bat.sub.set_text(("%s · %s" % (b.get("status"), fmt_hours(b["time_left"]))) if b.get("time_left") else (b.get("status") or ""))
            self.s_power.value.set_text("%.1f W" % b["power"] if b.get("power") is not None else "--")
            self.s_power.sub.set_text("on AC" if st.get("ac_online") else "on battery")
        dg = st.get("dgpu_state")
        nv = si.get("nvidia")
        if nv:
            self.s_dgpu.value.set_text("%.0f %% · %.0f°" % (nv["util"] or 0, nv["temp"] or 0))
            self.s_dgpu.sub.set_text("%.0f W · %.0f MHz" % (nv["power"] or 0, nv["clock"] or 0))
        else:
            self.s_dgpu.value.set_text({"active": "Awake", "suspended": "Sleeping"}.get(dg, dg or "--"))
            self.s_dgpu.sub.set_text("details on AC only" if dg == "active" else "saving power" if dg == "suspended" else "")
        if st.get("cpu_boost") is not None:
            self.s_boost.value.set_text("On" if st["cpu_boost"] else "Off")
            self.s_boost.sub.set_text("EPP: %s" % (st.get("epp") or "--"))
        if si.get("uptime"):
            up = int(si["uptime"])
            self.s_up.value.set_text("%dh %02dm" % (up // 3600, up % 3600 // 60))


class FansPage(Page):
    title, icon = "Fans", "nc-fan-symbolic"
    subtitle = "Firmware auto, full blast, fixed speed, or your own temperature curve"

    def __init__(self, win):
        super().__init__(win)
        self.curves = {k: [list(p) for p in v] for k, v in (("cpu", CURVES["balanced"]), ("gpu", CURVES["balanced"]))}
        self.saved_curves = None
        self.fan_key = "cpu"
        self.mode = None
        self.last_st = {}

        if not self.caps["fan_control"]:
            msg = card()
            msg.append(label("Fan control is not available", "preset-name"))
            msg.append(label("Your kernel's acer_wmi driver does not expose fan PWM control for this model. "
                             "Recent kernels support it on several Nitro/Predator models (including AN515-58); "
                             "try a newer kernel or install the Linuwu-Sense driver.", wrap=True))
            self.box.append(msg)

        top = card()
        head = Gtk.Box(spacing=12)
        head.append(label("MODE", "section-title"))
        self.live = label("", "dim", xalign=1.0)
        self.live.set_hexpand(True)
        head.append(self.live)
        top.append(head)
        self.seg, self.btns = segmented(FAN_MODES, self._on_mode)
        top.append(self.seg)
        self.mode_help = label("", "dim", wrap=True)
        top.append(self.mode_help)
        self.box.append(top)

        # custom speeds
        self.section("Fixed speed")
        cust = card()
        self.link = Gtk.CheckButton(label="Link CPU and GPU fans", active=True)
        cust.append(self.link)
        self.sl = {}
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        for i, (key, text) in enumerate((("cpu", "CPU fan"), ("gpu", "GPU fan"))):
            grid.attach(label(text, "stat-label"), 0, i, 1, 1)
            sc = nitro_scale(0, 100, 5, 50)
            sc.connect("value-changed", self._on_slider, key)
            grid.attach(sc, 1, i, 1, 1)
            self.sl[key] = sc
        cust.append(grid)
        cust.append(label("Moving a slider switches to Custom mode. Above 75 °C a safety floor keeps fans spinning "
                          "(40 % at 75 °C, 70 % at 85 °C, 100 % at 90 °C).", "dim", wrap=True))
        self.box.append(cust)
        self._send_custom = Debounce(300, self._push_custom)

        # curve editor
        self.section("Fan curve")
        cv = card()
        bar = Gtk.Box(spacing=10)
        seg, self.fan_sel = segmented([("cpu", "CPU fan"), ("gpu", "GPU fan")], self._on_pick_fan)
        bar.append(seg)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        for name in ("silent", "balanced", "performance"):
            b = Gtk.Button(label=name.capitalize(), css_classes=["flat"])
            b.connect("clicked", self._load_builtin, name)
            bar.append(b)
        cv.append(bar)
        self.editor = CurveEditor()
        self.editor.floor = SAFETY_FLOOR
        self.editor.set_points(self.curves["cpu"])
        self.editor.connect("changed", self._on_curve_edit)
        cv.append(self.editor)
        cv.append(label("Drag points to shape the curve · click empty space to add a point · right-click to remove. "
                        "Speed never decreases as temperature rises; fans slow down gently with 3 °C hysteresis.",
                        "dim", wrap=True))
        actions = Gtk.Box(spacing=10)
        self.hottest = Gtk.CheckButton(label="Both fans follow the hottest sensor")
        self.hottest.connect("toggled", self._on_source)
        actions.append(self.hottest)
        actions.append(Gtk.Box(hexpand=True))
        self.dirty_lbl = label("", "warn-text")
        actions.append(self.dirty_lbl)
        copy_btn = Gtk.Button(label="Copy to other fan")
        copy_btn.connect("clicked", self._copy_other)
        actions.append(copy_btn)
        revert = Gtk.Button(label="Revert")
        revert.connect("clicked", self._revert)
        actions.append(revert)
        apply_btn = Gtk.Button(label="Apply curve", css_classes=["suggested-action"])
        apply_btn.connect("clicked", self._apply_curve)
        actions.append(apply_btn)
        cv.append(actions)
        self.box.append(cv)
        self.fan_sel["cpu"].set_active(True)

        # safety
        self.section("Safety")
        grp = Adw.PreferencesGroup(description=(
            "Nitro Control only uses the kernel driver's fan interface; the firmware keeps its own thermal protection. "
            "In Custom and Curve modes it adds: full-speed override at the critical temperature, a speed floor when hot, "
            "a hand-back to firmware control if sensors fail, and automatic mode whenever the service stops."))
        self.spin = {}
        for key, text, lo, hi in (("cpu_critical", "CPU critical temperature", 75, 100),
                                  ("gpu_critical", "GPU critical temperature", 70, 95)):
            row = Adw.ActionRow(title=text, subtitle="Fans go to 100 % at or above this (°C)")
            sp = Gtk.SpinButton.new_with_range(lo, hi, 1)
            sp.set_valign(Gtk.Align.CENTER)
            sp.connect("value-changed", self._on_safety)
            row.add_suffix(sp)
            self.spin[key] = sp
            grp.add(row)
        self.box.append(grp)
        self._send_safety = Debounce(700, self._push_safety)

        self.section("Recent events")
        self.events_box = card(css="card-n event-log", spacing=4)
        self.box.append(self.events_box)
        self._events_sig = None

    # -- handlers
    def _on_mode(self, mode):
        if self.quiet:
            return
        if mode == "curve" and self.saved_curves and self.curves != self.saved_curves:
            self._apply_curve()
            return
        self.win.run_cmd("set_fan", "Fans: %s" % mode, mode=mode)

    def _on_slider(self, sc, key):
        if self.quiet:
            return
        if self.link.get_active():
            other = self.sl["gpu" if key == "cpu" else "cpu"]
            self.silently(other.set_value, sc.get_value())
        self._send_custom()

    def _push_custom(self):
        self.win.run_cmd("set_fan", None, mode="custom",
                         cpu=int(self.sl["cpu"].get_value()), gpu=int(self.sl["gpu"].get_value()))

    def _on_pick_fan(self, key):
        self.fan_key = key
        self.editor.color = ACCENT if key == "cpu" else CYAN
        self.editor.set_points(self.curves[key])
        self._update_editor_live()

    def _on_curve_edit(self, _ed):
        self.curves[self.fan_key] = [list(p) for p in self.editor.points]
        self._mark_dirty()

    def _mark_dirty(self):
        dirty = self.saved_curves is not None and self.curves != self.saved_curves
        self.dirty_lbl.set_text("unsaved changes" if dirty else "")

    def _load_builtin(self, _b, name):
        self.curves[self.fan_key] = [list(p) for p in CURVES[name]]
        self.editor.set_points(self.curves[self.fan_key])
        self._mark_dirty()

    def _copy_other(self, _b):
        other = "gpu" if self.fan_key == "cpu" else "cpu"
        self.curves[other] = [list(p) for p in self.curves[self.fan_key]]
        self._mark_dirty()
        self.win.toast("Copied to %s fan — press Apply to save" % other.upper())

    def _revert(self, _b):
        if self.saved_curves:
            self.curves = {k: [list(p) for p in v] for k, v in self.saved_curves.items()}
            self.editor.set_points(self.curves[self.fan_key])
            self._mark_dirty()

    def _apply_curve(self, *_):
        def work(client):
            client.call("set_curve", fan="cpu", points=self.curves["cpu"])
            client.call("set_curve", fan="gpu", points=self.curves["gpu"])
            client.call("set_fan", mode="curve")
        self.win.run_task(work, "Fan curves applied", refresh_state=True)

    def _on_source(self, btn):
        if self.quiet:
            return
        self.win.run_cmd("set_curve_source", None, source="max" if btn.get_active() else "own")

    def _on_safety(self, _sp):
        if not self.quiet:
            self._send_safety()

    def _push_safety(self):
        self.win.run_cmd("set_safety", "Safety limits saved", refresh_state=True,
                         cpu_critical=int(self.spin["cpu_critical"].get_value()),
                         gpu_critical=int(self.spin["gpu_critical"].get_value()))

    # -- updates
    def on_state(self, state):
        saved = {k: [list(p) for p in state["curves"][k]] for k in ("cpu", "gpu")}
        was_dirty = self.saved_curves is not None and self.curves != self.saved_curves
        self.saved_curves = saved
        if not was_dirty:
            self.curves = {k: [list(p) for p in v] for k, v in saved.items()}
            self.editor.set_points(self.curves[self.fan_key])
        self._mark_dirty()
        self.silently(self.hottest.set_active, state.get("curve_source") == "max")
        for key in ("cpu_critical", "gpu_critical"):
            self.silently(self.spin[key].set_value, state["safety"][key])
        for key in ("cpu", "gpu"):
            if not self.sl[key].has_focus():
                self.silently(self.sl[key].set_value, state["custom"][key])

    def _update_editor_live(self):
        st = self.last_st
        t = st.get("temps", {})
        if st.get("fan_mode") == "curve" and st.get("daemon"):
            self.editor.current_pct = st.get("fans", {}).get(self.fan_key, {}).get("pct")
        else:
            self.editor.current_pct = None
        temp = t.get(self.fan_key, t.get("cpu"))
        if self.hottest.get_active():
            vals = [t[k] for k in ("cpu", "gpu") if k in t]
            temp = max(vals) if vals else None
        self.editor.current_temp = temp
        self.editor.queue_draw()

    def update(self, st, si):
        self.last_st = st
        mode = st.get("fan_mode")
        if mode in self.btns and not self.btns[mode].get_active():
            self.silently(self.btns[mode].set_active, True)
        self.seg.set_sensitive(bool(st.get("daemon") and self.caps["fan_control"]))
        help_text = {
            "auto": "The firmware (EC) decides fan speed — same as Windows NitroSense 'Auto'.",
            "max": "Both fans at full speed (firmware 'Max' / Coolboost).",
            "custom": "Fixed duty cycle set below, never below the safety floor.",
            "curve": "Nitro Control drives the fans from your temperature curve once per second.",
        }.get(mode, "")
        if st.get("emergency"):
            help_text = "⚠ Critical temperature reached — fans forced to maximum until things cool down."
        self.mode_help.set_text(help_text)
        f, t = st.get("fans", {}), st.get("temps", {})
        self.live.set_text("  ·  ".join("%s %s rpm %s" % (k.upper(), f[k].get("rpm", "--"),
                                                          ("%.0f°C" % t[k]) if k in t else "") for k in f))
        self._update_editor_live()

        events = st.get("events") or []
        sig = (len(events), events[-1]["t"] if events else 0)
        if sig != self._events_sig:
            self._events_sig = sig
            child = self.events_box.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.events_box.remove(child)
                child = nxt
            if not events:
                self.events_box.append(label("No events yet." if st.get("daemon") else "Service offline.", "dim"))
            for e in reversed(events[-8:]):
                lbl = label("%s  %s" % (time.strftime("%H:%M:%S", time.localtime(e["t"])), e["msg"]),
                            "warn-text" if e.get("level") == "warn" else "", wrap=True)
                self.events_box.append(lbl)


class PerformancePage(Page):
    title, icon = "Performance", "nc-bolt-symbolic"
    subtitle = "Thermal profiles, CPU boost and automatic switching on AC / battery"

    def __init__(self, win):
        super().__init__(win)
        self.preset_names = []
        grp = Adw.PreferencesGroup(title="Thermal profile",
                                   description="Sets the firmware power limits and fan behaviour (acer_wmi platform profile).")
        self.prof_checks = {}
        first = None
        for prof in self.caps["profiles"]:
            row = Adw.ActionRow(title=hwmod.PROFILE_LABELS.get(prof, prof),
                                subtitle="%s  ·  %s" % (PROFILE_DESC.get(prof, ""), prof))
            chk = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first:
                chk.set_group(first)
            else:
                first = chk
            chk.connect("toggled", self._on_prof, prof)
            row.add_prefix(chk)
            row.set_activatable_widget(chk)
            self.prof_checks[prof] = chk
            grp.add(row)
        if not self.caps["profiles"]:
            grp.set_description("No platform profiles available on this kernel.")
        elif self.caps.get("ppd"):
            grp.set_description("Sets the firmware power limits and fan behaviour. Synced with your desktop's power "
                                "widget (power-profiles-daemon): Saver, Balanced and Performance there are the same "
                                "modes as here, so the two always agree.")
        self.box.append(grp)

        cpu = Adw.PreferencesGroup(title="Processor")
        self.boost = switch_row("CPU turbo boost", "Allow clocks above base frequency. Off = cooler & quieter, slower.",
                                self._on_boost)
        self.boost.set_sensitive(self.caps["cpu_boost"])
        cpu.add(self.boost)
        self.epp = combo_row("Energy performance preference",
                             "Hint to the CPU on how to trade power for speed", self.caps["epp"] or ["n/a"], self._on_epp)
        self.epp.set_sensitive(bool(self.caps["epp"]))
        cpu.add(self.epp)
        self.gov = combo_row("CPU governor", "Frequency scaling policy", self.caps["governors"] or ["n/a"], self._on_gov)
        self.gov.set_sensitive(bool(self.caps["governors"]))
        cpu.add(self.gov)
        self.box.append(cpu)

        auto = Adw.PreferencesGroup(title="Automatic switching",
                                    description="Apply a preset automatically when you plug in or unplug the charger.")
        self.auto_sw = switch_row("Switch preset on power change", "", self._on_auto)
        auto.add(self.auto_sw)
        self.ac_row = combo_row("On AC power", "", ["—"], lambda i: self._on_auto_pick("ac", i))
        self.bat_row = combo_row("On battery", "", ["—"], lambda i: self._on_auto_pick("battery", i))
        auto.add(self.ac_row)
        auto.add(self.bat_row)
        self.box.append(auto)

    def _on_prof(self, chk, prof):
        if self.quiet or not chk.get_active():
            return
        self.win.run_cmd("set_profile", "Mode: %s" % hwmod.PROFILE_LABELS.get(prof, prof), profile=prof)

    def _on_boost(self, active):
        if not self.quiet:
            self.win.run_cmd("set_boost", "CPU boost %s" % ("on" if active else "off"), enabled=active)

    def _on_epp(self, idx):
        if not self.quiet and self.caps["epp"]:
            self.win.run_cmd("set_epp", "EPP: %s" % self.caps["epp"][idx], value=self.caps["epp"][idx])

    def _on_gov(self, idx):
        if not self.quiet and self.caps["governors"]:
            self.win.run_cmd("set_governor", "Governor: %s" % self.caps["governors"][idx], value=self.caps["governors"][idx])

    def _on_auto(self, active):
        if not self.quiet:
            self.win.run_cmd("set_auto_switch", "Auto switching %s" % ("on" if active else "off"),
                             refresh_state=True, enabled=active)

    def _on_auto_pick(self, which, idx):
        if self.quiet or idx >= len(self.preset_names):
            return
        name = self.preset_names[idx] if idx > 0 else ""
        self.win.run_cmd("set_auto_switch", None, refresh_state=True, **{which: name})

    def on_state(self, state):
        names = ["—"] + list(state["builtin_presets"]) + list(state.get("presets") or {})
        self.preset_names = names
        a = state.get("auto_switch") or {}

        def fill():
            for row, key in ((self.ac_row, "ac"), (self.bat_row, "battery")):
                row.set_model(Gtk.StringList.new(names))
                row.set_selected(names.index(a.get(key)) if a.get(key) in names else 0)
            self.auto_sw.switch.set_active(bool(a.get("enabled")))
        self.silently(fill)

    def update(self, st, si):
        daemon = bool(st.get("daemon"))
        for w in (self.boost, self.epp, self.gov, self.auto_sw, self.ac_row, self.bat_row):
            w.set_sensitive(daemon)
        prof = st.get("profile")
        if prof in self.prof_checks and not self.prof_checks[prof].get_active():
            self.silently(self.prof_checks[prof].set_active, True)
        for chk in self.prof_checks.values():
            chk.set_sensitive(daemon)
        if st.get("cpu_boost") is not None and self.boost.switch.get_active() != st["cpu_boost"]:
            self.silently(self.boost.switch.set_active, st["cpu_boost"])
        for row, value, choices in ((self.epp, st.get("epp"), self.caps["epp"]),
                                    (self.gov, st.get("governor"), self.caps["governors"])):
            if value in choices and row.get_selected() != choices.index(value):
                self.silently(row.set_selected, choices.index(value))


class LightingPage(Page):
    title, icon = "Lighting", "nc-keyboard-symbolic"
    subtitle = "4-zone RGB keyboard colours and effects"

    def __init__(self, win):
        super().__init__(win)
        self.loaded = False
        caps = self.caps
        if caps["keyboard_rgb"]:
            self._build_rgb()
        elif caps["keyboard_led"]:
            grp = Adw.PreferencesGroup(title="Keyboard backlight")
            row = Adw.ActionRow(title="Brightness")
            self.led = nitro_scale(0, caps["keyboard_led_max"] or 3, 1, 0)
            self.led.set_size_request(260, -1)
            self.led_send = Debounce(250, lambda: self.win.run_cmd("set_kbd_led", None, value=int(self.led.get_value())))
            self.led.connect("value-changed", lambda *_: (not self.quiet) and self.led_send())
            row.add_suffix(self.led)
            grp.add(row)
            self.box.append(grp)
        else:
            info = card()
            info.append(label("RGB control needs the Linuwu-Sense driver", "preset-name"))
            info.append(label(
                "The mainline acer_wmi driver (what you're running) handles fans, temperatures and thermal profiles, "
                "but not the 4-zone RGB keyboard. The optional, open-source Linuwu-Sense kernel module adds it "
                "(plus an 80% battery limiter, LCD overdrive, USB charging and boot-sound toggles). "
                "Nitro Control detects it automatically once installed — nothing else to configure.", wrap=True))
            info.append(label("Set it up with one command from the Nitro Control folder:", "dim", wrap=True))
            info.append(label("sudo ./install.sh --with-rgb", "stat-value", selectable=True))
            info.append(label("It downloads a pinned, checksum-verified version, applies Nitro Control's kernel 7.x "
                              "and safety fixes, builds it with DKMS (rebuilt on kernel updates) and switches drivers "
                              "with automatic fallback to acer_wmi. Undo anytime with ./install.sh --uninstall. "
                              "Secure Boot must be off (or DKMS signing set up).", "dim", wrap=True))
            btn = Gtk.LinkButton(uri=LINUWU_URL, label="Open Linuwu-Sense on GitHub", halign=Gtk.Align.START)
            info.append(btn)
            self.box.append(info)
            self.preview = KeyboardPreview()
            self.preview.set_effect(3, "#ed1c34", 100, 4, 2)
            demo = card(self.preview)
            demo.append(label("Preview only", "dim", xalign=0.5))
            self.box.append(demo)

        if caps["toggles"]:
            grp = Adw.PreferencesGroup(title="Extras (Linuwu-Sense)")
            self.toggle_rows = {}
            for name, meta in caps["toggles"].items():
                if meta["values"] == [0, 1]:
                    row = switch_row(meta["label"], meta["desc"],
                                     lambda on, n=name: (not self.quiet) and self.win.run_cmd("set_toggle", None, name=n, value=int(on)))
                else:
                    items = ["Off" if v == 0 else "Until %d%%" % v for v in meta["values"]]
                    row = combo_row(meta["label"], meta["desc"], items,
                                    lambda i, n=name, vals=meta["values"]: (not self.quiet) and self.win.run_cmd("set_toggle", None, name=n, value=vals[i]))
                row.values = meta["values"]
                self.toggle_rows[name] = row
                grp.add(row)
            self.box.append(grp)

    def _build_rgb(self):
        self.preview = KeyboardPreview()
        self.box.append(card(self.preview, "card-n glow"))

        seg, self.kb_mode = segmented([("zones", "Static zones"), ("effect", "Effects")], self._on_kb_mode)
        self.box.append(seg)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        self.box.append(self.stack)

        # zones
        z = card()
        row = Gtk.Box(spacing=16)
        self.zone_btns = []
        for i in range(4):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            col.append(label("ZONE %d" % (i + 1), "stat-label", xalign=0.5))
            b = color_button("#ed1c34", self._preview_zones)
            self.zone_btns.append(b)
            col.append(b)
            row.append(col)
        z.append(row)
        pal = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8, column_spacing=6)
        for name, colors in PALETTES:
            b = Gtk.Button(label=name)
            b.connect("clicked", self._palette, colors)
            pal.append(b)
        z.append(pal)
        br = Gtk.Box(spacing=12)
        br.append(label("Brightness", "stat-label"))
        self.z_bright = nitro_scale(0, 100, 5, 100)
        self.z_bright.connect("value-changed", lambda *_: self._preview_zones())
        br.append(self.z_bright)
        z.append(br)
        btns = Gtk.Box(spacing=10, halign=Gtk.Align.END)
        off = Gtk.Button(label="Lights off")
        off.connect("clicked", lambda *_: self.win.run_cmd("set_keyboard", "Keyboard lighting off", refresh_state=True,
                                                          mode="zones", colors=["000000"] * 4, brightness=0))
        btns.append(off)
        ap = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        ap.connect("clicked", self._apply_zones)
        btns.append(ap)
        z.append(btns)
        self.stack.add_named(z, "zones")

        # effects
        e = card()
        grp = Adw.PreferencesGroup()
        self.fx = combo_row("Effect", "", hwmod.KB_EFFECTS, lambda i: self._preview_effect())
        self.fx.set_selected(3)
        grp.add(self.fx)
        self.dir_row = combo_row("Direction", "", ["Right to left", "Left to right"], lambda i: self._preview_effect())
        self.dir_row.set_selected(1)
        grp.add(self.dir_row)
        crow = Adw.ActionRow(title="Colour", subtitle="Used by single-colour effects")
        self.fx_color = color_button("#ed1c34", self._preview_effect)
        self.fx_color.set_valign(Gtk.Align.CENTER)
        crow.add_suffix(self.fx_color)
        grp.add(crow)
        e.append(grp)
        g = Gtk.Grid(column_spacing=14, row_spacing=8)
        g.attach(label("Speed", "stat-label"), 0, 0, 1, 1)
        self.fx_speed = nitro_scale(0, 9, 1, 4)
        self.fx_speed.connect("value-changed", lambda *_: self._preview_effect())
        g.attach(self.fx_speed, 1, 0, 1, 1)
        g.attach(label("Brightness", "stat-label"), 0, 1, 1, 1)
        self.fx_bright = nitro_scale(0, 100, 5, 100)
        self.fx_bright.connect("value-changed", lambda *_: self._preview_effect())
        g.attach(self.fx_bright, 1, 1, 1, 1)
        e.append(g)
        ap2 = Gtk.Button(label="Apply", css_classes=["suggested-action"], halign=Gtk.Align.END)
        ap2.connect("clicked", self._apply_effect)
        e.append(ap2)
        self.stack.add_named(e, "effect")
        self.kb_mode["zones"].set_active(True)

    def _on_kb_mode(self, mode):
        self.stack.set_visible_child_name(mode)
        self._preview_zones() if mode == "zones" else self._preview_effect()

    def _palette(self, _b, colors):
        for btn, c in zip(self.zone_btns, colors):
            self.silently(set_btn_color, btn, c)
        self._preview_zones()

    def _zone_colors(self):
        return [rgba_hex(b) for b in self.zone_btns]

    def _preview_zones(self):
        self.preview.set_static(self._zone_colors(), self.z_bright.get_value())

    def _fx_args(self):
        return {"effect": self.fx.get_selected(), "speed": int(self.fx_speed.get_value()),
                "brightness": int(self.fx_bright.get_value()), "direction": 1 if self.dir_row.get_selected() == 0 else 2,
                "color": rgba_hex(self.fx_color)}

    def _preview_effect(self):
        if not hasattr(self, "fx_bright"):
            return  # still building the page
        a = self._fx_args()
        self.preview.set_effect(a["effect"], a["color"], a["brightness"], a["speed"], a["direction"])

    def _apply_zones(self, *_):
        self.win.run_cmd("set_keyboard", "Keyboard colours applied", refresh_state=True, mode="zones",
                         colors=[c.lstrip("#") for c in self._zone_colors()], brightness=int(self.z_bright.get_value()))

    def _apply_effect(self, *_):
        a = self._fx_args()
        a["color"] = a["color"].lstrip("#")
        self.win.run_cmd("set_keyboard", "%s effect applied" % hwmod.KB_EFFECTS[a["effect"]], refresh_state=True,
                         mode="effect", **a)

    def on_state(self, state):
        kb = state.get("keyboard")
        if self.loaded or not kb or not self.caps["keyboard_rgb"]:
            return
        self.loaded = True

        def load():
            if kb.get("mode") == "zones" and kb.get("colors"):
                for btn, c in zip(self.zone_btns, kb["colors"]):
                    set_btn_color(btn, "#" + str(c).lstrip("#"))
                self.z_bright.set_value(kb.get("brightness", 100))
                self.kb_mode["zones"].set_active(True)
            elif kb.get("mode") == "effect":
                self.fx.set_selected(int(kb.get("effect", 3)))
                self.fx_speed.set_value(kb.get("speed", 4))
                self.fx_bright.set_value(kb.get("brightness", 100))
                self.dir_row.set_selected(0 if kb.get("direction") == 1 else 1)
                set_btn_color(self.fx_color, "#" + str(kb.get("color", "ed1c34")).lstrip("#"))
                self.kb_mode["effect"].set_active(True)
        self.silently(load)
        self._on_kb_mode("effect" if kb.get("mode") == "effect" else "zones")

    def update(self, st, si):
        daemon = bool(st.get("daemon"))
        if self.caps["keyboard_led"] and not self.caps["keyboard_rgb"]:
            self.led.set_sensitive(daemon)
            if st.get("kbd_led") is not None and not self.led.has_focus():
                self.silently(self.led.set_value, st["kbd_led"])
        for name, row in getattr(self, "toggle_rows", {}).items():
            row.set_sensitive(daemon)
            val = (st.get("toggles") or {}).get(name)
            if val is None:
                continue
            if hasattr(row, "switch"):
                if row.switch.get_active() != bool(val):
                    self.silently(row.switch.set_active, bool(val))
            elif val in row.values and row.get_selected() != row.values.index(val):
                self.silently(row.set_selected, row.values.index(val))


class BatteryPage(Page):
    title, icon = "Battery", "nc-battery-symbolic"
    subtitle = "Health, power draw and charge limiting"

    def __init__(self, win):
        super().__init__(win)
        top = card(css="card-n glow")
        row = Gtk.Box(spacing=18)
        self.big = label("--%", "big-num")
        row.append(self.big)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.state_lbl = label("", "preset-name")
        self.eta = label("", "dim")
        col.append(self.state_lbl)
        col.append(self.eta)
        row.append(col)
        top.append(row)
        self.level = Gtk.LevelBar(min_value=0, max_value=100)
        self.level.set_size_request(-1, 12)
        top.append(self.level)
        self.box.append(top)

        stats = flow(4, 2)
        self.t_health = stat_tile("Health")
        self.t_cycles = stat_tile("Charge cycles")
        self.t_cap = stat_tile("Capacity")
        self.t_power = stat_tile("Power")
        self.t_volt = stat_tile("Voltage")
        self.t_tech = stat_tile("Cell")
        for t in (self.t_health, self.t_cycles, self.t_cap, self.t_power, self.t_volt, self.t_tech):
            stats.append(t)
        self.box.append(stats)

        grp = Adw.PreferencesGroup(title="Charge limit")
        mode = self.caps["battery_limit"]
        if mode == "threshold":
            grp.set_description("Stop charging at a set level. Keeping a laptop that lives on the charger at 60–80 % "
                                "noticeably slows battery wear.")
            row = Adw.ActionRow(title="Stop charging at")
            self.limit = nitro_scale(50, 100, 5, 100)
            self.limit.set_size_request(280, -1)
            self.limit_send = Debounce(600, lambda: self.win.run_cmd(
                "set_battery_limit", "Charge limit %d%%" % self.limit.get_value(), value=int(self.limit.get_value())))
            self.limit.connect("value-changed", lambda *_: (not self.quiet) and self.limit_send())
            row.add_suffix(self.limit)
            grp.add(row)
        elif mode == "limiter":
            grp.set_description("Cap charging at 80 % to extend battery lifespan (Linuwu-Sense).")
            self.limiter = switch_row("Limit charging to 80 %", "Recommended if you're mostly plugged in",
                                      lambda on: (not self.quiet) and self.win.run_cmd(
                                          "set_battery_limit", "Charge limit %s" % ("80%" if on else "off"),
                                          value=80 if on else 100))
            grp.add(self.limiter)
        else:
            grp.set_description("The mainline driver doesn't expose a charge limit for this model yet. "
                                "Run 'sudo ./install.sh --with-rgb' to add the Linuwu-Sense driver, which brings an "
                                "80 % limiter that appears here automatically.")
        self.box.append(grp)

        tips = card()
        tips.append(label("TIPS", "section-title"))
        tips.append(label("• Use the Battery Saver preset (or auto-switching) when unplugged — Eco profile + no boost "
                          "roughly halves idle power on most Nitro models.\n"
                          "• The discrete GPU sleeps when unused; the dashboard shows its state. Apps that keep it "
                          "awake (e.g. browsers on the NVIDIA GPU) cost 5–15 W.\n"
                          "• Health below 80 % is normal wear after a few years.", wrap=True))
        self.box.append(tips)

    def update(self, st, si):
        b = st.get("battery")
        if not b:
            self.state_lbl.set_text("No battery detected")
            return
        cap = b.get("capacity")
        self.big.set_text("%s%%" % cap if cap is not None else "--")
        if cap is not None:
            self.level.set_value(cap)
        status = b.get("status") or ""
        self.state_lbl.set_text("%s · %s" % (status, "on AC" if st.get("ac_online") else "on battery"))
        eta = fmt_hours(b.get("time_left"))
        self.eta.set_text(("%s remaining" if status == "Discharging" else "%s to full") % eta if eta else "")
        self.t_health.value.set_text("%s %%" % b["health"] if b.get("health") else "--")
        self.t_health.sub.set_text("of design capacity")
        self.t_cycles.value.set_text(str(b.get("cycles")) if b.get("cycles") else "n/a")
        self.t_cycles.sub.set_text("reported by battery" if b.get("cycles") else "not reported by firmware")
        if b.get("full"):
            u = b["unit"]
            self.t_cap.value.set_text("%.2f %s" % (b["full"], u))
            self.t_cap.sub.set_text("design %.2f %s" % (b["design"], u) if b.get("design") else "")
        self.t_power.value.set_text("%.1f W" % b["power"] if b.get("power") is not None else "--")
        self.t_power.sub.set_text("charging" if status == "Charging" else "draw" if status == "Discharging" else status.lower())
        self.t_volt.value.set_text("%.2f V" % b["voltage"] if b.get("voltage") else "--")
        self.t_tech.value.set_text(b.get("technology") or "--")
        self.t_tech.sub.set_text(" ".join(x for x in (b.get("manufacturer"), b.get("model")) if x))
        daemon = bool(st.get("daemon"))
        if hasattr(self, "limit"):
            self.limit.set_sensitive(daemon)
            if b.get("limit") and not self.limit.has_focus():
                self.silently(self.limit.set_value, b["limit"])
        if hasattr(self, "limiter"):
            self.limiter.set_sensitive(daemon)
            on = (b.get("limit") or 100) < 100
            if self.limiter.switch.get_active() != on:
                self.silently(self.limiter.switch.set_active, on)


class PresetsPage(Page):
    title, icon = "Presets", "nc-presets-symbolic"
    subtitle = "One click to switch profile, fans, CPU tuning and lighting together"

    def __init__(self, win):
        super().__init__(win)
        self.grid = flow(3, 1)
        self.box.append(self.grid)
        self.section("Save current setup")
        save = card()
        row = Gtk.Box(spacing=10)
        self.name_entry = Gtk.Entry(placeholder_text="Preset name, e.g. Late-night coding", hexpand=True, max_length=32)
        self.name_entry.connect("activate", self._save)
        row.append(self.name_entry)
        btn = Gtk.Button(label="Save preset", css_classes=["suggested-action"])
        btn.connect("clicked", self._save)
        row.append(btn)
        save.append(row)
        save.append(label("Captures the current thermal profile, fan mode/curves, CPU boost, EPP and keyboard lighting.",
                          "dim", wrap=True))
        self.box.append(save)
        self.state = None
        self.active = None

    def _save(self, *_):
        name = self.name_entry.get_text().strip()
        if not name:
            self.win.toast("Give the preset a name first")
            return
        self.name_entry.set_text("")
        self.win.run_cmd("save_preset", "Preset '%s' saved" % name, refresh_state=True, name=name)

    def _rebuild(self):
        state = self.state
        child = self.grid.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.grid.remove(child)
            child = nxt
        items = [(n, p, True) for n, p in state["builtin_presets"].items()] + \
                [(n, p, False) for n, p in (state.get("presets") or {}).items()]
        for name, p, builtin in items:
            c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["preset-card"])
            if name == self.active:
                c.add_css_class("active")
            head = Gtk.Box(spacing=6)
            head.append(label(name, "preset-name"))
            head.append(Gtk.Box(hexpand=True))
            head.append(label("BUILT-IN" if builtin else "CUSTOM", "pill"))
            c.append(head)
            desc = label(p.get("desc", ""), "dim", wrap=True)
            desc.set_max_width_chars(28)
            c.append(desc)
            fan = (p.get("fan") or {}).get("mode", "—")
            prof = hwmod.PROFILE_LABELS.get(p.get("profile"), p.get("profile") or "—")
            boost = {True: "boost on", False: "boost off"}.get(p.get("cpu_boost"), "")
            c.append(label("⚡ %s   ❄ fans %s   %s" % (prof, fan, boost), "stat-label", wrap=True))
            acts = Gtk.Box(spacing=8, halign=Gtk.Align.END)
            if not builtin:
                d = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], tooltip_text="Delete preset")
                d.connect("clicked", lambda _b, n=name: self.win.run_cmd("delete_preset", "Deleted '%s'" % n,
                                                                         refresh_state=True, name=n))
                acts.append(d)
            a = Gtk.Button(label="Apply", css_classes=["suggested-action"])
            a.connect("clicked", lambda _b, n=name: self.win.run_cmd("apply_preset", "Preset '%s' applied" % n,
                                                                     refresh_state=True, name=n))
            a.set_sensitive(self.win.daemon_ok)
            acts.append(a)
            c.append(acts)
            self.grid.append(c)

    def on_state(self, state):
        self.state = state
        self._rebuild()

    def update(self, st, si):
        active = st.get("active_preset")
        if self.state and (active != self.active or getattr(self, "_daemon", None) != st.get("daemon")):
            self.active = active
            self._daemon = st.get("daemon")
            self._rebuild()


class SystemPage(Page):
    title, icon = "System", "nc-info-symbolic"
    subtitle = "Detected hardware, driver features and service status"

    def __init__(self, win):
        super().__init__(win)
        c = self.caps
        si = win.sysinfo
        dev = Adw.PreferencesGroup(title="Device")
        for k, v in (("Model", "%s %s" % (c["vendor"] or "", c["model"] or "")), ("BIOS", c["bios"]),
                     ("Board", c["board"]), ("Kernel", c["kernel"]), ("CPU", si.cpu_name)):
            dev.add(self._row(k, v))
        for i, g in enumerate(si.gpu_names):
            dev.add(self._row("GPU %d" % (i + 1), g))
        self.box.append(dev)

        feat = Adw.PreferencesGroup(title="Features",
                                    description="Detected at start-up from the kernel interfaces in /sys.")
        items = [
            ("Temperature sensors", bool(c["temps"]), ", ".join(c["temps"])),
            ("Fan speed monitoring", bool(c["fans"]), ", ".join(f["label"] for f in c["fans"])),
            ("Fan control", bool(c["fan_control"]), {"hwmon": "mainline acer_wmi (hwmon PWM)",
                                                     "linuwu": "Linuwu-Sense"}.get(c["fan_control"], "not available")),
            ("Thermal profiles", bool(c["profiles"]), ", ".join(hwmod.PROFILE_LABELS.get(p, p) for p in c["profiles"])),
            ("4-zone RGB keyboard", c["keyboard_rgb"], "Linuwu-Sense" if c["keyboard_rgb"] else "needs Linuwu-Sense"),
            ("Battery charge limit", bool(c["battery_limit"]), c["battery_limit"] or "not exposed by driver"),
            ("CPU boost control", c["cpu_boost"], ""),
            ("Energy preference (EPP)", bool(c["epp"]), ""),
            ("Discrete GPU power state", c["dgpu"], ""),
            ("Desktop power widget sync", c.get("ppd", False),
             "via power-profiles-daemon" if c.get("ppd") else "power-profiles-daemon not running"),
        ]
        for name, ok, detail in items:
            row = Adw.ActionRow(title=esc(name), subtitle=esc(detail))
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if ok else "window-close-symbolic")
            icon.add_css_class("success" if ok else "dim")
            row.add_suffix(icon)
            feat.add(row)
        self.box.append(feat)

        svc = Adw.PreferencesGroup(title="Service")
        self.svc_row = self._row("nitro-controld", "checking…")
        svc.add(self.svc_row)
        svc.add(self._row("Permissions", "Changing settings requires membership of wheel, sudo, admin or nitro "
                                         "(configurable). Everyone can read sensors."))
        self.box.append(svc)

        about = card()
        about.append(label("Nitro Control %s" % __version__, "preset-name"))
        about.append(label("Open-source NitroSense alternative for Linux. Not affiliated with Acer. "
                           "Uses only kernel interfaces — no EC hacking — so the firmware's own protections stay in charge.",
                           "dim", wrap=True))
        about.append(Gtk.LinkButton(uri=REPO_URL, label=REPO_URL, halign=Gtk.Align.START))
        self.box.append(about)

    def _row(self, title, value):
        row = Adw.ActionRow(title=esc(title), subtitle=esc(value or "—"))
        row.set_subtitle_selectable(True) if hasattr(row, "set_subtitle_selectable") else None
        return row

    def update(self, st, si):
        if st.get("daemon"):
            self.svc_row.set_subtitle(esc("running, version %s" % st.get("version")))
        else:
            self.svc_row.set_subtitle("not running — read-only. Start it: sudo systemctl enable --now nitro-controld")


PAGES = [DashboardPage, FansPage, PerformancePage, LightingPage, BatteryPage, PresetsPage, SystemPage]


# ---------------------------------------------------------------- the window
class Poller(threading.Thread):
    def __init__(self, win):
        super().__init__(daemon=True, name="poller")
        self.win = win
        self.client = Client(timeout=2.0)
        self.local = None
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.need_state = True

    def run(self):
        while not self.stop.is_set():
            st, state = None, None
            try:
                st = self.client.call("status")
                if self.need_state:
                    state = self.client.call("get_state")
                    self.need_state = False
            except (DaemonUnavailable, NitroError):
                if self.local is None:
                    self.local = hwmod.Hardware()
                st = self.local.snapshot()
                st["daemon"] = False
                self.need_state = True
            si = self.win.sysinfo.sample(st.get("dgpu_state"), st.get("ac_online"))
            GLib.idle_add(self.win.on_update, st, si, state)
            self.wake.wait(1.0)
            self.wake.clear()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Nitro Control")
        self.set_default_size(1240, 840)
        self.add_css_class("nitro")
        self.caps = hwmod.Hardware().capabilities()
        self.sysinfo = SysInfo()
        self.client = Client(timeout=5.0)
        self.daemon_ok = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        root = Gtk.Box()
        self.toasts.set_child(root)

        # sidebar
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["nitro-sidebar"])
        brand = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=22, margin_bottom=18, margin_start=22)
        b = Gtk.Label(xalign=0, css_classes=["brand"])
        b.set_markup("NITRO<span foreground='#ed1c34'>·</span>CTRL")
        brand.append(b)
        brand.append(label(self.caps["model"] or "Acer laptop", "brand-sub"))
        side.append(brand)
        self.nav = Gtk.ListBox(css_classes=["nav-list"], selection_mode=Gtk.SelectionMode.SINGLE)
        side.append(self.nav)
        side.append(Gtk.Box(vexpand=True))
        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=18, margin_end=18, margin_bottom=18)
        self.mini = label("", "brand-sub")
        foot.append(self.mini)
        self.pill = label("CONNECTING", "pill", xalign=0.5)
        self.pill.set_halign(Gtk.Align.START)
        foot.append(self.pill)
        side.append(foot)
        root.append(side)

        # content
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        header = Adw.HeaderBar()
        self.wtitle = Adw.WindowTitle(title="Dashboard", subtitle="Nitro Control")
        header.set_title_widget(self.wtitle)
        menu = Gio.Menu()
        menu.append("Reload service connection", "win.reconnect")
        menu.append("About Nitro Control", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        content.append(header)

        self.hot_banner = Gtk.Revealer()
        self.hot_banner.set_child(label("⚠  CRITICAL TEMPERATURE — fans forced to maximum until it cools down", "banner-hot"))
        content.append(self.hot_banner)
        self.off_banner = Gtk.Revealer()
        ob = Gtk.Box(spacing=10, css_classes=["banner-offline"])
        ob.append(label("Service not running — showing live readings only. Start it with:", "warn-text"))
        ob.append(label("sudo systemctl enable --now nitro-controld", "warn-text", selectable=True))
        self.off_banner.set_child(ob)
        content.append(self.off_banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150, vexpand=True)
        content.append(self.stack)
        root.append(content)

        self.pages = []
        for cls in PAGES:
            page = cls(self)
            self.pages.append(page)
            self.stack.add_named(page, cls.title)
            row = Gtk.ListBoxRow()
            rb = Gtk.Box(spacing=12)
            rb.append(Gtk.Image.new_from_icon_name(cls.icon))
            rb.append(label(cls.title, "nav-label"))
            row.set_child(rb)
            row.page_name = cls.title
            self.nav.append(row)
        self.nav.connect("row-selected", self._on_nav)
        self.nav.select_row(self.nav.get_row_at_index(0))

        act = Gio.SimpleAction.new("reconnect", None)
        act.connect("activate", lambda *_: self.refresh(state=True))
        self.add_action(act)
        for i in range(len(PAGES)):
            a = Gio.SimpleAction.new("page%d" % i, None)
            a.connect("activate", lambda *_a, idx=i: self.nav.select_row(self.nav.get_row_at_index(idx)))
            self.add_action(a)
            app.set_accels_for_action("win.page%d" % i, ["<Alt>%d" % (i + 1)])

        self.poller = Poller(self)
        self.poller.start()
        self.connect("close-request", self._on_close)

    def _on_nav(self, _lb, row):
        if row:
            self.stack.set_visible_child_name(row.page_name)
            self.wtitle.set_title(row.page_name)

    def _on_close(self, *_):
        self.poller.stop.set()
        self.poller.wake.set()
        return False

    def refresh(self, state=False):
        if state:
            self.poller.need_state = True
        self.poller.wake.set()

    def toast(self, text):
        t = Adw.Toast(title=GLib.markup_escape_text(text))
        t.set_timeout(3)
        self.toasts.add_toast(t)

    def on_update(self, st, si, state):
        self.daemon_ok = bool(st.get("daemon"))
        self.off_banner.set_reveal_child(not self.daemon_ok)
        self.hot_banner.set_reveal_child(bool(st.get("emergency")))
        for c in ("ok", "bad", "hot"):
            self.pill.remove_css_class(c)
        if st.get("emergency"):
            self.pill.set_text("THERMAL OVERRIDE")
            self.pill.add_css_class("hot")
        elif self.daemon_ok:
            self.pill.set_text("● SERVICE ONLINE")
            self.pill.add_css_class("ok")
        else:
            self.pill.set_text("● READ-ONLY")
            self.pill.add_css_class("bad")
        t = st.get("temps", {})
        prof = st.get("profile")
        self.mini.set_text("CPU %s°  GPU %s°  ·  %s" % (
            "%.0f" % t["cpu"] if "cpu" in t else "--", "%.0f" % t["gpu"] if "gpu" in t else "--",
            hwmod.PROFILE_LABELS.get(prof, prof or "--")))
        if state:
            for p in self.pages:
                p.on_state(state)
        for p in self.pages:
            try:
                p.update(st, si)
            except Exception as e:  # a broken page must not freeze the others
                print("page %s update failed: %s" % (p.title, e), file=sys.stderr)
        return False

    def run_task(self, fn, success=None, refresh_state=False):
        """Run fn(client) in a worker thread, then toast the result."""
        def work():
            err, state = None, None
            try:
                fn(self.client)
                if refresh_state:
                    state = self.client.call("get_state")
            except NitroError as e:
                err = str(e)
            GLib.idle_add(done, err, state)

        def done(err, state):
            if err:
                self.toast(err)
            elif success:
                self.toast(success)
            if state:
                for p in self.pages:
                    p.on_state(state)
            self.refresh()
            return False

        threading.Thread(target=work, daemon=True).start()

    def run_cmd(self, cmd, success=None, refresh_state=False, **args):
        self.run_task(lambda c: c.call(cmd, **args), success, refresh_state)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
                         if hasattr(Gio.ApplicationFlags, "DEFAULT_FLAGS") else Gio.ApplicationFlags.FLAGS_NONE)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(os.path.join(HERE, "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, fn, accels in (("quit", lambda *_: self.quit(), ["<Ctrl>q"]), ("about", self._about, [])):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", fn)
            self.add_action(a)
            if accels:
                self.set_accels_for_action("app." + name, accels)

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
        self.win.present()

    def _about(self, *_):
        kw = dict(application_name="Nitro Control", version=__version__, developer_name="thomasmartinoa",
                  license_type=Gtk.License.MIT_X11, website=REPO_URL, issue_url=REPO_URL + "/issues",
                  comments="Open-source NitroSense alternative for Acer Nitro & Predator laptops on Linux.")
        if hasattr(Adw, "AboutWindow"):
            dlg = Adw.AboutWindow(transient_for=self.win, application_icon=APP_ID, **kw)
        else:
            dlg = Gtk.AboutDialog(transient_for=self.win, program_name="Nitro Control", version=__version__,
                                  website=REPO_URL, license_type=Gtk.License.MIT_X11)
        dlg.present()


def main():
    return App().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
