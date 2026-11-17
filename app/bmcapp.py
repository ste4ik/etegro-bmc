#!/usr/bin/env python3
"""etegro BMC control panel - Flask backend + KVM bridge."""
import io
import json
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import esmskvm as K  # noqa: E402

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SHOTS_DIR = os.path.join(APP_DIR, "static", "shots")
os.makedirs(SHOTS_DIR, exist_ok=True)

DIST_ROOT = os.path.dirname(APP_DIR)
RUNTIME_DIR = os.path.join(DIST_ROOT, "runtime")
VENDOR_DIR = os.path.join(DIST_ROOT, "vendor")

# Development-layout fallbacks (the working tree used while reverse engineering).
LEGACY_JRE = os.path.join(DIST_ROOT, "tools", "jre8x86")
LEGACY_JAR = os.path.join(DIST_ROOT, "bmc", "JavaRSM.jar")
LEGACY_DLL = os.path.join(DIST_ROOT, "bmc", "jarx", "lib", "jni", "video",
                          "libWindowsVDecode.dll")

DEFAULTS = {
    "host": "",
    "user": "root",
    "mac": "",
    "quality": 3,
}


def find_java():
    for base in (RUNTIME_DIR, LEGACY_JRE):
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            if os.path.basename(root) == "bin" and "java.exe" in files:
                return os.path.join(root, "java.exe")
    return None


def find_vendor():
    jar = None
    dll = None
    for candidate in (os.path.join(VENDOR_DIR, "JavaRSM.jar"), LEGACY_JAR):
        if os.path.isfile(candidate):
            jar = candidate
            break
    for candidate in (os.path.join(VENDOR_DIR, "libWindowsVDecode.dll"), LEGACY_DLL):
        if os.path.isfile(candidate):
            dll = candidate
            break
    return jar, dll


CLASSES = os.path.join(APP_DIR, "decoder", "classes")
DECODER_PORT = 18902


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def read_password():
    pw = os.environ.get("ESMS_PASS")
    if pw:
        return pw
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                pw, _ = winreg.QueryValueEx(key, "ESMS_PASS")
                return pw
        except OSError:
            pass
    return None


def field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------- IPMI

_ipmi_lock = threading.Lock()
_cache = {}
_cache_lock = threading.Lock()


def _cached(name, ttl, producer):
    with _cache_lock:
        entry = _cache.get(name)
        if entry and time.time() - entry[0] < ttl:
            return entry[1]
    value = producer()
    with _cache_lock:
        _cache[name] = (time.time(), value)
    return value


class Bmc:
    def __init__(self):
        self.config = load_config()
        self.password = read_password()
        self.cmd = None
        self.cmd_lock = threading.Lock()

    def _command(self):
        from pyghmi.ipmi import command as ipmi_command
        if self.cmd is None:
            self.cmd = ipmi_command.Command(bmc=self.config["host"],
                                            userid=self.config["user"],
                                            password=self.password,
                                            keepalive=False)
        return self.cmd

    def reset_session(self):
        with self.cmd_lock:
            cmd, self.cmd = self.cmd, None
        if cmd is not None:
            try:
                cmd.ipmi_session.logout()
            except Exception:  # noqa: BLE001
                pass

    def call(self, fn, timeout=15):
        """Run one IPMI call on the shared session, with a hard response limit."""
        if not self.password:
            raise RuntimeError("BMC password is not set")
        result = {}

        def worker():
            cmd = None
            try:
                with self.cmd_lock:
                    cmd = self._command()
                    result["value"] = fn(cmd)
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc
                with self.cmd_lock:
                    if self.cmd is cmd:
                        self.cmd = None
                try:
                    if cmd is not None:
                        cmd.ipmi_session.logout()
                except Exception:  # noqa: BLE001
                    pass

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("IPMI call timed out")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def power(self):
        def producer():
            try:
                return self.call(lambda c: c.get_power().get("powerstate", "unknown"))
            except Exception as exc:  # noqa: BLE001
                return f"error: {exc}"
        return _cached("power", 3, producer)

    def sensors(self):
        def producer():
            try:
                rows = []
                for sensor in self.call(lambda c: list(c.get_sensor_data())):
                    value = field(sensor, "value")
                    rows.append({
                        "name": field(sensor, "name", "").replace("\x00", "").strip(),
                        "value": value,
                        "units": field(sensor, "units", ""),
                        "health": field(sensor, "health", 0),
                        "type": field(sensor, "type", ""),
                    })
                return rows
            except Exception as exc:  # noqa: BLE001
                return [{"name": "error", "value": None, "units": "", "health": 3,
                         "type": str(exc)}]
        return _cached("sensors", 5, producer)

    def sel(self, limit=80):
        def producer():
            try:
                events = []
                for event in self.call(lambda c: list(c.get_event_log())):
                    severity = field(event, "severity", 0)
                    if isinstance(severity, int):
                        severity = {0: "Info", 1: "Warning", 2: "Critical"}.get(
                            severity, str(severity))
                    events.append({
                        "timestamp": field(event, "timestamp", "") or field(event, "time", ""),
                        "severity": severity,
                        "description": field(event, "description", "") or field(event, "event", ""),
                    })
                return events[-limit:][::-1]
            except Exception as exc:  # noqa: BLE001
                return [{"timestamp": "", "severity": "error", "description": str(exc)}]
        return _cached("sel", 10, producer)

    def fru(self):
        def producer():
            try:
                rows = []
                for name, info in self.call(lambda c: list(c.get_inventory())):
                    if not info:
                        continue
                    rows.append({"name": name, "info": {k: str(v) for k, v in info.items()
                                                        if v not in (None, "")}})
                return rows
            except Exception as exc:  # noqa: BLE001
                return [{"name": "error", "info": {"error": str(exc)}}]
        return _cached("fru", 60, producer)

    def power_action(self, action):
        mapping = {"on": "on", "off": "off", "reset": "reset", "cycle": "cycle",
                   "shutdown": "shutdown"}
        if action not in mapping:
            raise ValueError("unknown power action")
        result = self.call(lambda c: c.set_power(mapping[action]))
        with _cache_lock:
            _cache.pop("power", None)
        return result.get("powerstate", mapping[action])

    def set_boot(self, device):
        mapping = {"pxe": "network", "net": "network", "hd": "hd", "disk": "hd",
                   "cd": "optical", "dvd": "optical", "bios": "setup", "setup": "setup",
                   "default": "default"}
        if device not in mapping:
            raise ValueError("unknown boot device")
        return str(self.call(lambda c: c.set_bootdev(mapping[device])))

    def reset_bmc(self):
        result = self.call(lambda c: c.raw_command(netfn=0x06, command=0x02))
        self.reset_session()
        return str(result)

    def bmc_info(self):
        def producer():
            try:
                raw = self.call(lambda c: c.raw_command(netfn=0x06, command=0x01))
                data = bytes(raw.get("data", b""))
                major = data[2] if len(data) > 2 else 0
                minor = data[3] if len(data) > 3 else 0
                return {"firmware": f"{major}.{minor:02x}", "raw": data.hex()}
            except Exception as exc:  # noqa: BLE001
                return {"firmware": "unknown", "raw": str(exc)}
        return _cached("bmc_info", 300, producer)


bmc = Bmc()


# ---------------------------------------------------------------- decoder

class Decoder:
    def __init__(self, port=DECODER_PORT):
        self.port = port
        self.sock = None
        self.lock = threading.Lock()
        self.proc = None

    def _port_open(self):
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=0.5)
            s.close()
            return True
        except OSError:
            return False

    def _start_server(self):
        if self.proc and self.proc.poll() is None:
            return
        java = find_java()
        jar, dll = find_vendor()
        if not java or not jar or not dll or not os.path.isdir(CLASSES):
            raise RuntimeError("decoder files are missing: run setup.py first")
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [java, "-cp", CLASSES + ";" + jar, "DecoderServer", dll, str(self.port)],
            cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags)
        for _ in range(40):
            if self._port_open():
                return
            time.sleep(0.25)
        raise RuntimeError("decoder server did not start")

    def running(self):
        return self.sock is not None or self._port_open()

    def decode(self, frame):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        if not self._port_open():
                            self._start_server()
                        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.sock.sendall(struct.pack(">i", len(frame)) + frame)
                    header = self._recv_exact(12)
                    width, height, boxes = struct.unpack(">iii", header)
                    bgr = self._recv_exact(width * height * 3)
                    return width, height, boxes, bgr
                except OSError:
                    try:
                        if self.sock:
                            self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
                    if attempt:
                        raise

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(min(1 << 20, n - len(buf)))
            if not chunk:
                raise OSError("decoder closed")
            buf += chunk
        return bytes(buf)


# ---------------------------------------------------------------- KVM

class KvmSession:
    def __init__(self, bmc_obj):
        self.bmc = bmc_obj
        self.decoder = Decoder()
        self.control = None
        self.video = None
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.cond = threading.Condition()
        self.running = False
        self.connected = False
        self.error = ""
        self.frame = None
        self.frame_seq = 0
        self.frame_times = deque(maxlen=30)
        self.last_frame = 0.0
        self.keyboard_led = 0
        self.thread = None
        self.reader_thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, name="kvm", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self._close()

    def _close(self):
        for sock in (self.control.sock if self.control else None,
                     self.video.sock if self.video else None):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass
        self.control = None
        self.video = None
        self.connected = False

    def _connect(self):
        cfg = self.bmc.config
        if not self.bmc.password:
            raise RuntimeError("BMC password is not set")
        self._close()
        self.control = K.Control(cfg["host"], timeout=8)
        fn, enc, data, _ = self.control.login(cfg["user"], self.bmc.password,
                                              K.parse_mac(cfg["mac"]))
        if fn != 0x6001 or not data or data[0] != 1:
            raise RuntimeError(f"KVM login failed (0x{fn:04x} {data.hex()})")
        with self.send_lock:
            self.control.send(0x8301, bytes([0]) + (106).to_bytes(4, "little"))
        time.sleep(0.25)
        levels = {0: bytes([0, 0, 1]), 1: bytes([6, 6, 1]),
                  2: bytes([11, 11, 1]), 3: bytes([11, 11, 0])}
        quality = int(cfg.get("quality", 3))
        with self.send_lock:
            self.control.send(0x8015, levels.get(quality, levels[3]))
        self.video = K.Video(cfg["host"])
        self.video.sock.settimeout(1.0)
        with self.send_lock:
            self.control.send(0x8202, b"")  # absolute mouse
            self.control.send(0x8010, b"")  # auto detect
            self.control.send(0x8011, b"")  # refresh
        self.connected = True
        self.error = ""
        self.reader_thread = threading.Thread(target=self._control_reader, name="kvm-ctrl",
                                              daemon=True)
        self.reader_thread.start()
        threading.Thread(target=self._heartbeat, name="kvm-heart", daemon=True).start()

    def _control_reader(self):
        control = self.control
        sock = control.sock if control else None
        while self.running and sock and control is self.control:
            try:
                r, _, _ = select.select([sock], [], [], 1.0)
                if not r:
                    continue
                fn, enc, data, _ = control.recv()
                if fn == 0x8101 and data:
                    self.keyboard_led = data[0]
            except (OSError, EOFError, ValueError, TimeoutError):
                if self.running:
                    self.connected = False
                    self.error = "control channel closed"
                return

    def _heartbeat(self):
        while self.running and self.connected:
            try:
                with self.send_lock:
                    if self.control:
                        self.control.send(0x8801, length=0)
            except OSError:
                return
            time.sleep(5)

    def _run(self):
        while self.running:
            try:
                if not self.connected:
                    self._connect()
                try:
                    fn, enc, data = self.video.recv_frame()
                except socket.timeout:
                    continue
                if fn != 0x8000 or len(data) < 32:
                    continue
                width, height, boxes, bgr = self.decoder.decode(data)
                now = time.time()
                with self.cond:
                    self.frame = (width, height, bgr)
                    self.frame_seq += 1
                    self.last_frame = now
                    self.frame_times.append(now)
                    self.cond.notify_all()
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.error = str(exc)
                self._close()
                time.sleep(2.0)

    def send_key(self, mods, keys):
        report = bytes([mods & 0xFF, 0] + [k & 0xFF for k in keys[:6]] + [0] * (6 - len(keys[:6])))
        with self.send_lock:
            if self.control:
                self.control.send(0x8100, report)

    def send_mouse_abs(self, buttons, x_norm, y_norm):
        x = max(0, min(32767, int(x_norm * 32767 + 0.5)))
        y = max(0, min(32767, int(y_norm * 32767 + 0.5)))
        report = bytes([buttons & 0xFF, 0, x & 0xFF, (x >> 8) & 0xFF,
                        y & 0xFF, (y >> 8) & 0xFF, 0, 0])
        with self.send_lock:
            if self.control:
                self.control.send(0x8200, report)

    def send_mouse_rel(self, buttons, dx, dy, wheel):
        report = bytes([buttons & 0xFF, 0, dx & 0xFF, 0, dy & 0xFF, 0,
                        wheel & 0xFF, (wheel & 0xFF) if wheel < 0 else 0])
        with self.send_lock:
            if self.control:
                self.control.send(0x8200, report)

    def ctrl_alt_del(self):
        self.send_key(5, [0x4C])
        time.sleep(0.05)
        self.send_key(0, [])

    def screenshot(self):
        with self.cond:
            frame = self.frame
        if not frame:
            return None
        width, height, bgr = frame
        name = f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        path = os.path.join(SHOTS_DIR, name)
        Image.frombytes("RGB", (width, height), bgr, "raw", "BGR").save(path)
        return f"/shots/{name}"

    def status(self):
        with self.cond:
            seq = self.frame_seq
            frame = self.frame
            last = self.last_frame
            times = list(self.frame_times)
        fps = 0.0
        if len(times) > 1:
            span = times[-1] - times[0]
            if span > 0:
                fps = (len(times) - 1) / span
        return {
            "connected": self.connected,
            "error": self.error,
            "seq": seq,
            "width": frame[0] if frame else 0,
            "height": frame[1] if frame else 0,
            "last_frame_age": round(time.time() - last, 1) if last else None,
            "fps": round(fps, 1),
            "keyboard_led": self.keyboard_led,
        }


kvm = KvmSession(bmc)


# ---------------------------------------------------------------- images

_placeholder_lock = threading.Lock()
_placeholder_cache = {}


def placeholder_jpeg(title, subtitle):
    key = (title, subtitle)
    with _placeholder_lock:
        if key in _placeholder_cache:
            return _placeholder_cache[key]
    width, height = 1024, 768
    img = Image.new("RGB", (width, height), (8, 10, 12))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(30, 35, 40))
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 30)
        small = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
        small = font
    tw = draw.textlength(title, font=font)
    draw.text(((width - tw) / 2, height / 2 - 34), title, font=font, fill=(160, 168, 176))
    sw = draw.textlength(subtitle, font=small)
    draw.text(((width - sw) / 2, height / 2 + 12), subtitle, font=small, fill=(92, 100, 108))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    data = buf.getvalue()
    with _placeholder_lock:
        _placeholder_cache[key] = data
    return data


_jpeg_lock = threading.Lock()
_jpeg_cache = {"seq": -1, "data": None}


def frame_jpeg(seq, frame):
    with _jpeg_lock:
        if _jpeg_cache["seq"] == seq and _jpeg_cache["data"]:
            return _jpeg_cache["data"]
    width, height, bgr = frame
    img = Image.frombytes("RGB", (width, height), bgr, "raw", "BGR")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=72)
    data = buf.getvalue()
    with _jpeg_lock:
        _jpeg_cache.update(seq=seq, data=data)
    return data


def mjpeg_stream():
    last_seq = -1
    while True:
        with kvm.cond:
            kvm.cond.wait(timeout=0.5)
            seq, frame = kvm.frame_seq, kvm.frame
        if frame and seq != last_seq:
            data = frame_jpeg(seq, frame)
            last_seq = seq
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
        else:
            if frame:
                continue
            data = placeholder_jpeg("No video signal",
                                    "BMC video is not responding. Reset the BMC and retry.")
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
            time.sleep(1.0)


# ---------------------------------------------------------------- web app

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.after_request
def no_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/video")
def video():
    return Response(mjpeg_stream(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/shots/<path:name>")
def shots(name):
    return send_from_directory(SHOTS_DIR, name)


@app.route("/api/status")
def api_status():
    config = bmc.config
    sensors = bmc.sensors()
    temps = {s["name"]: s for s in sensors}
    return jsonify({
        "host": config["host"],
        "user": config["user"],
        "mac": config["mac"],
        "password_set": bool(bmc.password),
        "power": bmc.power(),
        "bmc": bmc.bmc_info(),
        "kvm": kvm.status(),
        "decoder": {"running": kvm.decoder.running()},
        "temps": {
            "cpu1": temps.get("CPU1 Temp"),
            "cpu2": temps.get("CPU2 Temp"),
            "mb": temps.get("MB Temp"),
        },
    })


@app.route("/api/sensors")
def api_sensors():
    return jsonify({"sensors": bmc.sensors(), "updated": time.time()})


@app.route("/api/sel")
def api_sel():
    return jsonify({"events": bmc.sel()})


@app.route("/api/fru")
def api_fru():
    return jsonify({"inventory": bmc.fru()})


@app.route("/api/power", methods=["POST"])
def api_power():
    payload = request.get_json(force=True, silent=True) or {}
    action = payload.get("action", "")
    try:
        state = bmc.power_action(action)
        return jsonify({"ok": True, "power": state})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/boot", methods=["POST"])
def api_boot():
    payload = request.get_json(force=True, silent=True) or {}
    device = payload.get("device", "")
    try:
        return jsonify({"ok": True, "result": bmc.set_boot(device)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/bmc/reset", methods=["POST"])
def api_bmc_reset():
    try:
        bmc.reset_bmc()
        with _cache_lock:
            _cache.clear()
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/kvm/key", methods=["POST"])
def api_kvm_key():
    payload = request.get_json(force=True, silent=True) or {}
    mods = int(payload.get("mods", 0))
    keys = [int(k) for k in payload.get("keys", [])][:6]
    kvm.send_key(mods, keys)
    return jsonify({"ok": True})


@app.route("/api/kvm/mouse", methods=["POST"])
def api_kvm_mouse():
    payload = request.get_json(force=True, silent=True) or {}
    buttons = int(payload.get("buttons", 0))
    if payload.get("mode") == "rel":
        kvm.send_mouse_rel(buttons, int(payload.get("dx", 0)),
                           int(payload.get("dy", 0)), int(payload.get("wheel", 0)))
    else:
        kvm.send_mouse_abs(buttons, float(payload.get("x", 0.5)),
                           float(payload.get("y", 0.5)))
    return jsonify({"ok": True})


@app.route("/api/kvm/cad", methods=["POST"])
def api_kvm_cad():
    kvm.ctrl_alt_del()
    return jsonify({"ok": True})


@app.route("/api/kvm/screenshot", methods=["POST"])
def api_kvm_screenshot():
    url = kvm.screenshot()
    if not url:
        return jsonify({"ok": False, "error": "no frame received yet"}), 400
    return jsonify({"ok": True, "url": url})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        cfg = dict(bmc.config)
        return jsonify({"config": cfg, "password_set": bool(bmc.password)})
    payload = request.get_json(force=True, silent=True) or {}
    for key in ("host", "user", "mac"):
        if payload.get(key):
            bmc.config[key] = str(payload[key]).strip()
    if "quality" in payload:
        try:
            bmc.config["quality"] = int(payload["quality"])
        except (TypeError, ValueError):
            pass
    if payload.get("password"):
        bmc.password = str(payload["password"])
    save_config(bmc.config)
    with _cache_lock:
        _cache.clear()
    kvm.stop()
    time.sleep(0.2)
    kvm.start()
    return jsonify({"ok": True})


@app.route("/api/reconnect", methods=["POST"])
def api_reconnect():
    kvm.stop()
    time.sleep(0.2)
    kvm.start()
    return jsonify({"ok": True})


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()
    kvm.start()
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=args.port, threaded=True,
                                            use_reloader=False, debug=False),
                     daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/"
    if args.no_window:
        print(url)
        while True:
            time.sleep(1)
    try:
        import webview
        webview.create_window("etegro BMC", url, width=1480, height=940,
                              min_size=(1080, 680), background_color="#0c0e10")
        webview.start()
    except Exception:
        webbrowser.open(url)
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
