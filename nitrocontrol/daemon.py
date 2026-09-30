"""nitro-controld: the small privileged service behind Nitro Control.

It is the only component that writes to hardware. It:
  * validates every request (whitelisted sysfs files, ranges, enum values),
  * only lets root and members of admin groups change settings (reads are open),
  * runs the fan curves with hysteresis, a safety floor and a critical-temperature
    override that hands the fans to full speed,
  * falls back to firmware automatic fan control if sensors stop responding, when
    it is stopped, or when it crashes (systemd ExecStopPost runs --restore-auto),
  * re-applies settings after suspend/resume and restores them at boot.
"""

import argparse
import copy
import grp
import json
import logging
import os
import pwd
import signal
import socket
import socketserver
import struct
import threading
import time
from collections import deque

from . import SOCKET_PATH, STATE_PATH, __version__
from . import fancurve
from . import hw as hwmod

log = logging.getLogger("nitro-controld")

BUILTIN_PRESETS = {
    "Silent": {
        "desc": "Quiet profile with a relaxed fan curve for browsing and work",
        "profile": "quiet", "cpu_boost": True,
        "fan": {"mode": "curve", "curves": {"cpu": fancurve.CURVES["silent"], "gpu": fancurve.CURVES["silent"]}},
    },
    "Balanced": {
        "desc": "Firmware defaults: balanced profile, automatic fans",
        "profile": "balanced", "cpu_boost": True, "fan": {"mode": "auto"},
    },
    "Gaming": {
        "desc": "Performance profile with an aggressive fan curve",
        "profile": "balanced-performance", "cpu_boost": True,
        "fan": {"mode": "curve", "curves": {"cpu": fancurve.CURVES["performance"],
                                            "gpu": fancurve.CURVES["performance"]}},
    },
    "Turbo": {
        "desc": "Everything unlocked, fans at maximum (AC power recommended)",
        "profile": "performance", "cpu_boost": True, "fan": {"mode": "max"},
    },
    "Battery Saver": {
        "desc": "Eco profile, CPU boost off, power-saving EPP",
        "profile": "low-power", "cpu_boost": False, "epp": "power", "fan": {"mode": "auto"},
    },
}

DEFAULT_STATE = {
    "version": 1,
    "fan_mode": "auto",
    "custom": {"cpu": 50, "gpu": 50},
    "curves": {"cpu": fancurve.CURVES["balanced"], "gpu": fancurve.CURVES["balanced"]},
    "curve_source": "own",
    "profile": None,
    "cpu_boost": None,
    "epp": None,
    "governor": None,
    "battery_limit": None,
    "keyboard": None,
    "kbd_led": None,
    "toggles": {},
    "auto_switch": {"enabled": False, "ac": "Balanced", "battery": "Battery Saver"},
    "safety": {"cpu_critical": 95, "gpu_critical": 90},
    "presets": {},
    "active_preset": None,
    "restore_on_start": True,
    "allowed_groups": ["wheel", "sudo", "admin", "nitro"],
}

SAFETY_LIMITS = {"cpu_critical": (75, 100), "gpu_critical": (70, 95)}
COOL_DOWN = 10          # °C below critical before leaving emergency mode
VERIFY_EVERY = 15       # s between read-backs of the firmware fan mode
SENSOR_FAIL_LIMIT = 3   # consecutive failed reads before giving the fans back to firmware


class RequestError(Exception):
    pass


def _boottime_gap():
    """Seconds spent suspended since boot (CLOCK_BOOTTIME counts suspend, MONOTONIC does not)."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        return 0.0


class Controller:
    def __init__(self, hw, state_path=STATE_PATH):
        self.hw = hw
        self.state_path = state_path
        self.lock = threading.RLock()
        self.state = self._load_state()
        self.cache = {}
        self.applied = {}
        self.emergency = False
        self.sensor_failures = 0
        self.events = deque(maxlen=40)
        self.last_ac = hw.ac_online()
        self.suspend_gap = _boottime_gap()
        self.last_verify = 0.0

    # ------------------------------------------------------------ state I/O
    def _load_state(self):
        state = copy.deepcopy(DEFAULT_STATE)
        try:
            with open(self.state_path) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for key in DEFAULT_STATE:
                    if key in saved and saved[key] is not None:
                        state[key] = saved[key]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable state file: %s", e)
        # Never trust the file blindly
        for fan in ("cpu", "gpu"):
            try:
                state["curves"][fan] = fancurve.validate_curve(state["curves"].get(fan))
            except (ValueError, TypeError, AttributeError):
                state["curves"][fan] = fancurve.CURVES["balanced"]
            try:
                state["custom"][fan] = hwmod.clamp_int(state["custom"].get(fan, 50), 0, 100)
            except (ValueError, TypeError, AttributeError):
                state["custom"] = dict(DEFAULT_STATE["custom"])
        if state["fan_mode"] not in ("auto", "max", "custom", "curve"):
            state["fan_mode"] = "auto"
        for key, (lo, hi) in SAFETY_LIMITS.items():
            try:
                state["safety"][key] = hwmod.clamp_int(state["safety"].get(key, DEFAULT_STATE["safety"][key]), lo, hi)
            except (ValueError, TypeError, AttributeError):
                state["safety"] = dict(DEFAULT_STATE["safety"])
        return state

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=2)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.error("could not save state: %s", e)

    def event(self, msg, level="info"):
        getattr(log, "warning" if level == "warn" else level)(msg)
        self.events.append({"t": time.time(), "msg": msg, "level": level})

    # -------------------------------------------------------------- restore
    def restore(self):
        """Re-apply the remembered settings (boot, resume)."""
        s, hw = self.state, self.hw
        steps = [
            ("profile", lambda v: hw.set_profile(hwmod.closest_profile(v, hw.profile_choices()))),
            ("cpu_boost", hw.set_cpu_boost),
            ("epp", hw.set_epp),
            ("governor", hw.set_governor),
            ("battery_limit", hw.set_battery_limit),
            ("keyboard", self._write_keyboard),
            ("kbd_led", hw.set_kbd_led_brightness),
        ]
        for key, fn in steps:
            if s.get(key) is not None:
                try:
                    fn(s[key])
                except (OSError, ValueError, TypeError) as e:
                    log.warning("restore %s failed: %s", key, e)
        for name, value in (s.get("toggles") or {}).items():
            try:
                hw.set_toggle(name, value)
            except (OSError, ValueError) as e:
                log.warning("restore %s failed: %s", name, e)
        self._apply_fan_mode()

    # ------------------------------------------------------------- fan logic
    def _fan_temp(self, key, temps):
        if self.state["curve_source"] == "max":
            vals = [temps[k] for k in ("cpu", "gpu") if k in temps]
            return max(vals) if vals else None
        return temps.get(key, temps.get("cpu"))

    def _apply_fan_mode(self, temps=None):
        hw = self.hw
        if not hw.fan_backend:
            return
        mode = self.state["fan_mode"]
        self.applied = {}
        self.emergency = False
        try:
            if mode == "auto":
                hw.fan_set_auto()
            elif mode == "max":
                hw.fan_set_max()
            else:
                temps = temps if temps is not None else hw.read_temps()
                self._manual_step(temps, switch_mode=True)
        except OSError as e:
            self.event("Fan command rejected by firmware (%s), returning fans to automatic" % e, "warn")
            self._firmware_auto()

    def _firmware_auto(self):
        self.state["fan_mode"] = "auto"
        self.applied = {}
        try:
            self.hw.fan_set_auto()
        except OSError as e:
            log.error("could not restore automatic fan control: %s", e)

    def _manual_step(self, temps, switch_mode=False):
        targets = {}
        for f in self.hw.fans:
            t = self._fan_temp(f.key, temps)
            if self.state["fan_mode"] == "curve":
                target = fancurve.next_speed(self.state["curves"][f.key], t, self.applied.get(f.key))
            else:
                target = max(self.state["custom"].get(f.key, 50), fancurve.safety_floor(t))
            targets[f.key] = target
        if switch_mode or targets != self.applied:
            changed = targets if switch_mode else {k: v for k, v in targets.items() if self.applied.get(k) != v}
            self.hw.fan_set_speeds(changed, switch_mode=switch_mode)
            self.applied.update(changed)

    def tick(self):
        with self.lock:
            hw, s = self.hw, self.state
            temps = hw.read_temps()
            fans = hw.read_fans(with_pwm=False)

            gap = _boottime_gap()
            if gap - self.suspend_gap > 3:
                self.suspend_gap = gap
                self.event("Resumed from suspend, re-applying settings")
                self.restore()

            ac = hw.ac_online()
            if ac is not None and self.last_ac is not None and ac != self.last_ac:
                self.event("Switched to %s power" % ("AC" if ac else "battery"))
                auto = s["auto_switch"]
                if auto.get("enabled"):
                    name = auto.get("ac" if ac else "battery")
                    if name:
                        try:
                            self.apply_preset(name)
                        except (RequestError, OSError, ValueError) as e:
                            self.event("Auto-switch to %s failed: %s" % (name, e), "warn")
            self.last_ac = ac

            if s["fan_mode"] in ("custom", "curve") and hw.fan_backend:
                self._guard_manual(temps)

            if s["fan_mode"] != "auto" and time.monotonic() - self.last_verify > VERIFY_EVERY:
                self.last_verify = time.monotonic()
                expected = "max" if (s["fan_mode"] == "max" or self.emergency) else "custom"
                actual = hw.fan_mode()
                if actual and actual != expected:
                    self.event("Firmware changed the fan mode to %s, re-applying %s" % (actual, s["fan_mode"]))
                    self._apply_fan_mode(temps)

            for key, pct in self.applied.items():
                if key in fans:
                    fans[key]["pct"] = pct
            self.cache = {
                "temps": temps,
                "fans": fans,
                "profile": hw.profile(),
                "cpu_boost": hw.cpu_boost(),
                "epp": hw.epp(),
                "governor": hw.governor(),
                "battery": hw.battery_info(),
                "ac_online": ac,
                "keyboard": hw.kb_read(),
                "kbd_led": hw.kbd_led_brightness(),
                "toggles": hw.toggles(),
                "dgpu_state": hw.dgpu_state(),
            }

    def _guard_manual(self, temps):
        s, hw = self.state, self.hw
        needed = [self._fan_temp(f.key, temps) for f in hw.fans]
        if any(t is None for t in needed):
            self.sensor_failures += 1
            if self.sensor_failures >= SENSOR_FAIL_LIMIT:
                self.event("Temperature sensors not responding, fans handed back to firmware", "warn")
                self._firmware_auto()
                self.save()
            return
        self.sensor_failures = 0

        cpu, gpu = temps.get("cpu"), temps.get("gpu")
        crit = s["safety"]
        hot = (cpu is not None and cpu >= crit["cpu_critical"]) or (gpu is not None and gpu >= crit["gpu_critical"])
        if hot and not self.emergency:
            self.emergency = True
            self.applied = {}
            self.event("Critical temperature (CPU %s °C, GPU %s °C): fans forced to maximum" % (cpu, gpu), "warn")
            try:
                hw.fan_set_max()
            except OSError as e:
                log.error("could not force max fans: %s", e)
            return
        if self.emergency:
            cooled = (cpu is None or cpu < crit["cpu_critical"] - COOL_DOWN) and \
                     (gpu is None or gpu < crit["gpu_critical"] - COOL_DOWN)
            if cooled:
                self.event("Temperatures back to normal, resuming %s fan mode" % s["fan_mode"])
                self._apply_fan_mode(temps)
            return
        try:
            self._manual_step(temps)
        except OSError as e:
            self.event("Fan write failed (%s), returning fans to automatic" % e, "warn")
            self._firmware_auto()

    # ------------------------------------------------------------- commands
    def status(self):
        with self.lock:
            out = dict(self.cache)
            out.update({
                "daemon": True,
                "version": __version__,
                "fan_mode": self.state["fan_mode"],
                "emergency": self.emergency,
                "custom": dict(self.state["custom"]),
                "active_preset": self.state.get("active_preset"),
                "events": list(self.events)[-12:],
            })
            return out

    def get_state(self):
        with self.lock:
            out = copy.deepcopy(self.state)
            out["builtin_presets"] = BUILTIN_PRESETS
            out["builtin_curves"] = fancurve.CURVES
            out["safety_limits"] = SAFETY_LIMITS
            return out

    def set_fan(self, mode, cpu=None, gpu=None):
        if mode not in ("auto", "max", "custom", "curve"):
            raise RequestError("fan mode must be auto, max, custom or curve")
        if not self.hw.fan_backend:
            raise RequestError("fan control is not available on this system")
        for key, val in (("cpu", cpu), ("gpu", gpu)):
            if val is not None:
                self.state["custom"][key] = hwmod.clamp_int(val, 0, 100)
        if mode == self.state["fan_mode"] == "custom" and not self.emergency:
            self.state["active_preset"] = None
            self._manual_step(self.hw.read_temps())
        else:
            self.state["fan_mode"] = mode
            self.state["active_preset"] = None
            self._apply_fan_mode()
        self.save()

    def set_curve(self, fan, points):
        curve = fancurve.validate_curve(points)
        keys = ("cpu", "gpu") if fan == "both" else (fan,)
        for k in keys:
            if k not in ("cpu", "gpu"):
                raise RequestError("fan must be cpu, gpu or both")
            self.state["curves"][k] = [list(p) for p in curve]
        self.save()

    def set_curve_source(self, source):
        if source not in ("own", "max"):
            raise RequestError("source must be own or max")
        self.state["curve_source"] = source
        self.save()

    def set_profile(self, profile):
        if profile == "next":
            choices = self.hw.profile_choices()
            cur = self.hw.profile()
            profile = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
        self.hw.set_profile(profile)
        self.state["profile"] = profile
        self.state["active_preset"] = None
        self.save()
        return profile

    def set_boost(self, enabled):
        self.hw.set_cpu_boost(bool(enabled))
        self.state["cpu_boost"] = bool(enabled)
        self.save()

    def set_epp(self, value):
        self.hw.set_epp(value)
        self.state["epp"] = value
        self.save()

    def set_governor(self, value):
        self.hw.set_governor(value)
        self.state["governor"] = value
        self.save()

    def set_battery_limit(self, value):
        self.hw.set_battery_limit(value)
        self.state["battery_limit"] = int(value)
        self.save()

    def _write_keyboard(self, kb):
        mode = kb.get("mode")
        if mode == "zones":
            self.hw.kb_set_zones(kb["colors"], kb.get("brightness", 100))
        elif mode == "effect":
            self.hw.kb_set_effect(kb.get("effect", 0), kb.get("speed", 4), kb.get("brightness", 100),
                                  kb.get("direction", 2), kb.get("color", "ff0000"))
        else:
            raise RequestError("keyboard mode must be zones or effect")

    def set_keyboard(self, **kb):
        self._write_keyboard(kb)
        keep = {"mode", "colors", "brightness", "effect", "speed", "direction", "color"}
        self.state["keyboard"] = {k: v for k, v in kb.items() if k in keep}
        self.save()

    def set_kbd_led(self, value):
        self.hw.set_kbd_led_brightness(value)
        self.state["kbd_led"] = int(value)
        self.save()

    def set_toggle(self, name, value):
        self.hw.set_toggle(name, value)
        self.state.setdefault("toggles", {})[name] = int(value)
        self.save()

    def set_safety(self, cpu_critical=None, gpu_critical=None):
        for key, val in (("cpu_critical", cpu_critical), ("gpu_critical", gpu_critical)):
            if val is not None:
                lo, hi = SAFETY_LIMITS[key]
                if not lo <= int(val) <= hi:
                    raise RequestError("%s must be between %d and %d °C" % (key, lo, hi))
                self.state["safety"][key] = int(val)
        self.save()

    def set_auto_switch(self, enabled=None, ac=None, battery=None):
        a = self.state["auto_switch"]
        for key, val in (("ac", ac), ("battery", battery)):
            if val is not None:
                if val and val not in self._all_presets():
                    raise RequestError("unknown preset %r" % val)
                a[key] = val or None
        if enabled is not None:
            a["enabled"] = bool(enabled)
        self.save()

    def _all_presets(self):
        merged = dict(BUILTIN_PRESETS)
        merged.update(self.state.get("presets") or {})
        return merged

    def apply_preset(self, name):
        preset = self._all_presets().get(name)
        if preset is None:
            raise RequestError("unknown preset %r" % name)
        hw, notes = self.hw, []
        if preset.get("profile") and hw.profile_choices():
            prof = hwmod.closest_profile(preset["profile"], hw.profile_choices())
            if prof:
                hw.set_profile(prof)
                self.state["profile"] = prof
        if preset.get("cpu_boost") is not None and hw.boost_path:
            hw.set_cpu_boost(preset["cpu_boost"])
            self.state["cpu_boost"] = bool(preset["cpu_boost"])
        if preset.get("epp"):
            if preset["epp"] in hw.epp_choices():
                hw.set_epp(preset["epp"])
                self.state["epp"] = preset["epp"]
        elif preset.get("cpu_boost") is not None and self.state.get("epp") == "power" and "balance_performance" in hw.epp_choices():
            hw.set_epp("balance_performance")
            self.state["epp"] = "balance_performance"
        fan = preset.get("fan") or {}
        if fan and hw.fan_backend:
            for k, curve in (fan.get("curves") or {}).items():
                if k in ("cpu", "gpu"):
                    self.state["curves"][k] = fancurve.validate_curve(curve)
            for k, v in (fan.get("custom") or {}).items():
                if k in ("cpu", "gpu"):
                    self.state["custom"][k] = hwmod.clamp_int(v, 0, 100)
            if fan.get("mode") in ("auto", "max", "custom", "curve"):
                self.state["fan_mode"] = fan["mode"]
                self._apply_fan_mode()
        if preset.get("keyboard") and hw.kb4:
            try:
                self.set_keyboard(**preset["keyboard"])
            except (ValueError, KeyError, OSError) as e:
                notes.append("keyboard: %s" % e)
        self.state["active_preset"] = name
        self.save()
        self.event("Preset '%s' applied" % name)
        return {"notes": notes}

    def save_preset(self, name, desc=""):
        name = str(name).strip()
        if not 1 <= len(name) <= 32:
            raise RequestError("preset name must be 1..32 characters")
        if name in BUILTIN_PRESETS:
            raise RequestError("'%s' is a built-in preset, pick another name" % name)
        presets = self.state.setdefault("presets", {})
        if name not in presets and len(presets) >= 20:
            raise RequestError("too many presets (max 20)")
        s = self.state
        presets[name] = {
            "desc": str(desc)[:120] or "Custom preset",
            "profile": self.hw.profile(),
            "cpu_boost": self.hw.cpu_boost(),
            "epp": self.hw.epp(),
            "fan": {"mode": s["fan_mode"], "custom": dict(s["custom"]),
                    "curves": copy.deepcopy(s["curves"])},
            "keyboard": copy.deepcopy(s.get("keyboard")),
        }
        s["active_preset"] = name
        self.save()

    def delete_preset(self, name):
        if name not in (self.state.get("presets") or {}):
            raise RequestError("no user preset called %r" % name)
        del self.state["presets"][name]
        for key in ("ac", "battery"):
            if self.state["auto_switch"].get(key) == name:
                self.state["auto_switch"][key] = None
        self.save()

    def shutdown(self):
        with self.lock:
            if self.hw.fan_backend and (self.state["fan_mode"] != "auto" or self.emergency):
                log.info("handing fans back to firmware control")
                try:
                    self.hw.fan_set_auto()
                except OSError as e:
                    log.error("could not restore auto fans: %s", e)
            self.save()


# Commands: name -> (method name, needs write permission)
COMMANDS = {
    "ping": (None, False),
    "status": ("status", False),
    "capabilities": (None, False),
    "get_state": ("get_state", False),
    "set_fan": ("set_fan", True),
    "set_curve": ("set_curve", True),
    "set_curve_source": ("set_curve_source", True),
    "set_profile": ("set_profile", True),
    "set_boost": ("set_boost", True),
    "set_epp": ("set_epp", True),
    "set_governor": ("set_governor", True),
    "set_battery_limit": ("set_battery_limit", True),
    "set_keyboard": ("set_keyboard", True),
    "set_kbd_led": ("set_kbd_led", True),
    "set_toggle": ("set_toggle", True),
    "set_safety": ("set_safety", True),
    "set_auto_switch": ("set_auto_switch", True),
    "apply_preset": ("apply_preset", True),
    "save_preset": ("save_preset", True),
    "delete_preset": ("delete_preset", True),
}


def peer_credentials(sock):
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", data)


def uid_allowed(uid, groups):
    if uid == 0:
        return True
    try:
        pw = pwd.getpwuid(uid)
        member_of = set(os.getgrouplist(pw.pw_name, pw.pw_gid))
    except (KeyError, OSError):
        return False
    for name in groups:
        try:
            if grp.getgrnam(name).gr_gid in member_of:
                return True
        except KeyError:
            continue
    return False


def dispatch(ctl, request, may_write):
    if not isinstance(request, dict) or request.get("cmd") not in COMMANDS:
        raise RequestError("unknown command")
    cmd = request["cmd"]
    args = request.get("args") or {}
    if not isinstance(args, dict):
        raise RequestError("args must be an object")
    method, needs_write = COMMANDS[cmd]
    if needs_write and not may_write:
        raise RequestError("permission denied: you must be root or in one of the groups %s"
                           % ", ".join(ctl.state["allowed_groups"]))
    if cmd == "ping":
        return {"version": __version__}
    if cmd == "capabilities":
        return ctl.hw.capabilities()
    with ctl.lock:
        try:
            return getattr(ctl, method)(**args)
        except TypeError as e:
            raise RequestError("bad arguments: %s" % e)


class Handler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self):
        try:
            _pid, uid, _gid = peer_credentials(self.request)
        except OSError:
            return
        may_write = uid_allowed(uid, self.server.ctl.state["allowed_groups"])
        while True:
            try:
                line = self.rfile.readline(65536)
            except OSError:  # idle client timed out or went away
                break
            if not line:
                break
            try:
                req = json.loads(line)
                data = dispatch(self.server.ctl, req, may_write)
                if COMMANDS[req["cmd"]][1]:
                    log.info("uid %d: %s %s", uid, req.get("cmd"), json.dumps(req.get("args") or {})[:200])
                reply = {"ok": True, "data": data}
            except (RequestError, ValueError, PermissionError) as e:
                reply = {"ok": False, "error": str(e)}
            except OSError as e:
                reply = {"ok": False, "error": "hardware rejected the request: %s" % e}
            except Exception as e:  # never let a bad request kill the service
                log.exception("request failed")
                reply = {"ok": False, "error": "internal error: %s" % e}
            self.wfile.write((json.dumps(reply) + "\n").encode())


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def restore_auto():
    hw = hwmod.Hardware()
    if hw.fan_backend:
        hw.fan_set_auto()
        print("fans returned to automatic control")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nitro-controld", description="Nitro Control privileged service")
    ap.add_argument("--socket", default=SOCKET_PATH)
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--interval", type=float, default=1.0, help="control loop period in seconds (0.5..5)")
    ap.add_argument("--restore-auto", action="store_true", help="give the fans back to firmware and exit")
    ap.add_argument("--no-restore", action="store_true", help="do not re-apply saved settings at start")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if os.geteuid() != 0 and not hwmod.ROOT:
        ap.error("must run as root (it is normally started by systemd)")
    if args.restore_auto:
        restore_auto()
        return 0

    hw = hwmod.Hardware()
    caps = hw.capabilities()
    log.info("Nitro Control %s on %s %s (fans: %s, profiles: %s, rgb: %s)", __version__,
             caps["vendor"], caps["model"], caps["fan_control"], ",".join(caps["profiles"]) or "none",
             caps["keyboard_rgb"])

    ctl = Controller(hw, args.state)
    if ctl.state.get("restore_on_start", True) and not args.no_restore:
        ctl.restore()
    ctl.tick()

    sock_dir = os.path.dirname(args.socket)
    if sock_dir and not os.path.isdir(sock_dir):
        os.makedirs(sock_dir, mode=0o755)
    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass
    server = Server(args.socket, Handler)
    server.ctl = ctl
    os.chmod(args.socket, 0o666)  # anyone may read; writes are checked per peer uid
    threading.Thread(target=server.serve_forever, name="socket", daemon=True).start()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    interval = min(5.0, max(0.5, args.interval))
    try:
        while not stop.is_set():
            try:
                ctl.tick()
            except Exception:
                log.exception("control loop error")
                with ctl.lock:
                    if ctl.state["fan_mode"] in ("custom", "curve"):
                        ctl._firmware_auto()
            stop.wait(interval)
    finally:
        server.shutdown()
        server.server_close()
        ctl.shutdown()
        try:
            os.unlink(args.socket)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
