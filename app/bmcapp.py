#!/usr/bin/env python3
"""etegro BMC control panel - Flask backend + KVM bridge."""
import base64
import io
import json
import os
import re
import select
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from collections import deque

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageDraw, ImageFont

# Frozen builds put read-only resources in _MEIPASS and keep config, runtime and
# vendor files next to the executable.
FROZEN = bool(getattr(sys, "frozen", False))
WORKER_MODE = "--ipmi-worker" in sys.argv
if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
    DIST_ROOT = APP_DIR
elif "__file__" in globals():
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = APP_DIR
    DIST_ROOT = os.path.dirname(APP_DIR)
else:  # PyInstaller --onefile entry without __file__
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
    DIST_ROOT = APP_DIR

MODULE_DIR = RESOURCE_DIR if FROZEN else DIST_ROOT
BASE = DIST_ROOT
sys.path.insert(0, MODULE_DIR)
import esmskvm as K  # noqa: E402

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SHOTS_DIR = (os.path.join(APP_DIR, "shots") if FROZEN
             else os.path.join(APP_DIR, "static", "shots"))
os.makedirs(SHOTS_DIR, exist_ok=True)

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


DLL_IN_JAR = "lib/jni/video/libWindowsVDecode.dll"


def _legacy_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except (AttributeError, ValueError, ssl.SSLError):
        pass
    return ctx


def ensure_vendor(host):
    """Fetch the BMC's own decoder from the appliance itself, once.

    The vendor binaries are not part of this project. The BMC serves its own
    JavaRSM.jar without a login; the decoder DLL lives inside it.
    """
    jar_path = os.path.join(VENDOR_DIR, "JavaRSM.jar")
    dll_path = os.path.join(VENDOR_DIR, "libWindowsVDecode.dll")
    if os.path.isfile(jar_path) and os.path.isfile(dll_path):
        return True
    os.makedirs(VENDOR_DIR, exist_ok=True)
    ctx = _legacy_ssl_context()
    headers = {"User-Agent": "etegro-bmc"}
    data = None
    for url in (f"https://{host}/lib/rsm/JavaRSM.jar",
                f"http://{host}/lib/rsm/JavaRSM.jar",
                f"https://{host}/JavaRSM.jar"):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                payload = resp.read()
            if payload[:2] == b"PK" and len(payload) > 100_000:
                data = payload
                break
        except Exception:  # noqa: BLE001
            continue
    if not data:
        raise RuntimeError("Could not download the video codec from the BMC")
    with open(jar_path, "wb") as fh:
        fh.write(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as jar:
            dll = jar.read(DLL_IN_JAR)
    except KeyError as exc:
        raise RuntimeError("The video decoder is missing from JavaRSM.jar") from exc
    with open(dll_path, "wb") as fh:
        fh.write(dll)
    return True


CLASSES = os.path.join(RESOURCE_DIR if FROZEN else APP_DIR, "decoder", "classes")
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


# ---------------------------------------------------------------- credentials

def protect_secret(secret):
    """Encrypt with the current Windows user's DPAPI key. None when unavailable."""
    if os.name != "nt" or not secret:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        def make(data):
            buf = ctypes.create_string_buffer(data)
            return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

        in_blob, in_buf = make(secret.encode("utf-8"))
        out_blob = DATA_BLOB()
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(in_blob), None, None, None, None, 0x1,
                ctypes.byref(out_blob)):
            return None
        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
        return base64.b64encode(raw).decode("ascii")
    except Exception:  # noqa: BLE001
        return None


def unprotect_secret(token):
    if os.name != "nt" or not token:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        def make(data):
            buf = ctypes.create_string_buffer(data)
            return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

        raw = base64.b64decode(token)
        in_blob, in_buf = make(raw)
        out_blob = DATA_BLOB()
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0x1,
                ctypes.byref(out_blob)):
            return None
        try:
            data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
        return data.decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


def arp_mac(host):
    try:
        if os.name == "nt":
            subprocess.run(["ping", "-n", "1", "-w", "800", host],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out = subprocess.run(["arp", "-a", host], capture_output=True, text=True,
                                 timeout=6).stdout
        else:
            subprocess.run(["ping", "-c", "1", "-W", "1", host],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out = subprocess.run(["ip", "neigh", "show", host], capture_output=True,
                                 text=True, timeout=6).stdout
        match = re.search(r"([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", out)
        return match.group(1).replace("-", ":").lower() if match else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def local_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""


def discover_bmcs(timeout=2.5, extra_host=None):
    packet = bytearray(24)
    packet[0:3] = b"G21"
    packet[3] = ord("D")
    packet[4] = 1
    packet[6] = 1
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    except (AttributeError, OSError):
        pass
    try:
        sock.bind(("", 2000))
    except OSError as exc:
        sock.close()
        raise RuntimeError("UDP port 2000 is busy; enter the address manually") from exc
    sock.settimeout(0.2)
    targets = [("255.255.255.255", 2000), ("225.0.0.3", 5000)]
    ip = local_ip()
    if ip and not ip.startswith("127."):
        parts = ip.split(".")
        if len(parts) == 4:
            prefix = ".".join(parts[:3])
            targets.extend((f"{prefix}.{i}", 2000) for i in range(1, 255))
    if extra_host:
        targets.append((extra_host, 2000))
    for target in targets:
        try:
            sock.sendto(bytes(packet), target)
        except OSError:
            pass
    found = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except ConnectionResetError:
            continue
        except OSError:
            break
        if len(data) < 192 or data[:4] != b"G21D" or data[6] != 1:
            continue
        ip_bytes = data[166:170]
        mac_bytes = data[160:166]
        if len(ip_bytes) != 4 or len(mac_bytes) != 6:
            continue
        host_ip = ".".join(str(b) for b in ip_bytes)
        mac = ":".join(f"{b:02x}" for b in mac_bytes)
        hostname = data[32:160].split(b"\0")[0].decode("utf-8", "replace").strip()
        found[host_ip] = {"ip": host_ip, "mac": mac,
                          "hostname": hostname or "BMC"}
    sock.close()
    return sorted(found.values(), key=lambda item: item["ip"])


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


class IpmiWorker:
    """One pyghmi session in its own process, spoken to over localhost TCP."""

    def __init__(self, host, user, password):
        env = dict(os.environ)
        env.update({
            "ESMS_WORKER_HOST": host,
            "ESMS_WORKER_USER": user,
            "ESMS_WORKER_PASS": password,
        })
        creationflags = 0x08000000 if os.name == "nt" else 0
        self.lock = threading.Lock()
        self.port = self._free_port()
        if FROZEN:
            command = [sys.executable, "--ipmi-worker", "--port", str(self.port)]
        else:
            command = [sys.executable, os.path.join(APP_DIR, "ipmi_worker.py"),
                       "--port", str(self.port)]
        self.sock = None
        self.reader = None
        self.proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, cwd=APP_DIR,
            creationflags=creationflags)
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                break
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.15)
        if self.sock is None:
            self.stop()
            raise RuntimeError("IPMI worker did not start")
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")

    @staticmethod
    def _free_port():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
        finally:
            probe.close()

    def request(self, command, timeout=20, **payload):
        with self.lock:
            if not self.sock or not self.reader:
                raise RuntimeError("IPMI worker is not running")
            request = {"cmd": command}
            request.update(payload)
            try:
                self.sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            except OSError as exc:
                raise RuntimeError(f"IPMI worker write failed: {exc}") from exc
            self.sock.settimeout(timeout)
            try:
                line = self.reader.readline()
            except socket.timeout as exc:
                raise TimeoutError("IPMI call timed out") from exc
            except OSError as exc:
                raise RuntimeError(f"IPMI worker read failed: {exc}") from exc
            if not line:
                raise RuntimeError("IPMI worker closed the connection")
            try:
                response = json.loads(line)
            except ValueError as exc:
                raise RuntimeError("bad IPMI worker response") from exc
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "IPMI error"))
            return response.get("data")

    def stop(self):
        with self.lock:
            proc = self.proc
            self.proc = None
            reader, sock = self.reader, self.sock
            self.reader = None
            self.sock = None
            for stream in (reader, sock):
                try:
                    if stream:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass
            if not proc:
                return
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass


class Bmc:
    def __init__(self):
        self.config = load_config()
        self.password = (read_password()
                         or unprotect_secret(self.config.get("password_enc")))
        self.worker = None
        self.worker_error = ""
        if self.config.get("host") and self.password:
            try:
                self.start_worker()
            except Exception as exc:  # noqa: BLE001
                self.worker_error = str(exc)

    def start_worker(self, host=None, user=None, password=None):
        self.stop_worker()
        host = host or self.config.get("host")
        user = user or self.config.get("user", "root")
        password = password or self.password
        if not host:
            raise RuntimeError("BMC address is not set")
        if not password:
            raise RuntimeError("BMC password is not set")
        worker = IpmiWorker(host, user, password)
        try:
            worker.request("power", timeout=20)
        except Exception:
            worker.stop()
            raise
        self.worker = worker
        self.worker_error = ""
        return True

    def stop_worker(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

    def request(self, command, timeout=20, **payload):
        if self.worker is None:
            try:
                self.start_worker()
            except Exception as exc:  # noqa: BLE001
                self.worker_error = str(exc)
                raise RuntimeError(str(exc)) from exc
        try:
            return self.worker.request(command, timeout=timeout, **payload)
        except Exception as exc:  # noqa: BLE001
            self.worker_error = str(exc)
            self.stop_worker()
            raise

    def connect(self, host, user, password, mac=""):
        previous = (self.config.get("host"), self.config.get("user"), self.password)
        self.stop_worker()
        worker = None
        try:
            worker = IpmiWorker(host, user, password)
            power = worker.request("power", timeout=20)
            mac_from_ipmi = ""
            try:
                mac_from_ipmi = worker.request("net", timeout=12).get("mac", "")
            except Exception:  # noqa: BLE001
                pass
            mac = mac or mac_from_ipmi or arp_mac(host)
            if not mac:
                raise RuntimeError("Could not detect the server MAC. "
                                   "Enter it manually and retry.")
            self.worker = worker
            self.worker_error = ""
            self.config.update(host=host, user=user, mac=mac)
            self.password = password
            return power
        except Exception:
            if worker is not None:
                worker.stop()
            try:
                self.config.update(host=previous[0], user=previous[1])
                self.password = previous[2]
                if previous[0] and previous[2]:
                    self.start_worker(previous[0], previous[1], previous[2])
            except Exception:  # noqa: BLE001
                pass
            raise

    def power(self):
        def producer():
            try:
                return self.request("power")
            except Exception as exc:  # noqa: BLE001
                return f"error: {exc}"
        return _cached("power", 3, producer)

    def sensors(self):
        def producer():
            try:
                return self.request("sensors")
            except Exception as exc:  # noqa: BLE001
                return [{"name": "error", "value": None, "units": "", "health": 3,
                         "type": str(exc)}]
        return _cached("sensors", 5, producer)

    def sel(self, limit=80):
        def producer():
            try:
                events = self.request("sel")
                return events[-limit:][::-1]
            except Exception as exc:  # noqa: BLE001
                return [{"timestamp": "", "severity": "error", "description": str(exc)}]
        return _cached("sel", 10, producer)

    def fru(self):
        def producer():
            try:
                return self.request("fru")
            except Exception as exc:  # noqa: BLE001
                return [{"name": "error", "info": {"error": str(exc)}}]
        return _cached("fru", 60, producer)

    def power_action(self, action):
        mapping = {"on": "on", "off": "off", "reset": "reset", "cycle": "cycle",
                   "shutdown": "shutdown"}
        if action not in mapping:
            raise ValueError("unknown power action")
        result = self.request("power_action", action=mapping[action])
        with _cache_lock:
            _cache.pop("power", None)
        return result

    def set_boot(self, device):
        mapping = {"pxe": "network", "net": "network", "hd": "hd", "disk": "hd",
                   "cd": "optical", "dvd": "optical", "bios": "setup", "setup": "setup",
                   "default": "default"}
        if device not in mapping:
            raise ValueError("unknown boot device")
        return str(self.request("set_boot", device=mapping[device]))

    def reset_bmc(self):
        result = self.request("reset_bmc")
        self.stop_worker()
        return str(result)

    def bmc_info(self):
        def producer():
            try:
                return self.request("info", timeout=15)
            except Exception as exc:  # noqa: BLE001
                return {"firmware": "unknown", "raw": str(exc)}
        return _cached("bmc_info", 300, producer)

bmc = None if WORKER_MODE else Bmc()


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
                    pixels = self._recv_exact(width * height * 3)
                    return width, height, boxes, pixels
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
            if self.running:
                return
            self.thread.join(timeout=2)
        self.running = True
        self.thread = threading.Thread(target=self._run, name="kvm", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self._close()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.thread = None

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
                width, height, boxes, pixels = self.decoder.decode(data)
                now = time.time()
                with self.cond:
                    self.frame = (width, height, pixels)
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
        width, height, pixels = frame
        name = f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        path = os.path.join(SHOTS_DIR, name)
        Image.frombytes("RGB", (width, height), pixels, "raw", "RGB").save(path)
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
    width, height, pixels = frame
    img = Image.frombytes("RGB", (width, height), pixels, "raw", "RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, subsampling=0, optimize=True)
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

app = Flask(__name__,
            static_folder=os.path.join(RESOURCE_DIR, "static"),
            template_folder=os.path.join(RESOURCE_DIR, "templates"))
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


@app.route("/api/discover")
def api_discover():
    try:
        timeout = float(request.args.get("timeout", "2.5"))
    except ValueError:
        timeout = 2.5
    timeout = max(1.0, min(6.0, timeout))
    try:
        devices = discover_bmcs(timeout, bmc.config.get("host"))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"devices": [], "error": str(exc)})
    return jsonify({"devices": devices})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global bmc
    payload = request.get_json(force=True, silent=True) or {}
    host = str(payload.get("host", "")).strip()
    user = str(payload.get("user", "") or "root").strip() or "root"
    password = str(payload.get("password", "") or "")
    if not password:
        password = bmc.password or read_password() or ""
    mac = str(payload.get("mac", "") or "").strip().lower()
    remember = bool(payload.get("remember"))
    if not host:
        return jsonify({"ok": False, "error": "BMC address is required"}), 400
    if not password:
        return jsonify({"ok": False, "error": "Password is required"}), 400
    try:
        power = bmc.connect(host, user, password, mac)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    codec_ok, codec_error = True, ""
    try:
        ensure_vendor(host)
    except Exception as exc:  # noqa: BLE001
        codec_ok, codec_error = False, str(exc)
    if codec_ok and not find_java():
        codec_ok = False
        codec_error = "The 32-bit JRE is missing"
    if remember:
        token = protect_secret(password)
        if token:
            bmc.config["password_enc"] = token
        else:
            bmc.config.pop("password_enc", None)
    else:
        bmc.config.pop("password_enc", None)
    save_config(bmc.config)
    with _cache_lock:
        _cache.clear()
    kvm.bmc = bmc
    kvm.stop()
    time.sleep(0.1)
    kvm.start()
    return jsonify({"ok": True, "mac": bmc.config["mac"], "power": power,
                    "remembered": bool(bmc.config.get("password_enc")),
                    "codec": codec_ok, "codec_error": codec_error})


@app.route("/api/status")
def api_status():
    config = bmc.config
    sensors = bmc.sensors()
    temps = {s["name"]: s for s in sensors}
    return jsonify({
        "host": config["host"],
        "user": config["user"],
        "mac": config["mac"],
        "configured": bool(config.get("host")),
        "has_password": bool(bmc.password),
        "remembered": bool(config.get("password_enc")),
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
    global bmc
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
    bmc.stop_worker()
    try:
        bmc.start_worker()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    kvm.bmc = bmc
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
    if WORKER_MODE:
        import ipmi_worker
        ipmi_worker.main()
        return
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()
    if bmc.config.get("host") and bmc.password:
        kvm.start()
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=args.port, threaded=True,
                                            use_reloader=False, debug=False),
                     daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/"
    if args.no_window:
        try:
            print(url, flush=True)
        except (AttributeError, OSError):
            pass
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
