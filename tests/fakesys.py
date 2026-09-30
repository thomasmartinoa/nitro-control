"""Builds a fake /sys tree that mimics an Acer Nitro AN515-58 on a recent kernel."""

import os


def _w(root, path, value):
    full = root + path
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(str(value) + "\n")


def build(root, linuwu=False, threshold=False, linuwu_driver=False):
    """linuwu: linuwu_sense extras present. linuwu_driver: linuwu_sense *replaced* acer_wmi,
    so the hwmon device has sensors but no PWM control."""
    linuwu = linuwu or linuwu_driver
    hw = "/sys/devices/platform/acer-wmi/hwmon/hwmon5"
    attrs = [("name", "acer"), ("fan1_input", 2400), ("fan2_input", 2600), ("temp1_input", 55000),
             ("temp2_input", 50000), ("temp3_input", 45000)]
    if not linuwu_driver:
        attrs += [("pwm1", 0), ("pwm2", 0), ("pwm1_enable", 2), ("pwm2_enable", 2)]
    for name, val in attrs:
        _w(root, hw + "/" + name, val)
    os.makedirs(root + "/sys/class/hwmon", exist_ok=True)
    os.symlink(root + hw, root + "/sys/class/hwmon/hwmon5")

    core = "/sys/devices/platform/coretemp.0/hwmon/hwmon7"
    _w(root, core + "/name", "coretemp")
    _w(root, core + "/temp1_label", "Package id 0")
    _w(root, core + "/temp1_input", 57000)
    os.symlink(root + core, root + "/sys/class/hwmon/hwmon7")

    pp = "/sys/class/platform-profile/platform-profile-0"
    _w(root, pp + "/name", "acer-wmi")
    _w(root, pp + "/choices", "low-power quiet balanced balanced-performance performance")
    _w(root, pp + "/profile", "balanced")

    bat = "/sys/class/power_supply/BAT1"
    for name, val in (("type", "Battery"), ("capacity", 64), ("status", "Discharging"), ("cycle_count", 12),
                      ("charge_full", 3040000), ("charge_full_design", 3733000), ("charge_now", 1950000),
                      ("current_now", 1500000), ("voltage_now", 15800000), ("technology", "Li-ion")):
        _w(root, bat + "/" + name, val)
    if threshold:
        _w(root, bat + "/charge_control_end_threshold", 100)
    _w(root, "/sys/class/power_supply/ACAD/type", "Mains")
    _w(root, "/sys/class/power_supply/ACAD/online", 0)

    _w(root, "/sys/devices/system/cpu/intel_pstate/no_turbo", 0)
    for pol in ("policy0", "policy1"):
        base = "/sys/devices/system/cpu/cpufreq/" + pol
        _w(root, base + "/energy_performance_available_preferences",
           "default performance balance_performance balance_power power")
        _w(root, base + "/energy_performance_preference", "balance_performance")
        _w(root, base + "/scaling_available_governors", "performance powersave")
        _w(root, base + "/scaling_governor", "powersave")

    gpu = "/sys/bus/pci/devices/0000:01:00.0"
    _w(root, gpu + "/class", "0x030000")
    _w(root, gpu + "/vendor", "0x10de")
    _w(root, gpu + "/power/runtime_status", "suspended")

    for f, v in (("sys_vendor", "Acer"), ("product_name", "Nitro AN515-58"), ("bios_version", "V2.18"),
                 ("board_name", "Jimny_ADH")):
        _w(root, "/sys/class/dmi/id/" + f, v)

    if linuwu:
        base = "/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi"
        for name, val in (("battery_limiter", 0), ("backlight_timeout", 0), ("usb_charging", 0),
                          ("lcd_override", 0), ("boot_animation_sound", 1), ("fan_speed", "0,0")):
            _w(root, base + "/nitro_sense/" + name, val)
        _w(root, base + "/four_zoned_kb/per_zone_mode", "ff0000,00ff00,0000ff,ffffff,100")
        _w(root, base + "/four_zoned_kb/four_zone_mode", "0,1,100,2,255,0,0")
    return root


def set_value(root, path, value):
    _w(root, path, value)


HWMON = "/sys/devices/platform/acer-wmi/hwmon/hwmon5"
LINUWU = "/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi/nitro_sense"
