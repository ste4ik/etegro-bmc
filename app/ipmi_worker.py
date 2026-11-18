#!/usr/bin/env python3
"""One long-lived IPMI session in its own process.

The GUI talks to this over a local TCP socket (or stdin/stdout when no --port
is given), one JSON object per line. Keeping pyghmi in a separate process
means a broken session can be discarded by restarting the worker, without
poisoning the web process.
"""
import json
import os
import socket
import sys


def field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def run_worker(reader, writer):
    host = os.environ.get("ESMS_WORKER_HOST", "")
    user = os.environ.get("ESMS_WORKER_USER", "root")
    password = os.environ.get("ESMS_WORKER_PASS", "")
    from pyghmi.ipmi import command as ipmi_command

    cmd = None

    def ensure():
        nonlocal cmd
        if cmd is None:
            cmd = ipmi_command.Command(bmc=host, userid=user, password=password,
                                       keepalive=False)
        return cmd

    def drop():
        nonlocal cmd
        if cmd is not None:
            try:
                cmd.ipmi_session.logout()
            except Exception:  # noqa: BLE001
                pass
        cmd = None

    def sensors():
        rows = []
        for sensor in ensure().get_sensor_data():
            rows.append({
                "name": str(field(sensor, "name", "")).replace("\x00", "").strip(),
                "value": field(sensor, "value"),
                "units": field(sensor, "units", ""),
                "health": field(sensor, "health", 0),
                "type": field(sensor, "type", ""),
            })
        return rows

    def sel():
        events = []
        for event in ensure().get_event_log():
            severity = field(event, "severity", 0)
            if isinstance(severity, int):
                severity = {0: "Info", 1: "Warning", 2: "Critical"}.get(
                    severity, str(severity))
            events.append({
                "timestamp": field(event, "timestamp", "") or field(event, "time", ""),
                "severity": severity,
                "description": field(event, "description", "") or field(event, "event", ""),
            })
        return events

    def fru():
        rows = []
        for name, info in ensure().get_inventory():
            if not info:
                continue
            rows.append({"name": name,
                         "info": {k: str(v) for k, v in info.items()
                                  if v not in (None, "")}})
        return rows

    def device_info():
        raw = ensure().raw_command(netfn=0x06, command=0x01)
        data = bytes(raw.get("data", b""))
        major = data[2] if len(data) > 2 else 0
        minor = data[3] if len(data) > 3 else 0
        return {"firmware": f"{major}.{minor:02x}", "raw": data.hex()}

    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            action = request.get("cmd")
            if action == "power":
                data = ensure().get_power().get("powerstate", "unknown")
            elif action == "sensors":
                data = sensors()
            elif action == "sel":
                data = sel()
            elif action == "fru":
                data = fru()
            elif action == "info":
                data = device_info()
            elif action == "net":
                data = {"mac": ensure().get_net_configuration().get("mac_address", "")}
            elif action == "power_action":
                result = ensure().set_power(request.get("action", ""))
                data = result.get("powerstate", request.get("action", "")) \
                    if isinstance(result, dict) else str(result)
            elif action == "set_boot":
                data = str(ensure().set_bootdev(request.get("device", "")))
            elif action == "reset_bmc":
                data = str(ensure().raw_command(netfn=0x06, command=0x02))
                drop()
            else:
                raise ValueError(f"unknown command: {action}")
            writer.write(json.dumps({"ok": True, "data": data}) + "\n")
            writer.flush()
        except Exception as exc:  # noqa: BLE001
            drop()
            try:
                writer.write(json.dumps({"ok": False, "error": str(exc)}) + "\n")
                writer.flush()
            except Exception:  # noqa: BLE001
                return


def main():
    try:
        _main()
    except Exception:  # noqa: BLE001
        log = os.environ.get("ESMS_WORKER_LOG")
        if log:
            import traceback
            try:
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write("worker failed\n")
                    traceback.print_exc(file=fh)
            except OSError:
                pass
        raise


def _main():
    port = None
    argv = sys.argv
    for index, argument in enumerate(argv):
        if argument == "--port" and index + 1 < len(argv):
            try:
                port = int(argv[index + 1])
            except ValueError:
                port = None
    if port:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        connection, _ = server.accept()
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = connection.makefile("r", encoding="utf-8", newline="\n")
        writer = connection.makefile("w", encoding="utf-8", newline="\n")
        try:
            run_worker(reader, writer)
        finally:
            try:
                reader.close()
                writer.close()
                connection.close()
                server.close()
            except Exception:  # noqa: BLE001
                pass
    else:
        run_worker(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
