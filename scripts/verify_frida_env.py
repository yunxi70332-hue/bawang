#!/usr/bin/env python3
"""Verify host<->device frida connectivity on the new RK3588S device.

Usage:
    python scripts/verify_frida_env.py --serial 125.109.27.7:56915

Steps:
  1. adb forward tcp:27042 -> device loopback 27042 (frida-server must listen on 127.0.0.1)
  2. attach via remote device, enumerate processes, locate com.chagee.application.cn if running
"""

import argparse
import subprocess
import sys

import frida


def adb(serial: str, *args: str) -> str:
    cmd = ["adb", "-s", serial, *args]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"adb {args} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True)
    ap.add_argument("--host-port", type=int, default=27042)
    ap.add_argument("--device-port", type=int, default=27042)
    args = ap.parse_args()

    print(f"[*] frida client version: {frida.__version__}")

    fwd = adb(args.serial, "forward", f"tcp:{args.host_port}", f"tcp:{args.device_port}")
    print(f"[*] adb forward: {fwd}")

    try:
        mgr = frida.get_device_manager()
        device = mgr.add_remote_device(f"127.0.0.1:{args.host_port}")
        procs = device.enumerate_processes()
        print(f"[+] remote device ok, {len(procs)} processes visible")

        targets = [p for p in procs if "chagee" in p.name.lower()]
        if targets:
            for p in targets:
                print(f"[+] target running: {p.name} pid={p.pid}")
        else:
            print("[-] com.chagee.application.cn not running (cold start not done yet)")

        apps = [a for a in device.enumerate_applications() if "chagee" in (a.identifier or "").lower()]
        for a in apps:
            print(f"[+] installed app: {a.identifier} pid={a.pid}")
        return 0
    finally:
        adb(args.serial, "forward", "--remove", f"tcp:{args.host_port}")
        print("[*] forward removed")


if __name__ == "__main__":
    sys.exit(main())
