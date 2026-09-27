#!/usr/bin/env python3
"""Capture filtered adb logcat, sanitize it, and delete the raw temporary log."""
from __future__ import annotations

import argparse
import re
import sys
import subprocess
from datetime import datetime
from pathlib import Path

workspace = Path(__file__).resolve().parents[1]
if str(workspace) not in sys.path:
    sys.path.insert(0, str(workspace))

from scripts.sanitize_log import redact


PATTERN = re.compile(
    r"com\.chagee|flutter|Dio|dio|SSL|TLS|UnknownHost|SocketException|"
    r"ConnectException|验证码|短信|login|Login|QuickLogin|CMCC-SDK|onekey|api-cn|chagee",
    re.I,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("capture"))
    ap.add_argument("--lines", type=int, default=1600)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw = args.out_dir / f"interaction-{stamp}.raw.log"
    clean = args.out_dir / f"interaction-{stamp}.sanitized.log"
    try:
        proc = subprocess.run(
            ["adb", "-s", args.serial, "logcat", "-d", "-v", "threadtime", "-t", str(args.lines)],
            check=True, text=True, capture_output=True,
        )
        filtered = "\n".join(line for line in proc.stdout.splitlines() if PATTERN.search(line)) + "\n"
        raw.write_text(filtered, encoding="utf-8")
        clean.write_text(redact(filtered), encoding="utf-8")
    finally:
        raw.unlink(missing_ok=True)
    print(clean)
    print(f"raw_remaining={len(list(args.out_dir.glob('*.raw.log')))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
