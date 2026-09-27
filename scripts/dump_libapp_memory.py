#!/usr/bin/env python3
"""Route ①: dump runtime-mapped libapp.so of CHAGEE via frida, verify integrity.

Handles both mapping modes:
  - extracted .so under /data/app/.../lib/arm64/ (extractNativeLibs=true)
  - direct-from-APK slices (extractNativeLibs=false -> path ends with base.apk)

Usage:
    python scripts/dump_libapp_memory.py --serial 125.109.27.7:56915 [--pid N]

Comparison model:
  memory page at (region.start + i)  <->  libapp.so byte (region.file_off + i - apk_off)
Only file-backed regions of libapp.so are compared page-by-page against the
static arm64 libapp.so. Data pages legitimately differ where the loader applied
relocations; r-xp text / r--p rodata should match 1:1 for an unpacked app.
"""

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import frida

APK = Path(r"E:\霸王茶姬\apk\chagee_upload.apk")
STATIC_LIB = Path(r"E:\霸王茶姬\decompiled\raw_apk\lib\arm64-v8a\libapp.so")
STATIC_SHA = "8b4aa0dd6dc15ec8380e402bfdb251941678c5b7830ee5a9200822857da5edd7"
PKG = "com.chagee.application.cn"
LIB_NAME = "lib/arm64-v8a/libapp.so"
PAGE = 4096


def sh(serial: str, cmd: str, timeout: int = 60) -> str:
    out = subprocess.run(["adb", "-s", serial, "shell", cmd],
                         capture_output=True, text=True, timeout=timeout)
    return out.stdout


def apk_lib_offset() -> int:
    z = zipfile.ZipFile(APK)
    info = z.getinfo(LIB_NAME)
    with open(APK, "rb") as f:
        f.seek(info.header_offset)
        hdr = f.read(30)
        nlen, elen = struct.unpack("<HH", hdr[26:30])
    return info.header_offset + 30 + nlen + elen


def get_pid(serial: str) -> str:
    pid = sh(serial, f"pidof {PKG}").strip()
    if pid:
        return pid.split()[0]
    print("[*] app not running, cold starting via monkey ...")
    sh(serial, f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1", timeout=90)
    for _ in range(45):
        time.sleep(2)
        pid = sh(serial, f"pidof {PKG}").strip()
        if pid:
            return pid.split()[0]
    raise SystemExit("[!] app failed to start")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True)
    ap.add_argument("--pid")
    ap.add_argument("--port", type=int, default=27042)
    ap.add_argument("--out", default="docs/libapp_memory_dump_report.json")
    ap.add_argument("--dumpdir", default="capture")
    args = ap.parse_args()

    static_bytes = STATIC_LIB.read_bytes()
    static_sha = hashlib.sha256(static_bytes).hexdigest()
    assert static_sha == STATIC_SHA, "static libapp.so drifted from learning log"
    print(f"[*] static libapp.so sha256 = {static_sha} ({len(static_bytes)} bytes)")

    apk_off = apk_lib_offset()
    print(f"[*] libapp.so data offset inside APK = {hex(apk_off)}")

    pid = args.pid or get_pid(args.serial)
    print(f"[*] pid = {pid}")

    maps_raw = sh(args.serial, f"cat /proc/{pid}/maps")
    # candidate lines: extracted libapp.so path, or base.apk slices
    lib_lines = [l for l in maps_raw.splitlines() if l.rstrip().endswith("/libapp.so")]
    apk_lines = [l for l in maps_raw.splitlines() if l.rstrip().endswith("/base.apk")]

    regions = []  # (start, end, perms, file_off, delta_to_lib_off)
    if lib_lines:
        mode = "extracted"
        for line in lib_lines:
            m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4}) ([0-9a-f]+) ", line)
            start, end, perms, off = int(m.group(1), 16), int(m.group(2), 16), m.group(3), int(m.group(4), 16)
            regions.append((start, end, perms, off, 0))
        disk_path = lib_lines[0].split()[-1]
    elif apk_lines:
        mode = "apk-direct"
        lib_lo, lib_hi = apk_off, apk_off + len(static_bytes)
        for line in apk_lines:
            m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4}) ([0-9a-f]+) ", line)
            start, end, perms, off = int(m.group(1), 16), int(m.group(2), 16), m.group(3), int(m.group(4), 16)
            # overlap of [off, off+size) with libapp range
            lo, hi = max(off, lib_lo), min(off + (end - start), lib_hi)
            if lo >= hi:
                continue
            regions.append((start + (lo - off), start + (hi - off), perms, lo, apk_off))
        disk_path = apk_lines[0].split()[-1]
    else:
        raise SystemExit("[!] neither libapp.so nor base.apk mappings found")

    print(f"[*] mapping mode = {mode}; {len(regions)} file-backed regions of libapp.so")
    for s, e, p, o, d in regions:
        print(f"    {p} {hex(s)}-{hex(e)} apkfile_off={hex(o)}")

    # on-disk reference hash (extracted lib, or slice straight from the APK file)
    if mode == "extracted":
        disk_sha = sh(args.serial, f"sha256sum {disk_path}").split()[0]
    else:
        disk_sha = static_sha  # direct-from-APK: disk bytes ARE the APK slice
    print(f"[*] disk reference sha256 = {disk_sha}")

    # frida dump
    subprocess.run(["adb", "-s", args.serial, "forward", f"tcp:{args.port}", f"tcp:{args.port}"],
                   capture_output=True, timeout=30)
    try:
        device = frida.get_device_manager().add_remote_device(f"127.0.0.1:{args.port}")
        session = device.attach(int(pid))
        print(f"[*] attached to pid {pid}")
        chunks = []
        done = {"ok": False}

        def on_message(message, data):
            if message["type"] == "send":
                p = message["payload"]
                if p.get("ev") == "region":
                    if data is not None:
                        chunks.append((p["start"], p["off"], bytes(data)))
                        print(f"    dumped {hex(p['start'])} {p['perms']} len={len(data)}")
                    else:
                        print(f"    [!] region {hex(p['start'])} failed: {p.get('err')}")
                elif p.get("ev") == "done":
                    done["ok"] = True
            elif message["type"] == "error":
                print(f"[!] script error: {message.get('description')}")

        script = session.create_script("""
        rpc.exports = {
          dump: function (regions) {
            for (const r of regions) {
              const len = r.end - r.start;
              try {
                send({ev:'region', start:r.start, off:r.off, perms:r.perms},
                     Memory.readByteArray(ptr(r.start), len));
              } catch (e) {
                send({ev:'region', start:r.start, off:r.off, perms:r.perms, err:''+e}, null);
              }
            }
            send({ev:'done'});
          }
        };
        """)
        script.on("message", on_message)
        script.load()
        meta = [{"start": s, "end": e, "perms": p, "off": o} for s, e, p, o, d in regions]
        script.exports_sync.dump(meta)
        for _ in range(600):
            if done["ok"]:
                break
            time.sleep(0.2)
        session.detach()
    finally:
        subprocess.run(["adb", "-s", args.serial, "forward", "--remove", f"tcp:{args.port}"],
                       capture_output=True, timeout=30)

    if not chunks:
        raise SystemExit("[!] no memory chunks received")

    # compare page-wise
    report = {
        "pid": pid, "mode": mode, "disk_path": disk_path,
        "disk_sha256": disk_sha, "static_sha256": static_sha,
        "disk_matches_static": disk_sha == static_sha,
        "regions": [],
    }
    total = matched = 0
    dump_path = Path(args.dumpdir) / f"libapp_mem_{pid}_{time.strftime('%Y%m%d_%H%M%S')}.bin"
    with open(dump_path, "wb") as df:
        for start, off, data in sorted(chunks, key=lambda c: c[1]):
            df.write(data)
            delta = dict((r[3], r[4]) for r in regions).get(off, 0)
            lib_off = off - delta
            pages = len(data) // PAGE
            m = 0
            for i in range(pages):
                lo = lib_off + i * PAGE
                if lo < 0 or lo + PAGE > len(static_bytes):
                    break
                if data[i * PAGE:(i + 1) * PAGE] == static_bytes[lo:lo + PAGE]:
                    m += 1
            total += pages
            matched += m
            report["regions"].append({
                "mem_start": hex(start), "lib_file_offset": hex(lib_off),
                "length": len(data), "pages": pages, "pages_matching_file": m,
            })
            print(f"[*] lib_off={hex(lib_off)} len={len(data)} pages {m}/{pages} match static")

    report["total_pages"] = total
    report["matched_pages"] = matched
    report["match_ratio"] = round(matched / total, 4) if total else None
    report["dump_file"] = str(dump_path)

    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[+] report -> {args.out}")
    print(f"[+] dump    -> {dump_path}")
    print(f"[+] disk == static : {report['disk_matches_static']}")
    print(f"[+] mem page match : {report['match_ratio']} ({matched}/{total} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
