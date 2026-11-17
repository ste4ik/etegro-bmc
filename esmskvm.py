#!/usr/bin/env python3
"""esmskvm - Python client for the Quanta/ESMS Java-RSM KVM protocol.

Implements the parts recovered from JavaRSM.jar:
  * control channel  TCP 8890, packet header "G21" + version + fn(le16) + len(le32) + enc
  * login             fn 0x6000, payload user NUL md5-crypt(password, "xxxxxxxx")
  * video channel     TCP 8900, fn 0x8000, 32-byte frame header + payload
  * RC4               key = authKey repeated to 256 bytes, standard ARC4

Commands:
    login                 authenticate and print the response
    probe [seconds]       login, open video, print frame headers
    capture [n]           save n raw video payloads under bmc/captures/
"""
import argparse
import getpass
import hashlib
import os
import socket
import struct
import sys
import time

HOST = os.environ.get("ESMS_BMC", "")
USER = os.environ.get("ESMS_USER", "root")
PORT_CONTROL = 8890
PORT_VIDEO = 8900
SALT = "xxxxxxxx"

ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# header: "G21" ver fn_lo fn_hi len(le32) enc pad
HDR = struct.Struct("<3sBBBIBB")


def md5crypt(password: str, salt: str = SALT, magic: str = "$1$") -> str:
    """Port of MD5Crypt.crypt from JavaRSM.jar (standard Unix md5-crypt)."""
    pw = password.encode()
    if salt.startswith(magic):
        salt = salt[len(magic):]
    if "$" in salt:
        salt = salt.split("$", 1)[0]
    salt = salt[:8]
    sb = salt.encode()

    ctx = hashlib.md5(pw + magic.encode() + sb)
    ctx1 = hashlib.md5(pw + sb + pw).digest()

    pl = len(pw)
    while pl > 0:
        ctx.update(ctx1[: min(16, pl)])
        pl -= 16

    i = len(pw)
    while i:
        ctx.update(b"\x00" if (i & 1) else pw[:1])
        i >>= 1
    final = ctx.digest()

    for i in range(1000):
        c = hashlib.md5()
        c.update(pw if (i & 1) else final)
        if i % 3:
            c.update(sb)
        if i % 7:
            c.update(pw)
        c.update(final if (i & 1) else pw)
        final = c.digest()

    def to64(v, n):
        out = []
        for _ in range(n):
            out.append(ITOA64[v & 0x3F])
            v >>= 6
        return "".join(out)

    out = [magic, salt, "$"]
    out.append(to64((final[0] << 16) | (final[6] << 8) | final[12], 4))
    out.append(to64((final[1] << 16) | (final[7] << 8) | final[13], 4))
    out.append(to64((final[2] << 16) | (final[8] << 8) | final[14], 4))
    out.append(to64((final[3] << 16) | (final[9] << 8) | final[15], 4))
    out.append(to64((final[4] << 16) | (final[10] << 8) | final[5], 4))
    out.append(to64(final[11], 2))
    return "".join(out)


def auth_key(password: str) -> bytes:
    """HMAC-SHA1(key=first 16 chars of md5crypt, msg=privateKey) -> 16 bytes."""
    private = bytes([43, 78, 24, 88, 95, 84, 16, 56, 97, 13, 2, 44, 56, 57, 7, 29])
    import hmac
    pub = md5crypt(password)[:16].encode()
    return hmac.new(pub, private, hashlib.sha1).digest()[:16]


class RC4:
    def __init__(self, key: bytes):
        self.s = list(range(256))
        self.k = bytes(key[i % len(key)] for i in range(256))
        self.reset()

    def reset(self):
        s, k = self.s, self.k
        j = 0
        for i in range(256):
            j = (j + s[i] + k[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self.i = self.j = 0

    def crypt(self, data: bytes) -> bytes:
        s, i, j = self.s, self.i, self.j
        out = bytearray(len(data))
        for n, b in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


def packet(fn: int, data: bytes = b"", enc: int = 0, length: int = None) -> bytes:
    if length is None:
        length = len(data)
    return HDR.pack(b"G21", 1, fn & 0xFF, (fn >> 8) & 0xFF, length, enc, 0) + data


class Control:
    def __init__(self, host=HOST, timeout=10):
        self.host = host
        self.sock = socket.create_connection((host, PORT_CONTROL), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, fn, data=b"", length=None):
        self.sock.sendall(packet(fn, data, length=length))

    def recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("connection closed")
            buf += chunk
        return bytes(buf)

    def recv(self):
        hdr = self.recv_exact(12)
        sign, ver, fn_lo, fn_hi, length, enc, pad = HDR.unpack(hdr)
        if sign != b"G21":
            raise ValueError(f"bad header {hdr.hex()}")
        fn = fn_lo | (fn_hi << 8)
        data = self.recv_exact(length) if length else b""
        return fn, enc, data, hdr

    def login(self, user, password, mac=None):
        payload = b""
        if mac:
            payload += mac
        payload += user.encode() + b"\x00" + md5crypt(password).encode()
        self.send(0x6000, payload)
        fn, enc, data, hdr = self.recv()
        return fn, enc, data, hdr

    def close(self):
        try:
            self.send(0x8800, length=0)
        except OSError:
            pass
        self.sock.close()


class Video:
    def __init__(self, host=HOST, timeout=10):
        self.sock = socket.create_connection((host, PORT_VIDEO), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def recv_frame(self):
        hdr = self.recv_exact(12)
        sign, ver, fn_lo, fn_hi, length, enc, pad = HDR.unpack(hdr)
        if sign != b"G21":
            raise ValueError(f"bad video header {hdr.hex()}")
        fn = fn_lo | (fn_hi << 8)
        data = self.recv_exact(length) if length else b""
        return fn, enc, data

    def recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("video closed")
            buf += chunk
        return bytes(buf)

    def close(self):
        self.sock.close()


def parse_frame_header(data):
    if len(data) < 32:
        return None
    (start, frameno, nblocks) = struct.unpack_from("<III", data, 0)
    (h, v, sh, sv) = struct.unpack_from("<HHHH", data, 12)
    return {
        "start": start, "frame": frameno, "blocks": nblocks,
        "h": h, "v": v, "src_h": sh, "src_v": sv,
        "compression": data[20], "jpeg_scale": data[21],
        "y_table": data[22], "uv_table": data[23], "yuv_map": data[24],
        "adv_table": data[25], "adv_scale": data[26],
        "rc4_enable": data[27], "rc4_reset": data[28],
        "mode420": data[29], "mode_vq": data[30], "whole": data[31],
    }


def get_password(args):
    pw = args.password or os.environ.get("ESMS_PASS")
    if not pw and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                pw, _ = winreg.QueryValueEx(key, "ESMS_PASS")
        except OSError:
            pw = None
    if not pw:
        pw = getpass.getpass(f"BMC password for {args.user}@{args.bmc}: ")
    return pw


def parse_mac(value):
    if not value:
        return None
    cleaned = value.replace(":", "").replace("-", "").replace(".", "")
    return bytes.fromhex(cleaned)


def cmd_login(args):
    pw = get_password(args)
    c = Control(args.bmc)
    fn, enc, data, hdr = c.login(args.user, pw, parse_mac(args.mac))
    print(f"response fn=0x{fn:04x} enc={enc} data={data.hex()}")
    if fn == 0x6001 and data and data[0] == 1:
        print(f"LOGIN OK user_id={data[1] if len(data) > 1 else '?'}")
        print(f"authKey={auth_key(pw).hex()}")
    else:
        print("LOGIN FAILED")
    c.close()
    return 0 if (fn == 0x6001 and data and data[0] == 1) else 1


def cmd_probe(args):
    pw = get_password(args)
    c = Control(args.bmc)
    fn, enc, data, _ = c.login(args.user, pw, parse_mac(args.mac))
    if fn != 0x6001 or not data or data[0] != 1:
        sys.exit(f"login failed: fn=0x{fn:04x} data={data.hex()}")
    key = auth_key(pw)
    print(f"logged in, authKey={key.hex()}")
    # ask the BMC for video
    c.send(0x8010, length=0)   # auto detect
    c.send(0x8011, length=0)   # refresh
    c.sock.settimeout(2)
    v = Video(args.bmc)
    v.sock.settimeout(args.timeout)
    deadline = time.time() + args.timeout
    rc4 = None
    n = 0
    try:
        while time.time() < deadline:
            fn, enc, data = v.recv_frame()
            if fn != 0x8000:
                print(f"non-video packet fn=0x{fn:04x} len={len(data)}")
                continue
            fh = parse_frame_header(data)
            n += 1
            print(f"frame {n}: enc={enc} len={len(data)} {fh}")
            if fh and fh["rc4_enable"] and fh["rc4_reset"]:
                rc4 = RC4(key)
                print("RC4 reinit from authKey")
                if args.save:
                    dec = rc4.crypt(data[32:])
                    print(f"  rc4-decrypted first bytes: {dec[:16].hex()}")
    finally:
        v.close()
        c.close()


def cmd_capture(args):
    pw = get_password(args)
    c = Control(args.bmc)
    fn, enc, data, _ = c.login(args.user, pw, parse_mac(args.mac))
    if fn != 0x6001 or not data or data[0] != 1:
        sys.exit(f"login failed: fn=0x{fn:04x} data={data.hex()}")
    key = auth_key(pw)
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bmc", "captures")
    os.makedirs(outdir, exist_ok=True)
    c.send(0x8010, length=0)
    c.send(0x8011, length=0)
    v = Video(args.bmc)
    v.sock.settimeout(10)
    rc4 = None
    saved = 0
    while saved < args.count:
        fn, enc, data = v.recv_frame()
        if fn != 0x8000 or len(data) < 32:
            continue
        fh = parse_frame_header(data)
        if fh["rc4_enable"] and fh["rc4_reset"]:
            rc4 = RC4(key)
        name = os.path.join(outdir, f"frame_{saved:03d}_{fh['h']}x{fh['v']}_raw.bin")
        with open(name, "wb") as fh_out:
            fh_out.write(data)
        if rc4 and fh["rc4_enable"]:
            dec = rc4.crypt(data[32:])
            with open(name.replace("_raw", "_rc4"), "wb") as fh_out:
                fh_out.write(data[:32] + dec)
        print(f"saved {name} len={len(data)} {fh}")
        saved += 1
    v.close()
    c.close()


def cmd_keys(args):
    """Send a single key press/release (USB HID usage id)."""
    pw = get_password(args)
    c = Control(args.bmc)
    fn, enc, data, _ = c.login(args.user, pw, parse_mac(args.mac))
    if fn != 0x6001 or not data or data[0] != 1:
        sys.exit("login failed")
    for byte in args.bytes:
        c.send(0x8100, bytes([int(byte, 0) & 0xFF]) * 8)
        print(f"sent keyboard {byte}")
    c.close()


def main():
    p = argparse.ArgumentParser(description="ESMS/Quanta Java-RSM KVM client")
    p.add_argument("-b", "--bmc", default=HOST)
    p.add_argument("-u", "--user", default=USER)
    p.add_argument("-p", "--password")
    p.add_argument("-m", "--mac", default=os.environ.get("ESMS_MAC", ""),
                   help="6-byte server MAC for MAC-validated login")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("login")
    lp.set_defaults(func=cmd_login)

    pr = sub.add_parser("probe")
    pr.add_argument("--timeout", type=float, default=15)
    pr.add_argument("--save", action="store_true")
    pr.set_defaults(func=cmd_probe)

    cp = sub.add_parser("capture")
    cp.add_argument("count", nargs="?", type=int, default=5)
    cp.set_defaults(func=cmd_capture)


    kp = sub.add_parser("keys")
    kp.add_argument("bytes", nargs="+")
    kp.set_defaults(func=cmd_keys)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()


