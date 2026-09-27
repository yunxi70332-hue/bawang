#!/usr/bin/env python3
"""Stage APK native libraries by ABI without losing the original provenance.

The script uses hard links by default on the same NTFS volume, falling back to
copying only if hard links are unavailable.  It never overwrites a staged file
whose SHA-256 differs from the APK extraction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path


MACHINES = {
    0x0003: "EM_386",
    0x0028: "EM_ARM",
    0x003E: "EM_X86_64",
    0x00B7: "EM_AARCH64",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def elf_metadata(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        header = handle.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return {"is_elf": False}
    elf_class = {1: "ELF32", 2: "ELF64"}.get(header[4], f"unknown({header[4]})")
    endian = "little" if header[5] == 1 else "big" if header[5] == 2 else "unknown"
    fmt = "<H" if endian == "little" else ">H" if endian == "big" else "<H"
    machine = struct.unpack(fmt, header[18:20])[0]
    return {
        "is_elf": True,
        "class": elf_class,
        "endianness": endian,
        "machine": f"0x{machine:04x}",
        "machine_name": MACHINES.get(machine, "unknown"),
    }


def stage(source: Path, destination: Path, mode: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256(source) != sha256(destination):
            raise RuntimeError(f"refusing to overwrite mismatched file: {destination}")
        return "existing"
    if mode == "copy":
        shutil.copy2(source, destination)
        return "copy"
    try:
        os.link(source, destination)
        return "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        return "copy-fallback"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="APK extracted lib/ directory")
    parser.add_argument("--dest", type=Path, required=True, help="ABI-organized analysis directory")
    parser.add_argument("--manifest", type=Path, required=True, help="output JSON manifest")
    parser.add_argument("--mode", choices=("hardlink", "copy"), default="hardlink")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.source.is_dir():
        parser.error(f"missing source directory: {args.source}")
    entries = []
    for source in sorted(args.source.rglob("*.so")):
        relative = source.relative_to(args.source)
        if len(relative.parts) < 2:
            continue
        abi = relative.parts[0]
        target = args.dest / relative
        action = "dry-run" if args.dry_run else stage(source, target, args.mode)
        entries.append({
            "abi": abi,
            "name": source.name,
            "source": str(source.resolve()),
            "staged": str(target.resolve()) if target.exists() else str(target),
            "bytes": source.stat().st_size,
            "sha256": sha256(source),
            "stage_action": action,
            "elf": elf_metadata(source),
        })

    manifest = {
        "source": str(args.source.resolve()),
        "destination": str(args.dest.resolve()),
        "mode": args.mode,
        "entries": entries,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"staged {len(entries)} libraries; wrote {args.manifest}")
    for entry in entries:
        print(f"{entry['abi']:<12} {entry['name']:<24} {entry['elf'].get('machine_name')} {entry['stage_action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
