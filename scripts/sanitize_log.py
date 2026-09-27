#!/usr/bin/env python3
"""Redact runtime-log values before preserving a learning capture.

The filter deliberately retains event names and high-level errors while replacing
query values, bearer-like strings, UUIDs, phone numbers, and JPush registration IDs.
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path

RULES = [
    (re.compile(r'([?&][A-Za-z0-9_.-]+=)[^&\s]+'), r'\1<REDACTED>'),
    (re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+'), r'\1<REDACTED>'),
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I), '<UUID>'),
    (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), '<PHONE>'),
    (re.compile(r"(JPush registration ID:\s*)[A-Za-z0-9_-]+", re.I), r'\1<REDACTED>'),
    (re.compile(r'(registration ID[^:=]*[=:]\s*)[A-Za-z0-9_-]+', re.I), r'\1<REDACTED>'),
    (re.compile(r'(token["\']?\s*[:=]\s*["\']?)[A-Za-z0-9._~+\-/=]+', re.I), r'\1<REDACTED>'),
]

def redact(text: str) -> str:
    for pattern, replacement in RULES:
        text = pattern.sub(replacement, text)
    return text

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    text = a.input.read_text(encoding='utf-8', errors='replace')
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(redact(text), encoding='utf-8')
    print(f'wrote {a.output}')
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
