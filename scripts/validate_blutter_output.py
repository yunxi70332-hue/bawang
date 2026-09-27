#!/usr/bin/env python3
"""Offline integrity checks for a completed Blutter recovery directory."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REQUIRED = (
    "asm",
    "ida_script/addNames.py",
    "ida_script/ida_dart_struct.h",
    "blutter_frida.js",
    "objs.txt",
    "pp.txt",
    "asm/chagee_base_network/network/chagee_interceptor.dart",
    "asm/chagee_cn_login_module/business/login_service.dart",
)

ANCHORS = (
    "ChageeInterceptor",
    "buildEncryptedDataFromOriginal",
    "decryptFieldsInResponse",
    "MYLoginService",
    "_callOneClickLoginAPI",
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate(root: Path, libapp: Path) -> dict[str, object]:
    missing = [item for item in REQUIRED if not (root / item).exists()]
    dart_files = list((root / "asm").rglob("*.dart")) if (root / "asm").is_dir() else []
    joined = "\n".join(
        (root / item).read_text(encoding="utf-8", errors="replace")
        for item in REQUIRED[-2:]
        if (root / item).is_file()
    )
    anchors = {anchor: anchor in joined for anchor in ANCHORS}
    return {
        "root": str(root.resolve()),
        "libapp": {"path": str(libapp.resolve()), "sha256": sha256(libapp), "bytes": libapp.stat().st_size},
        "missing": missing,
        "dart_file_count": len(dart_files),
        "anchors": anchors,
        "ok": not missing and len(dart_files) >= 100 and all(anchors.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--libapp", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if not args.libapp.is_file():
        ap.error(f"missing libapp: {args.libapp}")
    result = validate(args.root, args.libapp)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
