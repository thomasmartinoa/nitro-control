"""Tests run against a fake sysfs tree; they never touch real hardware.

    python3 -m unittest discover -s tests -v
"""

import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fakesys  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="nitro-fakesys-")
os.environ["NITRO_SYSFS_ROOT"] = ROOT

from nitrocontrol import fancurve  # noqa: E402
from nitrocontrol import hw as hwmod  # noqa: E402
from nitrocontrol import daemon  # noqa: E402


def fresh(linuwu=False, threshold=False, linuwu_driver=False):
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT)
    fakesys.build(ROOT, linuwu=linuwu, threshold=threshold, linuwu_driver=linuwu_driver)
    importlib.reload(hwmod)
    return hwmod.Hardware()


def val(path):
    with open(ROOT + path) as f:
        return f.read().strip()


H = fakesys.HWMON


class CurveTests(unittest.TestCase):
    def test_validate_sorts_and_makes_monotonic(self):
        c = fancurve.validate_curve([[80, 50], [40, 60], [60, 30]])
        self.assertEqual(c, [[40, 60], [60, 60], [80, 60]])

    def test_validate_rejects_bad(self):
        for bad in ([[40, 20]], [[10, 20], [50, 30]], [[40, 20], [40, 30]], [[40, 120], [50, 30]], "x",
                    [[i * 5 + 20, i] for i in range(9)]):
            with self.assertRaises((ValueError, TypeError)):
                fancurve.validate_curve(bad)

    def test_interpolate(self):
        pts = [[40, 20], [60, 60]]
        self.assertEqual(fancurve.interpolate(pts, 30), 20)
        self.assertEqual(fancurve.interpolate(pts, 50), 40)
        self.assertEqual(fancurve.interpolate(pts, 90), 60)
        self.assertEqual(fancurve.interpolate(pts, None), 100)

    def test_safety_floor(self):
        self.assertEqual(fancurve.safety_floor(60), 0)
        self.assertEqual(fancurve.safety_floor(76), 40)
        self.assertEqual(fancurve.safety_floor(86), 70)
        self.assertEqual(fancurve.safety_floor(92), 100)

    def test_hysteresis_and_ramp(self):
        pts = [[40, 0], [80, 100]]
        up = fancurve.next_speed(pts, 70, 10)
        self.assertEqual(up, 75)                      # ramps up instantly
        down = fancurve.next_speed(pts, 60, 75)
        self.assertEqual(down, 71)                    # slows at most RAMP_DOWN per step
        hold = fancurve.next_speed(pts, 69, 75)
        self.assertEqual(hold, 75)                    # within hysteresis: holds
        self.assertEqual(fancurve.next_speed([[40, 0], [60, 0]], 88, None), 70)  # floor wins


class HardwareTests(unittest.TestCase):
    def test_detect(self):
        hw = fresh()
        caps = hw.capabilities()
        self.assertEqual(caps["fan_control"], "hwmon")
        self.assertEqual(caps["model"], "Nitro AN515-58")
        self.assertEqual(caps["profiles"][0], "low-power")
        self.assertFalse(caps["keyboard_rgb"])
        self.assertEqual(hw.read_temps()["cpu"], 55.0)

    def test_fan_writes(self):
        hw = fresh()
        hw.fan_set_speeds({"cpu": 50, "gpu": 100})
        self.assertEqual(val(H + "/pwm1_enable"), "1")
        self.assertEqual(val(H + "/pwm1"), "128")
        self.assertEqual(val(H + "/pwm2"), "255")
        self.assertEqual(hw.fan_mode(), "custom")
        hw.fan_set_max()
        self.assertEqual(hw.fan_mode(), "max")
        hw.fan_set_auto()
        self.assertEqual(val(H + "/pwm2_enable"), "2")

    def test_rejects_invalid(self):
        hw = fresh()
        with self.assertRaises(ValueError):
            hw.set_profile("ludicrous")
        with self.assertRaises(ValueError):
            hw.set_epp("warp")
        with self.assertRaises(ValueError):
            hw.set_battery_limit(80)  # not supported without threshold / linuwu
        with self.assertRaises(PermissionError):
            hwmod.write("/etc/passwd", "x")

    def test_linuwu(self):
        hw = fresh(linuwu=True)
        caps = hw.capabilities()
        self.assertTrue(caps["keyboard_rgb"])
        self.assertEqual(caps["battery_limit"], "limiter")
        self.assertEqual(caps["fan_control"], "hwmon")   # mainline preferred when present
        hw.kb_set_zones(["#ff0000", "00ff00", [0, 0, 255], "ffffff"], 70)
        self.assertEqual(val("/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi/four_zoned_kb/per_zone_mode"),
                         "ff0000,00ff00,0000ff,ffffff,70")
        hw.kb_set_effect(3, 12, 150, 2, "00ffcc")
        self.assertEqual(val("/sys/module/linuwu_sense/drivers/platform:acer-wmi/acer-wmi/four_zoned_kb/four_zone_mode"),
                         "3,9,100,2,0,255,204")
        with self.assertRaises(ValueError):
            hw.set_toggle("usb_charging", 15)
        hw.set_battery_limit(80)
        self.assertEqual(hw.battery_info()["limit"], 80)

    def test_threshold(self):
        hw = fresh(threshold=True)
        hw.set_battery_limit(80)
        self.assertEqual(hw.battery_info()["limit"], 80)
        with self.assertRaises(ValueError):
            hw.set_battery_limit(20)

    def test_closest_profile(self):
        self.assertEqual(hwmod.closest_profile("quiet", ["low-power", "balanced", "performance"]), "balanced")
        self.assertEqual(hwmod.closest_profile("balanced-performance", ["low-power", "balanced", "performance"]),
                         "performance")
        self.assertIsNone(hwmod.closest_profile("quiet", []))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.hw = fresh()
        self.state = os.path.join(ROOT, "state.json")
        daemon.hwmod = hwmod
        self.ctl = daemon.Controller(self.hw, self.state)

    def temps(self, cpu, gpu):
        fakesys.set_value(ROOT, H + "/temp1_input", cpu * 1000)
        fakesys.set_value(ROOT, H + "/temp2_input", gpu * 1000)

    def test_curve_mode_drives_pwm(self):
        self.ctl.set_curve("both", [[40, 20], [80, 100]])
        self.ctl.set_fan("curve")
        self.temps(60, 50)
        self.ctl.tick()
        self.assertEqual(val(H + "/pwm1_enable"), "1")
        self.assertEqual(self.ctl.applied, {"cpu": 60, "gpu": 40})

    def test_custom_respects_floor(self):
        self.ctl.set_fan("custom", cpu=10, gpu=10)
        self.temps(86, 50)
        self.ctl.tick()
        self.assertEqual(self.ctl.applied["cpu"], 70)
        self.assertEqual(self.ctl.applied["gpu"], 10)

    def test_emergency_and_recovery(self):
        self.ctl.set_fan("custom", cpu=30, gpu=30)
        self.temps(96, 60)
        self.ctl.tick()
        self.assertTrue(self.ctl.emergency)
        self.assertEqual(val(H + "/pwm1_enable"), "0")
        self.temps(90, 60)
        self.ctl.tick()
        self.assertTrue(self.ctl.emergency)         # still within cool-down band
        self.temps(80, 60)
        self.ctl.tick()
        self.assertFalse(self.ctl.emergency)
        self.assertEqual(val(H + "/pwm1_enable"), "1")

    def test_sensor_failure_returns_to_auto(self):
        self.ctl.set_fan("curve")
        os.remove(ROOT + H + "/temp1_input")
        os.remove(ROOT + H + "/temp2_input")
        self.hw.temps.pop("gpu")
        self.hw.temps["cpu"] = ROOT + H + "/temp1_input"
        for _ in range(daemon.SENSOR_FAIL_LIMIT):
            self.ctl.tick()
        self.assertEqual(self.ctl.state["fan_mode"], "auto")
        self.assertEqual(val(H + "/pwm1_enable"), "2")

    def test_shutdown_restores_auto(self):
        self.ctl.set_fan("max")
        self.ctl.shutdown()
        self.assertEqual(val(H + "/pwm1_enable"), "2")
        with open(self.state) as f:
            saved = json.load(f)
        self.assertEqual(saved["fan_mode"], "max")      # remembered for next start

    def test_presets(self):
        self.ctl.apply_preset("Battery Saver")
        self.assertEqual(self.hw.profile(), "low-power")
        self.assertEqual(self.hw.cpu_boost(), False)
        self.assertEqual(self.hw.epp(), "power")
        self.ctl.apply_preset("Gaming")
        self.assertEqual(self.hw.profile(), "balanced-performance")
        self.assertEqual(self.ctl.state["fan_mode"], "curve")
        self.assertEqual(self.hw.epp(), "balance_performance")
        self.ctl.save_preset("Mine")
        self.assertIn("Mine", self.ctl.state["presets"])
        with self.assertRaises(daemon.RequestError):
            self.ctl.save_preset("Turbo")
        self.ctl.delete_preset("Mine")
        self.assertNotIn("Mine", self.ctl.state["presets"])

    def test_auto_switch_on_ac_change(self):
        self.ctl.set_auto_switch(enabled=True, ac="Turbo", battery="Battery Saver")
        self.ctl.tick()
        fakesys.set_value(ROOT, "/sys/class/power_supply/ACAD/online", 1)
        self.ctl.tick()
        self.assertEqual(self.hw.profile(), "performance")
        self.assertEqual(self.ctl.state["fan_mode"], "max")

    def test_corrupt_state_file(self):
        with open(self.state, "w") as f:
            json.dump({"fan_mode": "warp", "curves": {"cpu": [[1, 2]]}, "safety": {"cpu_critical": 200}}, f)
        ctl = daemon.Controller(self.hw, self.state)
        self.assertEqual(ctl.state["fan_mode"], "auto")
        self.assertEqual(ctl.state["curves"]["cpu"], fancurve.CURVES["balanced"])
        self.assertEqual(ctl.state["safety"]["cpu_critical"], 100)

    def test_safety_limits_enforced(self):
        with self.assertRaises(daemon.RequestError):
            self.ctl.set_safety(cpu_critical=105)


class LinuwuDriverTests(unittest.TestCase):
    """linuwu_sense replaced acer_wmi: fans are driven through nitro_sense/fan_speed."""

    def setUp(self):
        self.hw = fresh(linuwu_driver=True)
        self.ctl = daemon.Controller(self.hw, os.path.join(ROOT, "state.json"))

    def test_backend(self):
        caps = self.hw.capabilities()
        self.assertEqual(caps["fan_control"], "linuwu")
        self.assertEqual([f["key"] for f in caps["fans"]], ["cpu", "gpu"])
        self.assertTrue(caps["keyboard_rgb"])
        self.assertEqual(self.hw.read_fans()["cpu"]["rpm"], 2400)

    def test_writes(self):
        self.hw.fan_set_speeds({"cpu": 0, "gpu": 55})
        self.assertEqual(val(fakesys.LINUWU + "/fan_speed"), "1,55")   # 0 would mean auto
        self.assertEqual(self.hw.fan_mode(), "custom")
        self.hw.fan_set_max()
        self.assertEqual(self.hw.fan_mode(), "max")
        self.hw.fan_set_auto()
        self.assertEqual(val(fakesys.LINUWU + "/fan_speed"), "0,0")

    def test_full_custom_is_not_a_mismatch(self):
        self.ctl.set_fan("custom", cpu=100, gpu=100)
        self.ctl.last_verify = -1e9
        self.ctl.tick()
        self.assertFalse(any("Firmware changed" in e["msg"] for e in self.ctl.events))

    def test_ac_change_reapplies_fans(self):
        self.ctl.set_fan("custom", cpu=40, gpu=40)
        self.ctl.tick()
        fakesys.set_value(ROOT, fakesys.LINUWU + "/fan_speed", "0,0")   # driver restored its AC state
        fakesys.set_value(ROOT, "/sys/class/power_supply/ACAD/online", 1)
        self.ctl.tick()
        self.assertEqual(val(fakesys.LINUWU + "/fan_speed"), "40,40")


class PowerProfilesTests(unittest.TestCase):
    def test_mapping_full_firmware(self):
        ch = ["low-power", "quiet", "balanced", "balanced-performance", "performance"]
        self.assertEqual(hwmod.ppd_profile_for("low-power", ch), "power-saver")
        self.assertEqual(hwmod.ppd_profile_for("performance", ch), "performance")
        self.assertIsNone(hwmod.ppd_profile_for("quiet", ch))          # no PPD equivalent: written directly

    def test_mapping_three_mode_firmware(self):
        ch = ["quiet", "balanced", "balanced-performance"]           # AN515-58 under linuwu_sense
        self.assertEqual(hwmod.ppd_profile_for("quiet", ch), "power-saver")
        self.assertEqual(hwmod.ppd_profile_for("balanced", ch), "balanced")
        self.assertEqual(hwmod.ppd_profile_for("balanced-performance", ch), "performance")

    def test_never_touches_real_ppd_in_tests(self):
        self.assertFalse(hwmod.ppd_active())
        self.assertFalse(hwmod.ppd_set("balanced"))


class SlowReadTests(unittest.TestCase):
    def test_unreadable_toggle_is_unknown(self):
        hw = fresh(linuwu=True)
        fakesys.set_value(ROOT, fakesys.LINUWU + "/lcd_override", -1)
        self.assertIsNone(hw.toggles()["lcd_override"])

    def test_keyboard_read_is_throttled(self):
        hw = fresh(linuwu=True)
        ctl = daemon.Controller(hw, os.path.join(ROOT, "state.json"))
        calls = []
        orig = hw.kb_read
        hw.kb_read = lambda: calls.append(1) or orig()
        for _ in range(5):
            ctl.tick()
        self.assertEqual(len(calls), 1)
        daemon.dispatch(ctl, {"cmd": "set_keyboard", "args": {"mode": "zones", "colors": ["ff0000"] * 4}}, True)
        ctl.tick()
        self.assertEqual(len(calls), 2)                      # refreshed right after a change


class SocketTests(unittest.TestCase):
    def test_roundtrip_and_permissions(self):
        hw = fresh()
        ctl = daemon.Controller(hw, os.path.join(ROOT, "state.json"))
        ctl.tick()
        path = os.path.join(ROOT, "test.sock")
        srv = daemon.Server(path, daemon.Handler)
        srv.ctl = ctl
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            from nitrocontrol.client import Client, NitroError
            c = Client(path)
            self.assertEqual(c.call("status")["temps"]["cpu"], 55.0)
            ctl.state["allowed_groups"] = []
            if os.geteuid() != 0:
                with self.assertRaises(NitroError):
                    c.call("set_profile", profile="quiet")
            ctl.state["allowed_groups"] = [__import__("grp").getgrgid(os.getgid()).gr_name]
            c.call("set_profile", profile="quiet")
            self.assertEqual(hw.profile(), "quiet")
            with self.assertRaises(NitroError):
                c.call("set_profile", profile="nope")
            with self.assertRaises(NitroError):
                c.call("rm_rf")
            with self.assertRaises(NitroError):
                c.call("set_fan", mode="custom", bogus=1)
            s = socket.socket(socket.AF_UNIX)
            s.connect(path)
            s.sendall(b"not json\n")
            self.assertFalse(json.loads(s.makefile().readline())["ok"])
            s.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
