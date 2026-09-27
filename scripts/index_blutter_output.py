#!/usr/bin/env python3
"""Index selected Blutter AOT-recovery output for repeatable APK study.

The script treats Blutter's recovered Dart-like source as *recovery output*:
identifiers, paths, and method bodies are useful anchors, but every important
claim should still be checked against the original libapp.so or a runtime trace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


DEFAULT_PACKAGES = (
    "chagee_base_network",
    "chagee_cn_login_module",
    "chagee_cn_app_login_module",
    "chagee_cn_login_service",
    "chagee_cn_app_user_module",
    "chagee_cn_app_order_module",
    "chagee_cn_app_pay_module",
    "chagee_common_utils",
)

KEYWORDS = (
    "login", "token", "authorization", "bearer", "encrypt", "decrypt",
    "sign", "signature", "header", "interceptor", "dio", "request",
    "response", "refresh", "device", "secure", "storage", "cookie",
    "api", "baseurl", "quicklogin", "mobile",
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="Blutter output directory")
    ap.add_argument("--libapp", type=Path, required=True, help="original arm64 libapp.so")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--packages", nargs="*", default=DEFAULT_PACKAGES)
    args = ap.parse_args()

    asm = args.root / "asm"
    if not asm.is_dir():
        ap.error(f"missing asm directory: {asm}")
    if not args.libapp.is_file():
        ap.error(f"missing original libapp: {args.libapp}")

    rows: list[dict[str, object]] = []
    aggregate = Counter()
    for package in args.packages:
        pkg = asm / package
        for file in sorted(pkg.rglob("*.dart")) if pkg.is_dir() else []:
            text = file.read_text(encoding="utf-8", errors="replace")
            hits = {key: len(re.findall(re.escape(key), text, re.I)) for key in KEYWORDS}
            hits = {key: count for key, count in hits.items() if count}
            if hits:
                aggregate.update(hits)
                rows.append({
                    "package": package,
                    "file": str(file.relative_to(args.root)).replace("\\", "/"),
                    "bytes": file.stat().st_size,
                    "hits": hits,
                })

    rows.sort(key=lambda r: (-sum(r["hits"].values()), r["file"]))
    result = {
        "tool": "Blutter",
        "recovery_output_root": str(args.root.resolve()),
        "original_libapp": {
            "path": str(args.libapp.resolve()),
            "sha256": sha256(args.libapp),
            "bytes": args.libapp.stat().st_size,
        },
        "selected_packages": args.packages,
        "keyword_totals": dict(aggregate.most_common()),
        "files_with_keyword_hits": rows,
        "caveat": (
            "Blutter output is an AOT recovery aid, not original Dart source. "
            "Validate behavior with original bytes, a runtime hook, or a sanitized trace."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"matches={len(rows)} top={rows[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
