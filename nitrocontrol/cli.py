"""nitroctl — command-line front end for Nitro Control."""

import argparse
import json
import sys
import time

from . import __version__
from . import hw as hwmod
from .client import Client, DaemonUnavailable, NitroError

RED, CYAN, DIM, BOLD, RESET = "\033[91m", "\033[96m", "\033[2m", "\033[1m", "\033[0m"


def _c(code, text):
    return code + str(text) + RESET if sys.stdout.isatty() else str(text)


def _local_status():
    return hwmod.Hardware().snapshot()


def get_status(client):
    try:
        return client.call("status")
    except DaemonUnavailable:
        st = _local_status()
        st["daemon"] = False
        return st


def fmt_temp(v):
    if v is None:
        return "--"
    color = RED if v >= 85 else (CYAN if v < 60 else "")
    return _c(color, "%.0f°C" % v) if color else "%.0f°C" % v


def print_status(st):
    t, fans = st.get("temps", {}), st.get("fans", {})
    prof = st.get("profile")
    print(_c(BOLD, "Nitro Control") + _c(DIM, "  (service %s)" % ("running" if st.get("daemon") else "offline")))
    print("  Profile   : %s" % (hwmod.PROFILE_LABELS.get(prof, prof) if prof else "--"))
    print("  Fan mode  : %s%s" % (st.get("fan_mode") or "--", _c(RED, "  [EMERGENCY MAX]") if st.get("emergency") else ""))
    print("  CPU       : %s   fan %s rpm%s" % (fmt_temp(t.get("cpu")), fans.get("cpu", {}).get("rpm", "--"),
                                             " @ %s%%" % fans["cpu"]["pct"] if fans.get("cpu", {}).get("pct") is not None and st.get("fan_mode") in ("custom", "curve") else ""))
    print("  GPU       : %s   fan %s rpm%s" % (fmt_temp(t.get("gpu")), fans.get("gpu", {}).get("rpm", "--"),
                                             " @ %s%%" % fans["gpu"]["pct"] if fans.get("gpu", {}).get("pct") is not None and st.get("fan_mode") in ("custom", "curve") else ""))
    if "sys" in t:
        print("  System    : %s" % fmt_temp(t["sys"]))
    b = st.get("battery")
    if b:
        extra = " limit %s%%" % b["limit"] if b.get("limit") and b["limit"] < 100 else ""
        power = " %.1f W" % b["power"] if b.get("power") else ""
        health = " health %s%%" % b["health"] if b.get("health") else ""
        print("  Battery   : %s%% %s%s%s%s" % (b.get("capacity"), b.get("status"), power, health, extra))
    if st.get("cpu_boost") is not None:
        print("  CPU boost : %s   EPP %s" % ("on" if st["cpu_boost"] else "off", st.get("epp") or "--"))
    if st.get("dgpu_state"):
        print("  dGPU      : %s" % st["dgpu_state"])
    if st.get("active_preset"):
        print("  Preset    : %s" % st["active_preset"])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nitroctl", description="Control fans, performance, lighting and battery on Acer Nitro/Predator laptops")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="show temperatures, fans and settings")
    w = sub.add_parser("watch", help="live status")
    w.add_argument("-n", type=float, default=1.0, help="refresh interval")
    sub.add_parser("info", help="show detected hardware capabilities")

    pr = sub.add_parser("profile", help="get/set the thermal profile")
    pr.add_argument("name", nargs="?", help="eco|quiet|balanced|performance|turbo, a raw ACPI name, or 'next'")

    fa = sub.add_parser("fan", help="fan control")
    fa.add_argument("mode", choices=["auto", "max", "custom", "curve"])
    fa.add_argument("cpu", nargs="?", type=int, help="CPU fan %% (custom)")
    fa.add_argument("gpu", nargs="?", type=int, help="GPU fan %% (custom, defaults to CPU value)")

    cu = sub.add_parser("curve", help="set a fan curve, e.g. 'curve both 40:20 60:45 75:70 85:100' or 'curve both silent'")
    cu.add_argument("fan", choices=["cpu", "gpu", "both"])
    cu.add_argument("points", nargs="+")

    bo = sub.add_parser("boost", help="CPU turbo boost on/off")
    bo.add_argument("state", choices=["on", "off"])

    ep = sub.add_parser("epp", help="CPU energy performance preference")
    ep.add_argument("value")

    ba = sub.add_parser("battery-limit", help="charge limit in percent (100 = off)")
    ba.add_argument("percent", type=int)

    kb = sub.add_parser("rgb", help="4-zone keyboard RGB (needs linuwu_sense)")
    kbs = kb.add_subparsers(dest="kbmode")
    z = kbs.add_parser("zones", help="static colours per zone: rgb zones ff0000 00ff00 0000ff ffffff -b 100")
    z.add_argument("colors", nargs="+")
    z.add_argument("-b", "--brightness", type=int, default=100)
    e = kbs.add_parser("effect", help="animated effect: rgb effect wave -s 4 -b 100 -c ff0000 -d right")
    e.add_argument("name", choices=[x.lower() for x in hwmod.KB_EFFECTS])
    e.add_argument("-s", "--speed", type=int, default=4)
    e.add_argument("-b", "--brightness", type=int, default=100)
    e.add_argument("-c", "--color", default="ff0000")
    e.add_argument("-d", "--direction", choices=["left", "right"], default="right")
    kbs.add_parser("off", help="turn the keyboard lighting off")

    ps = sub.add_parser("preset", help="list/apply/save/delete presets")
    ps.add_argument("action", choices=["list", "apply", "save", "delete"])
    ps.add_argument("name", nargs="?")

    args = ap.parse_args(argv)
    client = Client()

    try:
        return run(args, client)
    except DaemonUnavailable as e:
        print(_c(RED, "error: ") + str(e), file=sys.stderr)
        print("start it with:  sudo systemctl enable --now nitro-controld", file=sys.stderr)
        return 2
    except NitroError as e:
        print(_c(RED, "error: ") + str(e), file=sys.stderr)
        return 1


PROFILE_ALIASES = {"eco": "low-power", "quiet": "quiet", "silent": "quiet", "balanced": "balanced",
                   "performance": "balanced-performance", "turbo": "performance"}


def run(args, client):
    out = (lambda d: print(json.dumps(d, indent=2))) if args.json else None

    if args.cmd in (None, "status"):
        st = get_status(client)
        out(st) if out else print_status(st)
    elif args.cmd == "watch":
        try:
            while True:
                st = get_status(client)
                sys.stdout.write("\033[H\033[J")
                print_status(st)
                time.sleep(max(0.5, args.n))
        except KeyboardInterrupt:
            pass
    elif args.cmd == "info":
        caps = hwmod.Hardware().capabilities()
        if out:
            out(caps)
        else:
            for k, v in caps.items():
                print("%-15s %s" % (k, v))
    elif args.cmd == "profile":
        if not args.name:
            prof = get_status(client).get("profile")
            choices = hwmod.Hardware().profile_choices()
            for c in choices:
                mark = _c(RED, "●") if c == prof else " "
                print(" %s %-12s (%s)" % (mark, hwmod.PROFILE_LABELS.get(c, c), c))
        else:
            name = PROFILE_ALIASES.get(args.name.lower(), args.name.lower())
            got = client.call("set_profile", profile=name)
            print("profile: %s" % hwmod.PROFILE_LABELS.get(got, got))
    elif args.cmd == "fan":
        kw = {"mode": args.mode}
        if args.mode == "custom":
            if args.cpu is None:
                raise NitroError("custom mode needs a CPU percentage, e.g. 'nitroctl fan custom 60 70'")
            kw.update(cpu=args.cpu, gpu=args.gpu if args.gpu is not None else args.cpu)
        client.call("set_fan", **kw)
        print("fans: %s" % args.mode)
    elif args.cmd == "curve":
        from .fancurve import CURVES
        if len(args.points) == 1 and args.points[0] in CURVES:
            pts = CURVES[args.points[0]]
        else:
            try:
                pts = [[int(a), int(b)] for a, b in (x.split(":") for x in args.points)]
            except ValueError:
                raise NitroError("points look like TEMP:PERCENT, e.g. 60:45")
        client.call("set_curve", fan=args.fan, points=pts)
        client.call("set_fan", mode="curve")
        print("curve applied to %s fan(s)" % args.fan)
    elif args.cmd == "boost":
        client.call("set_boost", enabled=args.state == "on")
        print("CPU boost %s" % args.state)
    elif args.cmd == "epp":
        client.call("set_epp", value=args.value)
        print("EPP: %s" % args.value)
    elif args.cmd == "battery-limit":
        client.call("set_battery_limit", value=args.percent)
        print("battery limit: %d%%" % args.percent)
    elif args.cmd == "rgb":
        if args.kbmode == "zones":
            cols = (args.colors * 4)[:4]
            client.call("set_keyboard", mode="zones", colors=cols, brightness=args.brightness)
        elif args.kbmode == "effect":
            client.call("set_keyboard", mode="effect", effect=[x.lower() for x in hwmod.KB_EFFECTS].index(args.name),
                        speed=args.speed, brightness=args.brightness, color=args.color,
                        direction=1 if args.direction == "left" else 2)
        elif args.kbmode == "off":
            client.call("set_keyboard", mode="zones", colors=["000000"] * 4, brightness=0)
        else:
            raise NitroError("use: nitroctl rgb zones|effect|off")
        print("keyboard updated")
    elif args.cmd == "preset":
        if args.action == "list":
            state = client.call("get_state")
            active = get_status(client).get("active_preset")
            for group, items in (("built-in", state["builtin_presets"]), ("yours", state.get("presets") or {})):
                for name, p in items.items():
                    mark = _c(RED, "●") if name == active else " "
                    print(" %s %-16s %s" % (mark, name, _c(DIM, "%s: %s" % (group, p.get("desc", "")))))
        else:
            if not args.name:
                raise NitroError("preset %s needs a name" % args.action)
            client.call("%s_preset" % args.action, name=args.name)
            print("preset %s: %s" % (args.action.replace("apply", "applied").replace("save", "saved").replace("delete", "deleted"), args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
