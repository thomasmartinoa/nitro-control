"""Hardware discovery and access.

Everything in here goes through documented kernel interfaces in /sys. There are
no raw embedded-controller writes and no direct ACPI method calls: the kernel
driver (acer_wmi, or optionally linuwu_sense) validates every request before it
reaches the firmware, and the firmware keeps its own thermal protection active.

Setting NITRO_SYSFS_ROOT points every path at a fake tree, which the tests use.
"""

import glob
import os
import re
import shutil
import subprocess

ROOT = os.environ.get("NITRO_SYSFS_ROOT", "").rstrip("/")

# hwmon pwmN_enable values used by acer_wmi
PWM_ENABLE_MAX = 0
PWM_ENABLE_MANUAL = 1
PWM_ENABLE_AUTO = 2

# Friendly names for the ACPI platform profiles, matching NitroSense's modes on
# acer_wmi (performance = Turbo, balanced-performance = Performance, ...).
PROFILE_LABELS = {
    "low-power": "Eco",
    "cool": "Cool",
    "quiet": "Quiet",
    "balanced": "Balanced",
    "balanced-performance": "Performance",
    "performance": "Turbo",
    "custom": "Custom",
}
PROFILE_ORDER = ["low-power", "cool", "quiet", "balanced", "balanced-performance", "performance"]

KB_EFFECTS = ["Static", "Breathing", "Neon", "Wave", "Shifting", "Zoom", "Meteor", "Twinkling"]

# Optional linuwu_sense extras: attribute -> (label, description, allowed values)
LINUWU_TOGGLES = {
    "backlight_timeout": ("Keyboard light timeout", "Turn the RGB off after 30 s idle", (0, 1)),
    "boot_animation_sound": ("Boot animation & sound", "Acer logo animation and chime at power-on", (0, 1)),
    "lcd_override": ("LCD overdrive", "Lower panel response time, less ghosting", (0, 1)),
    "usb_charging": ("Power-off USB charging", "Keep USB powered while off, until battery drops to N%", (0, 10, 20, 30)),
}

LINUWU_BASES = (
    "/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi",
    "/sys/devices/platform/acer-wmi",
)


def p(path):
    return ROOT + path


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return default


def read_int(path, default=None):
    value = read(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def write(path, value):
    sys_root = os.path.realpath(ROOT + "/sys")
    if not path or not os.path.realpath(path).startswith(sys_root + "/"):
        raise PermissionError("refusing to write outside /sys: %r" % path)
    with open(path, "w") as f:
        f.write(str(value))


def _exists(path):
    return bool(path) and os.path.exists(path)


def find_hwmon(*names):
    for d in sorted(glob.glob(p("/sys/class/hwmon/hwmon*"))):
        if read(d + "/name") in names:
            return d
    return None


class Fan:
    def __init__(self, key, label, rpm, pwm=None, enable=None):
        self.key = key
        self.label = label
        self.rpm = rpm
        self.pwm = pwm if _exists(pwm) else None
        self.enable = enable if _exists(enable) else None


class Hardware:
    """Detects what this machine exposes and offers safe, validated accessors."""

    def __init__(self):
        self.detect()

    # ------------------------------------------------------------------ detect
    def detect(self):
        self.acer = find_hwmon("acer")
        self.fans = []
        self.temps = {}
        if self.acer:
            for idx, key, label in ((1, "cpu", "CPU"), (2, "gpu", "GPU")):
                rpm = "%s/fan%d_input" % (self.acer, idx)
                if _exists(rpm):
                    self.fans.append(Fan(key, label, rpm,
                                         "%s/pwm%d" % (self.acer, idx),
                                         "%s/pwm%d_enable" % (self.acer, idx)))
            for idx, key in ((1, "cpu"), (2, "gpu"), (3, "sys")):
                t = "%s/temp%d_input" % (self.acer, idx)
                if _exists(t):
                    self.temps[key] = t

        # CPU package sensor as a fallback / extra reading
        core = find_hwmon("coretemp", "k10temp", "zenpower")
        self.cpu_pkg_temp = None
        if core:
            for label_file in sorted(glob.glob(core + "/temp*_label")):
                if read(label_file) in ("Package id 0", "Tctl", "Tdie"):
                    self.cpu_pkg_temp = label_file.replace("_label", "_input")
                    break
            if not self.cpu_pkg_temp and _exists(core + "/temp1_input"):
                self.cpu_pkg_temp = core + "/temp1_input"
        if "cpu" not in self.temps and self.cpu_pkg_temp:
            self.temps["cpu"] = self.cpu_pkg_temp

        # Platform profile: prefer the per-driver class device (kernel >= 6.14)
        self.profile_path = self.profile_choices_path = None
        for d in sorted(glob.glob(p("/sys/class/platform-profile/platform-profile-*"))):
            if read(d + "/name") == "acer-wmi":
                self.profile_path, self.profile_choices_path = d + "/profile", d + "/choices"
                break
        if not self.profile_path and _exists(p("/sys/firmware/acpi/platform_profile")):
            self.profile_path = p("/sys/firmware/acpi/platform_profile")
            self.profile_choices_path = p("/sys/firmware/acpi/platform_profile_choices")

        # Optional out-of-tree linuwu_sense driver (4-zone RGB, battery limiter, ...)
        self.linuwu = self.kb4 = None
        for base in LINUWU_BASES:
            for sub in ("nitro_sense", "predator_sense"):
                if not self.linuwu and os.path.isdir(p(base + "/" + sub)):
                    self.linuwu = p(base + "/" + sub)
            if not self.kb4 and os.path.isdir(p(base + "/four_zoned_kb")):
                self.kb4 = p(base + "/four_zoned_kb")

        # Linuwu fan control only when mainline hwmon pwm is missing
        self.fan_backend = None
        if self.fans and all(f.pwm and f.enable for f in self.fans):
            self.fan_backend = "hwmon"
        elif self.linuwu and _exists(self.linuwu + "/fan_speed"):
            self.fan_backend = "linuwu"
            if not self.fans:
                self.fans = [Fan("cpu", "CPU", None), Fan("gpu", "GPU", None)]

        # Power supplies
        self.battery = self.ac = None
        for d in sorted(glob.glob(p("/sys/class/power_supply/*"))):
            kind = read(d + "/type")
            if kind == "Battery" and not self.battery and read(d + "/scope") != "Device":
                self.battery = d
            elif kind == "Mains" and not self.ac:
                self.ac = d
        self.charge_threshold = None
        if self.battery and _exists(self.battery + "/charge_control_end_threshold"):
            self.charge_threshold = self.battery + "/charge_control_end_threshold"

        # Generic keyboard backlight LED (non-RGB models)
        leds = glob.glob(p("/sys/class/leds/*kbd_backlight*"))
        self.kbd_led = sorted(leds)[0] if leds else None

        # CPU boost / EPP / governor
        self.boost_path, self.boost_inverted = None, False
        if _exists(p("/sys/devices/system/cpu/intel_pstate/no_turbo")):
            self.boost_path, self.boost_inverted = p("/sys/devices/system/cpu/intel_pstate/no_turbo"), True
        elif _exists(p("/sys/devices/system/cpu/cpufreq/boost")):
            self.boost_path = p("/sys/devices/system/cpu/cpufreq/boost")
        self.policies = sorted(glob.glob(p("/sys/devices/system/cpu/cpufreq/policy*")))

        # Discrete NVIDIA/AMD GPU for runtime power state (read-only)
        self.dgpu = None
        for d in sorted(glob.glob(p("/sys/bus/pci/devices/*"))):
            cls = read(d + "/class", "")
            if cls.startswith("0x03") and read(d + "/vendor") in ("0x10de", "0x1002") and d.endswith(":01:00.0"):
                self.dgpu = d
                break
        if not self.dgpu:
            for d in sorted(glob.glob(p("/sys/bus/pci/devices/*"))):
                if read(d + "/class", "").startswith("0x03") and read(d + "/vendor") == "0x10de":
                    self.dgpu = d
                    break

    # ----------------------------------------------------------- capabilities
    def capabilities(self):
        return {
            "model": dmi("product_name"),
            "vendor": dmi("sys_vendor"),
            "bios": dmi("bios_version"),
            "board": dmi("board_name"),
            "kernel": os.uname().release,
            "fans": [{"key": f.key, "label": f.label} for f in self.fans],
            "fan_control": self.fan_backend,
            "temps": sorted(self.temps),
            "profiles": self.profile_choices(),
            "keyboard_rgb": bool(self.kb4),
            "keyboard_led": bool(self.kbd_led),
            "keyboard_led_max": read_int(self.kbd_led + "/max_brightness") if self.kbd_led else None,
            "linuwu": bool(self.linuwu),
            "toggles": {k: {"label": v[0], "desc": v[1], "values": list(v[2])}
                        for k, v in LINUWU_TOGGLES.items()
                        if self.linuwu and _exists(self.linuwu + "/" + k)},
            "battery_limit": "threshold" if self.charge_threshold else
                             ("limiter" if self.linuwu and _exists(self.linuwu + "/battery_limiter") else None),
            "cpu_boost": bool(self.boost_path),
            "epp": self.epp_choices(),
            "governors": self.governor_choices(),
            "dgpu": bool(self.dgpu),
            "battery": bool(self.battery),
            "ppd": ppd_active(),
        }

    # ---------------------------------------------------------------- sensors
    def read_temps(self):
        out = {}
        for key, path in self.temps.items():
            v = read_int(path)
            if v is not None and 0 < v < 150000:
                out[key] = v / 1000.0
        if self.cpu_pkg_temp and self.temps.get("cpu") != self.cpu_pkg_temp:
            v = read_int(self.cpu_pkg_temp)
            if v:
                out["cpu_pkg"] = v / 1000.0
        return out

    def read_fans(self, with_pwm=True):
        out = {}
        for f in self.fans:
            entry = {"rpm": read_int(f.rpm) if f.rpm else None}
            if with_pwm and f.pwm:
                raw = read_int(f.pwm)
                entry["pct"] = round(raw * 100 / 255) if raw is not None else None
            out[f.key] = entry
        return out

    def fan_mode(self):
        """Current firmware fan mode: auto / max / custom, or None if unknown."""
        if self.fan_backend == "hwmon":
            modes = set()
            for f in self.fans:
                v = read_int(f.enable)
                # acer_wmi reports ENXIO before the firmware has been told any mode:
                # that is the power-on automatic behaviour.
                modes.add({PWM_ENABLE_MAX: "max", PWM_ENABLE_MANUAL: "custom"}.get(v, "auto"))
            return modes.pop() if len(modes) == 1 else "custom"
        if self.fan_backend == "linuwu":
            vals = (read(self.linuwu + "/fan_speed") or "").split(",")
            if all(v.strip() == "0" for v in vals):
                return "auto"
            if all(v.strip() == "100" for v in vals):
                return "max"
            return "custom"
        return None

    # ------------------------------------------------------------ fan control
    def fan_set_auto(self):
        if self.fan_backend == "hwmon":
            for f in self.fans:
                write(f.enable, PWM_ENABLE_AUTO)
        elif self.fan_backend == "linuwu":
            write(self.linuwu + "/fan_speed", "0,0")

    def fan_set_max(self):
        if self.fan_backend == "hwmon":
            for f in self.fans:
                write(f.enable, PWM_ENABLE_MAX)
        elif self.fan_backend == "linuwu":
            write(self.linuwu + "/fan_speed", "100,100")

    def fan_set_speeds(self, speeds, switch_mode=True):
        """speeds: {"cpu": pct, "gpu": pct}. Values are clamped to 0..100."""
        speeds = {k: max(0, min(100, int(round(v)))) for k, v in speeds.items()}
        if self.fan_backend == "hwmon":
            for f in self.fans:
                if f.key not in speeds:
                    continue
                raw = int(round(speeds[f.key] * 255 / 100))
                if switch_mode:
                    # Pre-load the duty cycle so the fan never dips when entering manual mode
                    try:
                        write(f.pwm, raw)
                    except OSError:
                        pass
                    write(f.enable, PWM_ENABLE_MANUAL)
                write(f.pwm, raw)
        elif self.fan_backend == "linuwu":
            cur = (read(self.linuwu + "/fan_speed") or "0,0").split(",")
            cpu = speeds.get("cpu", int(cur[0]) if cur[0].strip().isdigit() else 50)
            gpu = speeds.get("gpu", int(cur[-1]) if cur[-1].strip().isdigit() else 50)
            # 0 means "auto" for linuwu, so the manual minimum is 1 %
            write(self.linuwu + "/fan_speed", "%d,%d" % (max(1, cpu), max(1, gpu)))

    # --------------------------------------------------------------- profiles
    def profile_choices(self):
        raw = read(self.profile_choices_path, "") if self.profile_choices_path else ""
        choices = raw.split()
        return sorted(choices, key=lambda c: PROFILE_ORDER.index(c) if c in PROFILE_ORDER else 99)

    def profile(self):
        return read(self.profile_path) if self.profile_path else None

    def set_profile(self, name):
        choices = self.profile_choices()
        if name not in choices:
            raise ValueError("unsupported profile %r" % name)
        # With power-profiles-daemon running, go through it so the desktop's power
        # widget stays in sync (it also sets the matching CPU EPP). Fall back to
        # writing sysfs directly if it can't do this particular profile.
        ppd = ppd_profile_for(name, choices)
        if ppd and ppd_set(ppd) and self.profile() == name:
            return
        write(self.profile_path, name)

    # -------------------------------------------------------------- CPU tune
    def cpu_boost(self):
        v = read_int(self.boost_path) if self.boost_path else None
        if v is None:
            return None
        return (v == 0) if self.boost_inverted else (v == 1)

    def set_cpu_boost(self, enabled):
        if not self.boost_path:
            raise ValueError("CPU boost control not available")
        on = bool(enabled)
        write(self.boost_path, int(not on) if self.boost_inverted else int(on))

    def epp_choices(self):
        if not self.policies:
            return []
        return (read(self.policies[0] + "/energy_performance_available_preferences") or "").split()

    def epp(self):
        return read(self.policies[0] + "/energy_performance_preference") if self.policies else None

    def set_epp(self, value):
        if value not in self.epp_choices():
            raise ValueError("unsupported EPP %r" % value)
        for pol in self.policies:
            write(pol + "/energy_performance_preference", value)

    def governor_choices(self):
        if not self.policies:
            return []
        return (read(self.policies[0] + "/scaling_available_governors") or "").split()

    def governor(self):
        return read(self.policies[0] + "/scaling_governor") if self.policies else None

    def set_governor(self, value):
        if value not in self.governor_choices():
            raise ValueError("unsupported governor %r" % value)
        for pol in self.policies:
            write(pol + "/scaling_governor", value)

    # --------------------------------------------------------------- battery
    def battery_info(self, with_limit=True):
        b = self.battery
        if not b:
            return None

        def num(name):
            return read_int(b + "/" + name)

        info = {
            "name": os.path.basename(b),
            "capacity": num("capacity"),
            "status": read(b + "/status"),
            "cycles": num("cycle_count"),
            "technology": read(b + "/technology"),
            "manufacturer": read(b + "/manufacturer"),
            "model": read(b + "/model_name"),
            "voltage": (num("voltage_now") or 0) / 1e6 or None,
        }
        full, design, now = num("energy_full"), num("energy_full_design"), num("energy_now")
        unit = "Wh"
        if full is None:
            full, design, now = num("charge_full"), num("charge_full_design"), num("charge_now")
            unit = "Ah"
        info["full"] = full / 1e6 if full else None
        info["design"] = design / 1e6 if design else None
        info["now"] = now / 1e6 if now else None
        info["unit"] = unit
        info["health"] = round(full * 100.0 / design, 1) if full and design else None

        power = num("power_now")
        if power is not None:
            watts = power / 1e6
        else:
            cur, volt = num("current_now"), num("voltage_now")
            watts = (cur * volt) / 1e12 if cur is not None and volt else None
        info["power"] = round(abs(watts), 2) if watts is not None else None

        # Time estimate in hours
        info["time_left"] = None
        if info["power"] and info["now"] is not None:
            watt_hours_now = info["now"] if unit == "Wh" else info["now"] * (info["voltage"] or 0)
            watt_hours_full = info["full"] if unit == "Wh" else (info["full"] or 0) * (info["voltage"] or 0)
            if info["status"] == "Discharging":
                info["time_left"] = watt_hours_now / info["power"]
            elif info["status"] == "Charging" and watt_hours_full:
                info["time_left"] = max(0.0, watt_hours_full - watt_hours_now) / info["power"]

        info["limit"] = self.battery_limit() if with_limit else None
        return info

    def battery_limit(self):
        if self.charge_threshold:
            return read_int(self.charge_threshold)
        if self.linuwu and _exists(self.linuwu + "/battery_limiter"):
            return 80 if read_int(self.linuwu + "/battery_limiter") == 1 else 100
        return None

    def ac_online(self):
        return read_int(self.ac + "/online") == 1 if self.ac else None

    def set_battery_limit(self, percent):
        percent = int(percent)
        if self.charge_threshold:
            if not 50 <= percent <= 100:
                raise ValueError("charge limit must be 50..100 %")
            write(self.charge_threshold, percent)
        elif self.linuwu and _exists(self.linuwu + "/battery_limiter"):
            write(self.linuwu + "/battery_limiter", 1 if percent < 100 else 0)
        else:
            raise ValueError("battery charge limit not supported on this system")

    # -------------------------------------------------------------- keyboard
    def kb_set_zones(self, colors, brightness):
        if not self.kb4:
            raise ValueError("4-zone RGB keyboard control needs the linuwu_sense driver")
        if len(colors) != 4:
            raise ValueError("need exactly 4 zone colours")
        hexes = [parse_color(c) for c in colors]
        b = clamp_int(brightness, 0, 100)
        write(self.kb4 + "/per_zone_mode", ",".join(hexes + [str(b)]))

    def kb_set_effect(self, effect, speed, brightness, direction, color):
        if not self.kb4:
            raise ValueError("4-zone RGB keyboard control needs the linuwu_sense driver")
        mode = clamp_int(effect, 0, len(KB_EFFECTS) - 1)
        spd = clamp_int(speed, 0, 9)
        b = clamp_int(brightness, 0, 100)
        d = 1 if int(direction) == 1 else 2
        h = parse_color(color)
        r, g, bl = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        write(self.kb4 + "/four_zone_mode", "%d,%d,%d,%d,%d,%d,%d" % (mode, spd, b, d, r, g, bl))

    def kb_read(self):
        if not self.kb4:
            return None
        return {"zones": read(self.kb4 + "/per_zone_mode"), "effect": read(self.kb4 + "/four_zone_mode")}

    def kbd_led_brightness(self):
        return read_int(self.kbd_led + "/brightness") if self.kbd_led else None

    def set_kbd_led_brightness(self, value):
        if not self.kbd_led:
            raise ValueError("no keyboard backlight LED")
        mx = read_int(self.kbd_led + "/max_brightness") or 1
        write(self.kbd_led + "/brightness", clamp_int(value, 0, mx))

    # --------------------------------------------------------------- toggles
    def toggles(self):
        if not self.linuwu:
            return {}
        out = {}
        for k, (_label, _desc, allowed) in LINUWU_TOGGLES.items():
            if _exists(self.linuwu + "/" + k):
                v = read_int(self.linuwu + "/" + k)
                out[k] = v if v in allowed else None
        return out

    def set_toggle(self, name, value):
        if name not in LINUWU_TOGGLES or not self.linuwu or not _exists(self.linuwu + "/" + name):
            raise ValueError("unknown or unsupported setting %r" % name)
        value = int(value)
        if value not in LINUWU_TOGGLES[name][2]:
            raise ValueError("invalid value %r for %s" % (value, name))
        write(self.linuwu + "/" + name, value)

    # ------------------------------------------------------------------ dGPU
    def dgpu_state(self):
        if not self.dgpu:
            return None
        return read(self.dgpu + "/power/runtime_status")

    # -------------------------------------------------------------- snapshot
    def snapshot(self):
        """Everything a UI needs, read in one go. Safe for unprivileged users."""
        return {
            "temps": self.read_temps(),
            "fans": self.read_fans(),
            "fan_mode": self.fan_mode(),
            "profile": self.profile(),
            "cpu_boost": self.cpu_boost(),
            "epp": self.epp(),
            "governor": self.governor(),
            "battery": self.battery_info(),
            "ac_online": self.ac_online(),
            "keyboard": self.kb_read(),
            "kbd_led": self.kbd_led_brightness(),
            "toggles": self.toggles(),
            "dgpu_state": self.dgpu_state(),
        }


# ---------------------------------------------------------------- helpers
def dmi(name):
    return read(p("/sys/class/dmi/id/" + name))


def clamp_int(value, lo, hi):
    return max(lo, min(hi, int(value)))


_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(value):
    """'#ff0044' / 'ff0044' / [r, g, b] -> 'ff0044'."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return "%02x%02x%02x" % tuple(clamp_int(c, 0, 255) for c in value)
    m = _HEX.match(str(value).strip())
    if not m:
        raise ValueError("bad colour %r" % (value,))
    return m.group(1).lower()


def _ppdctl():
    return None if ROOT else shutil.which("powerprofilesctl")


def ppd_active():
    """True when power-profiles-daemon is running and answering."""
    ctl = _ppdctl()
    if not ctl:
        return False
    try:
        return subprocess.run([ctl, "get"], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ppd_profile_for(profile, choices):
    """Map a platform profile to power-profiles-daemon's name for it, if it has one.

    Mirrors power-profiles-daemon's own mapping, including its fallbacks for
    firmware without low-power/performance (quiet / balanced-performance)."""
    saver = "low-power" if "low-power" in choices else "quiet"
    perf = "performance" if "performance" in choices else "balanced-performance"
    return {saver: "power-saver", "balanced": "balanced", perf: "performance"}.get(profile)


def ppd_set(name):
    ctl = _ppdctl()
    if not ctl:
        return False
    try:
        return subprocess.run([ctl, "set", name], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def closest_profile(wanted, choices):
    """Pick the nearest available platform profile (e.g. no 'quiet' -> 'low-power')."""
    if wanted in choices:
        return wanted
    if not choices or wanted not in PROFILE_ORDER:
        return None
    idx = PROFILE_ORDER.index(wanted)
    ranked = sorted((c for c in choices if c in PROFILE_ORDER),
                    key=lambda c: (abs(PROFILE_ORDER.index(c) - idx), -PROFILE_ORDER.index(c)))
    return ranked[0] if ranked else None
