#!/usr/bin/env python3
"""Compare two APKs entry-by-entry to verify a repack round-trip.

Answers: what actually changed between the original and the rebuilt APK?
Useful as the control-group check for a baseline repack (no logic changes
should mean: same entry set, only meta/signature differences).

Usage:
    python compare_apk_entries.py <original.apk> <rebuilt.apk> [--out report.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path

# Entries whose bytes are expected to differ after a rebuild/sign cycle.
EXPECTED_DIFF_PREFIXES = ("META-INF/",)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def inventory(apk: Path) -> dict:
    with zipfile.ZipFile(apk) as z:
        entries = {}
        for info in z.infolist():
            if info.is_dir():
                continue
            data = z.read(info.filename)
            entries[info.filename] = {
                "size": info.file_size,
                "compress_type": info.compress_type,
                "crc": info.CRC,
                "sha256": sha256_bytes(data),
                "file_size": info.file_size,
                "compress_size": info.compress_size,
            }
    return {
        "path": str(apk.resolve()),
        "bytes": apk.stat().st_size,
        "sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
        "entry_count": len(entries),
        "entries": entries,
    }


def bucket(name: str) -> str:
    if name.startswith("lib/") and name.endswith(".so"):
        parts = name.split("/")
        return f"lib/{parts[1]}/*.so" if len(parts) > 2 else "lib/*.so"
    if name.endswith(".dex"):
        return "*.dex"
    if name.startswith("res/"):
        return "res/*"
    if name.startswith("assets/flutter_assets/"):
        return "assets/flutter_assets/*"
    if name.startswith("assets/"):
        return "assets/*"
    if name.startswith("META-INF/"):
        return "META-INF/*"
    return name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", type=Path)
    ap.add_argument("rebuilt", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    a, b = inventory(args.original), inventory(args.rebuilt)
    ea, eb = a["entries"], b["entries"]

    only_a = sorted(set(ea) - set(eb))
    only_b = sorted(set(eb) - set(ea))
    changed, identical = [], []
    for name in sorted(set(ea) & set(eb)):
        if ea[name]["sha256"] != eb[name]["sha256"]:
            changed.append({
                "entry": name,
                "size_original": ea[name]["size"],
                "size_rebuilt": eb[name]["size"],
                "expected": name.startswith(EXPECTED_DIFF_PREFIXES),
            })
        else:
            identical.append(name)

    by_bucket_a = Counter(bucket(n) for n in ea)
    by_bucket_b = Counter(bucket(n) for n in eb)

    report = {
        "original": {k: v for k, v in a.items() if k != "entries"},
        "rebuilt": {k: v for k, v in b.items() if k != "entries"},
        "entry_count": {"original": a["entry_count"], "rebuilt": b["entry_count"]},
        "only_in_original": only_a,
        "only_in_rebuilt": only_b,
        "identical_content": len(identical),
        "changed_content": changed,
        "changed_unexpected": [c for c in changed if not c["expected"]],
        "buckets": {
            k: {"original": by_bucket_a.get(k, 0), "rebuilt": by_bucket_b.get(k, 0)}
            for k in sorted(set(by_bucket_a) | set(by_bucket_b))
        },
        "verdict": (
            "identical entry set; only expected (meta) differences"
            if not only_a and not only_b
            and not [c for c in changed if not c["expected"]]
            else "differences need review"
        ),
    }

    print(f"original : {a['entry_count']:>6} entries  {a['bytes']:>12,} bytes  {a['sha256'][:16]}…")
    print(f"rebuilt  : {b['entry_count']:>6} entries  {b['bytes']:>12,} bytes  {b['sha256'][:16]}…")
    print()
    print(f"identical content : {len(identical)}")
    print(f"changed content   : {len(changed)}  (expected {sum(1 for c in changed if c['expected'])})")
    print(f"only in original  : {len(only_a)}")
    print(f"only in rebuilt   : {len(only_b)}")
    print()
    print("bucket                                 original   rebuilt")
    for k, v in report["buckets"].items():
        print(f"  {k:<38} {v['original']:>8}  {v['rebuilt']:>8}")
    if report["changed_unexpected"]:
        print("\n⚠️ 非预期差异：")
        for c in report["changed_unexpected"][:40]:
            print(f"  - {c['entry']}  {c['size_original']} -> {c['size_rebuilt']}")
    if only_a:
        print(f"\n仅原包存在（前 20）：")
        for n in only_a[:20]:
            print(f"  - {n}")
    if only_b:
        print(f"\n仅新包存在（前 20）：")
        for n in only_b[:20]:
            print(f"  + {n}")
    print(f"\n结论：{report['verdict']}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
