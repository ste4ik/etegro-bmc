#!/usr/bin/env python3
"""One-time setup: fetch the BMC's own video codec and a 32-bit JRE.

The vendor binaries (JavaRSM.jar and its native decoder) are not distributed
with this project. This script downloads them from the BMC you own, then
downloads the Temurin 8 x86 JRE from Adoptium.
"""
import argparse
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.request
import zipfile

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_ROOT = os.path.dirname(APP_DIR)
VENDOR_DIR = os.path.join(DIST_ROOT, "vendor")
RUNTIME_DIR = os.path.join(DIST_ROOT, "runtime")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

JRE_URL = ("https://api.adoptium.net/v3/binary/latest/8/ga/windows/x86/jre/"
           "hotspot/normal/eclipse?project=jdk")
DLL_IN_JAR = "lib/jni/video/libWindowsVDecode.dll"


def legacy_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except (AttributeError, ValueError, ssl.SSLError):
        pass
    return ctx


def fetch(url, ctx=None, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "etegro-bmc-setup"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def arp_mac(host):
    if os.name != "nt":
        return ""
    try:
        subprocess.run(["ping", "-n", "1", "-w", "800", host],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out = subprocess.run(["arp", "-a", host], capture_output=True, text=True,
                             timeout=10).stdout
        match = re.search(r"([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", out)
        if match:
            return match.group(1).replace("-", ":").lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def find_java():
    for root, _dirs, files in os.walk(RUNTIME_DIR):
        if os.path.basename(root) == "bin" and "java.exe" in files:
            return os.path.join(root, "java.exe")
    return None


def fetch_vendor(host):
    os.makedirs(VENDOR_DIR, exist_ok=True)
    jar_path = os.path.join(VENDOR_DIR, "JavaRSM.jar")
    dll_path = os.path.join(VENDOR_DIR, "libWindowsVDecode.dll")
    if os.path.isfile(jar_path) and os.path.isfile(dll_path):
        print("vendor files already present")
        return
    ctx = legacy_ctx()
    candidates = [
        f"https://{host}/lib/rsm/JavaRSM.jar",
        f"http://{host}/lib/rsm/JavaRSM.jar",
        f"https://{host}/JavaRSM.jar",
    ]
    data = None
    for url in candidates:
        try:
            print(f"fetching {url}")
            data = fetch(url, ctx)
            if data[:2] == b"PK" and len(data) > 100_000:
                break
            data = None
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {exc}")
    if not data:
        raise SystemExit(
            "Could not download JavaRSM.jar from the BMC.\n"
            f"Open https://{host}/lib/rsm/JavaRSM.jar in a browser (or copy it\n"
            "from the JavaRSM.jnlp cache) and put these two files here:\n"
            f"  {jar_path}\n  {dll_path}\n"
            "The DLL is inside the jar at " + DLL_IN_JAR)
    with open(jar_path, "wb") as fh:
        fh.write(data)
    print(f"saved {jar_path} ({len(data)} bytes)")
    with zipfile.ZipFile(io.BytesIO(data)) as jar:
        try:
            dll = jar.read(DLL_IN_JAR)
        except KeyError:
            raise SystemExit("libWindowsVDecode.dll is not inside JavaRSM.jar")
    with open(dll_path, "wb") as fh:
        fh.write(dll)
    print(f"saved {dll_path} ({len(dll)} bytes)")


def fetch_jre():
    if find_java():
        print("JRE already present")
        return
    print("downloading Temurin 8 x86 JRE (~37 MB)")
    data = fetch(JRE_URL)
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(RUNTIME_DIR)
    if not find_java():
        raise SystemExit("JRE extracted but java.exe was not found")
    print("JRE installed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="BMC address, for example 10.0.0.10")
    parser.add_argument("--user", default="root")
    parser.add_argument("--mac", help="server MAC used by the KVM login")
    parser.add_argument("--no-jre", action="store_true", help="skip the JRE download")
    args = parser.parse_args()

    host = (args.host or input("BMC address: ")).strip()
    if not host:
        raise SystemExit("BMC address is required")
    mac = (args.mac or arp_mac(host) or input("Server MAC (aa:bb:cc:dd:ee:ff): ")).strip()

    fetch_vendor(host)
    if not args.no_jre:
        fetch_jre()

    config = {"host": host, "user": args.user, "mac": mac, "quality": 3}
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    print(f"wrote {CONFIG_PATH}")
    print("Setup complete. Run start.ps1")


if __name__ == "__main__":
    main()
