#!/usr/bin/env python3
"""Verify and repair a baksmali/apktool decode against the source DEX files.

Motivation: baksmali has no retry logic, and on some hosts a security filter
transiently returns ERROR_ACCESS_DENIED for new file creation (a *random*
fail-closed race, not a path-policy problem). The result is a decode that
reports success while silently skipping a handful of classes. Rebuilding such a
tree yields a broken APK, so completeness must be checked explicitly.

This script:
  verify  - report, per DEX, which classes are missing from a smali tree
  merge   - fill missing .smali files in a target tree from one or more donor
            trees (each decode pass fails on a different random subset, so the
            union of two passes is normally complete)

Usage:
    python verify_smali_tree.py verify --tree <decoded> --dex-dir <raw_apk> [--json out.json]
    python verify_smali_tree.py merge  --tree <decoded> --donors <decoded_b> [<decoded_c> ...]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dex_class_list import dex_classes, descriptor_to_smali_path  # noqa: E402

# How apktool names the smali dir for each dex.
DEX_TO_SMALI = {
    "classes.dex": "smali",
    "classes2.dex": "smali_classes2",
    "classes3.dex": "smali_classes3",
    "classes4.dex": "smali_classes4",
}


def expected_from_dex(dex_dir: Path) -> dict[str, set[str]]:
    """{'smali': {'com/foo/Bar.smali', ...}, ...}"""
    out: dict[str, set[str]] = {}
    for dex_name, smali_dir in DEX_TO_SMALI.items():
        dex = dex_dir / dex_name
        if not dex.is_file():
            continue
        out[smali_dir] = {descriptor_to_smali_path(c) for c in dex_classes(dex.read_bytes())}
    return out


def present_in(tree: Path, smali_dir: str) -> set[str]:
    root = tree / smali_dir
    if not root.is_dir():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*.smali")}


def cmd_verify(args) -> int:
    expected = expected_from_dex(args.dex_dir)
    report, total_exp, total_missing = {}, 0, 0
    for smali_dir, want in expected.items():
        have = present_in(args.tree, smali_dir)
        missing = sorted(want - have)
        total_exp += len(want)
        total_missing += len(missing)
        report[smali_dir] = {
            "expected": len(want),
            "present": len(have),
            "missing": len(missing),
            "missing_list": missing,
        }
        print(f"{smali_dir:18s} 期望={len(want):6d} 已落盘={len(have):6d} 缺失={len(missing):4d}")
    print()
    print(f"合计：期望 {total_exp} 类，缺失 {total_missing} 类")
    if total_missing == 0:
        print("✅ 完整性通过：smali 树与 DEX 类定义一一对应")
    else:
        print("❌ 完整性不通过：直接回编译会产出缺类的包")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {"tree": str(args.tree), "total_expected": total_exp,
             "total_missing": total_missing, "by_dir": report},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0 if total_missing == 0 else 1


def cmd_merge(args) -> int:
    expected = expected_from_dex(args.dex_dir)
    copied_total = 0
    for smali_dir, want in expected.items():
        target_root = args.tree / smali_dir
        missing = want - present_in(args.tree, smali_dir)
        if not missing:
            continue
        for rel in sorted(missing):
            for donor in args.donors:
                src = donor / smali_dir / rel
                if src.is_file():
                    dst = target_root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    copied_total += 1
                    break
    print(f"从 donor 补齐 {copied_total} 个 .smali 文件")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="report missing classes")
    v.add_argument("--tree", type=Path, required=True)
    v.add_argument("--dex-dir", type=Path, required=True,
                   help="dir containing classes*.dex extracted from the APK")
    v.add_argument("--json", type=Path)
    v.set_defaults(func=cmd_verify)

    m = sub.add_parser("merge", help="fill gaps from donor trees")
    m.add_argument("--tree", type=Path, required=True, help="target tree (modified in place)")
    m.add_argument("--donors", type=Path, nargs="+", required=True)
    m.add_argument("--dex-dir", type=Path, required=True)
    m.set_defaults(func=cmd_merge)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
