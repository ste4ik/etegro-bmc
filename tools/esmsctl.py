#!/usr/bin/env python3
"""esmsctl - Python replacement for the ESMS/Quanta BMC Java client (IPMI part).

Talks standard IPMI 2.0 over LAN (UDP 623) via pyghmi, so it needs no Java,
no browser and no vendor tools. Covers power, sensors, SEL, FRU, boot device
and SOL.

Usage:
    esmsctl.py [-b BMC] [-u USER] [-p PASS] [-P PORT] <command> [args]

Credentials: -p / --password, or env ESMS_PASS, or interactive prompt.
"""
import argparse
import getpass
import json
import os
import sys
import time

try:
    from pyghmi.ipmi import command as ipmicmd
    from pyghmi.ipmi import console as ipmiconsole
    from pyghmi.exceptions import IpmiException
except ImportError:
    sys.exit("pyghmi is required: python -m pip install pyghmi")

def _config_host():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app",
                        "config.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh).get("host", "")
    except (OSError, ValueError):
        return ""


DEFAULT_BMC = os.environ.get("ESMS_BMC") or _config_host()
DEFAULT_USER = "root"


def get_env_password():
    """ESMS_PASS from env; on Windows fall back to the user environment registry."""
    pw = os.environ.get("ESMS_PASS")
    if pw:
        return pw
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                pw, _ = winreg.QueryValueEx(key, "ESMS_PASS")
        except OSError:
            pw = None
    return pw


def connect(args):
    password = args.password or get_env_password()
    if not password:
        password = getpass.getpass(f"IPMI password for {args.user}@{args.bmc}: ")
    try:
        return ipmicmd.Command(
            bmc=args.bmc, userid=args.user, password=password, port=args.port
        )
    except Exception as exc:  # noqa: BLE001 - surface any connect error plainly
        sys.exit(f"connect failed: {exc}")


def field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def fmt_value(sensor):
    val = field(sensor, "value")
    if val is None:
        return "-"
    if isinstance(val, float):
        val = f"{val:g}"
    return f"{val} {field(sensor, 'units', '')}".strip()


def cmd_info(ipmi, args):
    try:
        dev = ipmi.raw_command(netfn=0x06, command=0x01)
    except IpmiException as exc:
        sys.exit(f"ipmi error: {exc}")
    data = dev.get("data", ())
    fields = ["Device ID", "Device Revision", "Firmware Major",
              "Firmware Minor", "IPMI Version", "Support", "Manufacturer ID",
              "Product ID", "Aux Firmware Revision"]
    for name, value in zip(fields, data):
        print(f"{name:24}: {value}")


def cmd_bmc(ipmi, args):
    if args.action == "reset":
        result = ipmi.raw_command(netfn=0x06, command=0x02)  # Cold Reset
        print("cold reset sent", result)
        print("BMC will be unavailable for ~1-2 minutes (host stays up)")
    else:
        result = ipmi.raw_command(netfn=0x06, command=0x01)
        print(result)


def cmd_power(ipmi, args):
    action = args.action or "status"
    if action == "status":
        print(ipmi.get_power().get("powerstate", "unknown"))
        return
    mapping = {
        "on": "on", "off": "off", "reset": "reset", "cycle": "cycle",
        "softoff": "shutdown", "shutdown": "shutdown",
    }
    if action not in mapping:
        sys.exit(f"unknown power action: {action}")
    result = ipmi.set_power(mapping[action], wait=args.wait)
    print(result.get("powerstate", result))


def cmd_sensors(ipmi, args):
    rows = []
    for sensor in ipmi.get_sensor_data():
        if not args.all and field(sensor, "value") is None:
            continue
        rows.append(sensor)
    if args.json:
        import json
        print(json.dumps([getattr(s, "__dict__", s) for s in rows], indent=2, default=str))
        return
    print(f"{'SENSOR':32} {'VALUE':>18} {'HEALTH':<10} TYPE")
    for s in rows:
        print(f"{field(s, 'name', ''):32} {fmt_value(s):>18} "
              f"{str(field(s, 'health', '-')):<10} {field(s, 'type', '')}")


def cmd_sel(ipmi, args):
    events = list(ipmi.get_event_log(clear=args.clear))
    if args.clear and events:
        print(f"cleared {len(events)} event(s)", file=sys.stderr)
    if args.json:
        import json
        print(json.dumps([getattr(e, "__dict__", e) for e in events], indent=2, default=str))
        return
    if not events:
        print("no SEL entries")
        return
    for e in events:
        ts = field(e, "timestamp", "") or field(e, "time", "")
        sev = field(e, "severity", "")
        desc = field(e, "description") or field(e, "event") or ""
        print(f"{ts}  {sev:<8} {desc}")


def cmd_fru(ipmi, args):
    for name, info in ipmi.get_inventory():
        if not info:
            continue
        print(f"== {name}")
        for key, value in info.items():
            if value in (None, ""):
                continue
            print(f"  {key:22}: {value}")


def cmd_boot(ipmi, args):
    if args.device is None:
        print(ipmi.get_bootdev())
        return
    mapping = {
        "pxe": "network", "net": "network", "hd": "hd", "disk": "hd",
        "cd": "optical", "dvd": "optical", "bios": "setup", "setup": "setup",
        "default": "default",
    }
    dev = mapping.get(args.device, args.device)
    print(ipmi.set_bootdev(dev, persist=args.persist, uefiboot=args.uefi))


def cmd_raw(ipmi, args):
    data = tuple(int(x, 0) for x in args.data)
    result = ipmi.raw_command(netfn=args.netfn, command=args.command, data=data)
    print(result)


class StdoutHandler:
    """pyghmi SOL wants a callable receiving bytes."""

    def __call__(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def cmd_sol(ipmi, args):
    try:
        con = ipmiconsole.Console(
            bmc=args.bmc, userid=args.user, password=args.password or
            os.environ.get("ESMS_PASS") or getpass.getpass(
                f"IPMI password for {args.user}@{args.bmc}: "),
            iohandler=StdoutHandler(), port=args.port, force=True,
        )
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"SOL failed: {exc}")

    import threading

    def pump():
        while True:
            chunk = os.read(sys.stdin.fileno(), 1)
            if not chunk:
                break
            if chunk == b"\x1d":  # Ctrl-] leaves the console
                break
            con.send_data(chunk)

    print("[SOL active - Ctrl-] to exit]", file=sys.stderr)
    threading.Thread(target=pump, daemon=True).start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        con.close()


def build_parser():
    p = argparse.ArgumentParser(
        description="IPMI control for ESMS/Quanta BMC (Python, no Java)")
    p.add_argument("-b", "--bmc", default=DEFAULT_BMC)
    p.add_argument("-u", "--user", default=os.environ.get("ESMS_USER", DEFAULT_USER))
    p.add_argument("-p", "--password", default=None)
    p.add_argument("-P", "--port", type=int, default=int(os.environ.get("ESMS_PORT", 623)))
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="BMC device ID").set_defaults(func=cmd_info)

    bmc = sub.add_parser("bmc", help="BMC reset / raw device ID")
    bmc.add_argument("action", nargs="?", default="info", choices=["info", "reset"])
    bmc.set_defaults(func=cmd_bmc)

    pp = sub.add_parser("power", help="power state / control")
    pp.add_argument("action", nargs="?", default="status",
                    choices=["status", "on", "off", "reset", "cycle",
                             "softoff", "shutdown"])
    pp.add_argument("-w", "--wait", action="store_true", help="wait for transition")
    pp.set_defaults(func=cmd_power)

    sp = sub.add_parser("sensors", help="sensor readings")
    sp.add_argument("-a", "--all", action="store_true", help="include sensors without value")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sensors)

    sl = sub.add_parser("sel", help="system event log")
    sl.add_argument("--clear", action="store_true", help="fetch and clear")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=cmd_sel)

    sub.add_parser("fru", help="FRU inventory").set_defaults(func=cmd_fru)

    bp = sub.add_parser("boot", help="boot device override")
    bp.add_argument("device", nargs="?",
                    choices=["pxe", "net", "hd", "disk", "cd", "dvd", "bios",
                             "setup", "default"])
    bp.add_argument("--persist", action="store_true")
    bp.add_argument("--uefi", action="store_true")
    bp.set_defaults(func=cmd_boot)

    rp = sub.add_parser("raw", help="raw IPMI command")
    rp.add_argument("netfn", type=lambda x: int(x, 0))
    rp.add_argument("command", type=lambda x: int(x, 0))
    rp.add_argument("data", nargs="*", default=[])
    rp.set_defaults(func=cmd_raw)

    sol = sub.add_parser("sol", help="SOL serial console (Ctrl-] to exit)")
    sol.set_defaults(func=cmd_sol)

    return p


def main():
    args = build_parser().parse_args()
    if args.command == "sol":
        args.func(None, args)
        return
    ipmi = connect(args)
    try:
        args.func(ipmi, args)
    except IpmiException as exc:
        sys.exit(f"ipmi error: {exc}")


if __name__ == "__main__":
    main()
