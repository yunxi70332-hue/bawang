#!/usr/bin/env python3
"""Deterministic offline triage for an Android APK.

Creates a compact JSON inventory without modifying the input APK. It extracts no
runtime secrets and is designed to be rerun while learning reverse engineering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

INTERESTING = (
    "/api/", "https://", "http://", "login", "token", "sign", "encrypt",
    "captcha", "sms", "phone", "user", "flutter", "dart", "mmkv",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ascii_strings(data: bytes, minimum: int = 6) -> list[str]:
    return [s.decode("latin1", "replace") for s in re.findall(rb"[ -~]{%d,}" % minimum, data)]


def apk_info(apk: Path) -> dict:
    with ZipFile(apk) as z:
        names = z.namelist()
        dex, native, assets = [], [], []
        for name in names:
            info = z.getinfo(name)
            record = {"name": name, "size": info.file_size}
            if name.endswith(".dex"):
                dex.append(record)
            elif name.startswith("lib/") and name.endswith(".so"):
                native.append(record)
            elif name.startswith("assets/"):
                assets.append(record)

        native_by_abi: dict[str, list[str]] = {}
        for item in native:
            _, abi, filename = item["name"].split("/", 2)
            native_by_abi.setdefault(abi, []).append(filename)

        dex_hits = {}
        for item in dex:
            strings = ascii_strings(z.read(item["name"]))
            hits = sorted({s for s in strings if any(k in s.lower() for k in INTERESTING)})
            dex_hits[item["name"]] = hits[:300]

        flutter_assets = [x["name"] for x in assets if x["name"].startswith("assets/flutter_assets/")]
        return {
            "input": str(apk.resolve()),
            "sha256": sha256_file(apk),
            "apk_bytes": apk.stat().st_size,
            "zip_entries": len(names),
            "top_level_entries": dict(sorted(Counter(x.split("/", 1)[0] for x in names).items())),
            "dex": dex,
            "native": native,
            "native_by_abi": native_by_abi,
            "asset_count": len(assets),
            "flutter": {
                "detected": any(x["name"].endswith("libflutter.so") for x in native) and bool(flutter_assets),
                "asset_count": len(flutter_assets),
                "contains_libapp": any(x["name"].endswith("libapp.so") for x in native),
            },
            "dex_interesting_strings": dex_hits,
        }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("apk", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if not args.apk.is_file():
        p.error(f"APK not found: {args.apk}")
    result = apk_info(args.apk)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"sha256={result['sha256']}")
    print(f"flutter={result['flutter']['detected']} dex={len(result['dex'])} native={len(result['native'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
