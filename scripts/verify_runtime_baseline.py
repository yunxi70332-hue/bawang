#!/usr/bin/env python3
"""Verify an installed APK's sanitized cold-start baseline without secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


PACKAGE = "com.chagee.application.cn"


def run_adb(serial: str, *args: str) -> str:
    proc = subprocess.run(["adb", "-s", serial, *args], text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(f"adb {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--record", type=Path, required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--libapp", type=Path, required=True)
    ap.add_argument("--capture-dir", type=Path, default=Path("capture"))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    # PowerShell ConvertTo-Json writes UTF-8 with a BOM on Windows PowerShell.
    record = json.loads(args.record.read_text(encoding="utf-8-sig"))
    log = args.log.read_text(encoding="utf-8", errors="replace")
    state = run_adb(args.serial, "get-state").strip()
    package_paths = run_adb(args.serial, "shell", "pm", "path", PACKAGE)
    activity = run_adb(args.serial, "shell", "dumpsys", "activity", "activities")
    pid = run_adb(args.serial, "shell", "pidof", PACKAGE).strip()

    raw_logs = sorted(args.capture_dir.glob("*.raw.log"))
    checks = {
        "adb_online": state == "device",
        "package_installed": f"package:" in package_paths,
        "package_name": PACKAGE in activity,
        "main_activity_present": "com.chagee.application.cn/.MainActivity" in activity,
        "main_activity_displayed": bool(re.search(r"Displayed com\.chagee\.application\.cn/\.MainActivity", log)),
        "flutter_events": bool(re.search(r"\bI flutter\s*:", log)),
        "one_click_login_init": "一键登录服务初始化完成" in log,
        "privacy_false_baseline": "隐私政策同意状态: false" in log,
        "raw_logs_absent": not raw_logs,
        "arm64_libapp_hash_matches": sha256(args.libapp).lower() == "8b4aa0dd6dc15ec8380e402bfdb251941678c5b7830ee5a9200822857da5edd7",
    }
    result = {
        "serial": args.serial,
        "package": PACKAGE,
        "pid": pid,
        "record": str(args.record.resolve()),
        "sanitized_log": str(args.log.resolve()),
        "libapp": {"path": str(args.libapp.resolve()), "sha256": sha256(args.libapp)},
        "device": record.get("device", {}),
        "package_after": record.get("package_after", {}),
        "checks": checks,
        "maps_check": {
            "status": "permission-denied-fallback",
            "detail": "Android 9 emulator denied /proc/<pid>/maps; ABI, package path, Flutter events and static libapp hash were used instead.",
        },
        "ok": all(checks.values()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
