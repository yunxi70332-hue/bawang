#!/usr/bin/env python3
"""List every class defined in a DEX file (minimal DEX parser).

Used to verify that a baksmali decode produced a complete smali tree: the set
of .smali files must exactly match the class_defs in the DEX.

Usage:
    python dex_class_list.py <file.dex> [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path


def _uleb128(buf: bytes, off: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, off


def dex_classes(data: bytes) -> list[str]:
    if data[:4] != b"dex\n":
        raise ValueError("not a DEX file (bad magic)")
    (string_ids_size, string_ids_off,
     _type_ids_size, type_ids_off,
     _proto_ids_size, _proto_ids_off,
     _field_ids_size, _field_ids_off,
     _method_ids_size, _method_ids_off,
     class_defs_size, class_defs_off) = struct.unpack_from("<12I", data, 0x38)

    def string_at(idx: int) -> str:
        str_off = struct.unpack_from("<I", data, string_ids_off + idx * 4)[0]
        # skip uleb128 utf16 length
        _, p = _uleb128(data, str_off)
        end = data.index(b"\x00", p)
        return data[p:end].decode("utf-8", "replace")

    def type_descriptor(type_idx: int) -> str:
        str_idx = struct.unpack_from("<I", data, type_ids_off + type_idx * 4)[0]
        return string_at(str_idx)

    out = []
    for i in range(class_defs_size):
        class_idx = struct.unpack_from("<I", data, class_defs_off + i * 0x20)[0]
        out.append(type_descriptor(class_idx))
    return out


def descriptor_to_smali_path(desc: str) -> str:
    """'Lcom/foo/Bar$1;' -> 'com/foo/Bar$1.smali'"""
    if not (desc.startswith("L") and desc.endswith(";")):
        return desc  # primitive/array — should not appear as a defined class
    body = desc[1:-1]
    # DEX uses '.' only for inner-class separators in some tooling; keep as-is.
    return body + ".smali"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dex", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    classes = dex_classes(args.dex.read_bytes())
    print(f"{args.dex.name}: {len(classes)} classes")
    if args.json:
        args.json.write_text(json.dumps(classes, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
