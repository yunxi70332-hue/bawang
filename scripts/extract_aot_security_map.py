#!/usr/bin/env python3
"""Extract a small, evidence-backed map from Blutter's Dart AOT recovery output.

This is intentionally a *learning index*, not a request replayer or a key
extractor.  It captures function addresses, field names and call relationships
needed to navigate the original arm64 ``libapp.so`` in an offline disassembler.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ADDR_RE = re.compile(r"^\s*// \*\* addr: (0x[0-9a-f]+), size: (0x[0-9a-f]+)", re.I)
LITERAL_RE = re.compile(r'\br\d+ = "([^"\\]*(?:\\.[^"\\]*)*)"')

TARGETS = {
    "network/chagee_interceptor.dart": (
        "onRequest",
        "_handleRequestPostBody",
        "_generateSign",
        "buildEncryptedDataFromOriginal",
        "_handleRequestHeaders",
        "_handleCustomBaseUrl",
        "onResponse",
        "_decryptResponseIfNeeded",
        "decryptFieldsInResponse",
    ),
    "network/chagee_network.dart": ("_handleOptionsAndToast", "_requestBy", "_downloadRequestBy"),
}

LOGIN_TARGET = "../chagee_cn_login_module/business/login_service.dart"
LOGIN_METHODS = (
    "initOneClickLogin",
    "preGetPhoneNumber",
    "_tryOneClickLoginOrFallback",
    "_callOneClickLoginAPI",
    "_handleOneClickLoginSuccess",
    "_processLoginSuccess",
    "cleanToken",
    "loginSuccess",
)

FIELD_MARKERS = {
    "customHeaders", "customBaseUrl", "encryptFields", "decryptFields", "encryptedData",
    "accessToken", "customerId", "firstLogin", "newUser", "token", "login_type",
    "login_userLoginToken", "timestamp", "sign", "data",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def method_addresses(text: str) -> dict[str, dict[str, str]]:
    """Return method address/size pairs emitted immediately below headers."""
    lines = text.splitlines()
    result: dict[str, dict[str, str]] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.endswith("{") and "(" in stripped and ")" in stripped):
            continue
        # Blutter emits several valid header forms, including:
        #   _ initOneClickLogin(...) async {
        #   _ _handleRequestPostBody(...) {
        #   dynamic onRequest(...) {
        # Pull the identifier immediately preceding the argument list rather
        # than assuming a recovered Dart return type is available.
        before_args = stripped.split("(", 1)[0]
        names = re.findall(r"[A-Za-z_]\w*", before_args)
        if not names:
            continue
        name = names[-1]
        for candidate in lines[index + 1:index + 5]:
            addr = ADDR_RE.match(candidate)
            if addr:
                result[name] = {"address": addr.group(1), "size": addr.group(2)}
                break
    return result


def selected_literals(text: str) -> list[str]:
    values = {bytes(v, "utf-8").decode("unicode_escape") for v in LITERAL_RE.findall(text)}
    return sorted(value for value in values if value in FIELD_MARKERS)


def process_file(path: Path, methods: tuple[str, ...]) -> dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    recovered = method_addresses(text)
    return {
        "file": str(path).replace("\\", "/"),
        "methods": {name: recovered[name] for name in methods if name in recovered},
        "selected_literals": selected_literals(text),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--blutter-root", type=Path, required=True)
    ap.add_argument("--libapp", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    asm = args.blutter_root / "asm"
    network = asm / "chagee_base_network"
    login = asm / "chagee_cn_login_module" / "business" / "login_service.dart"
    expected = [network / name for name in TARGETS] + [login]
    missing = [str(path) for path in expected if not path.is_file()]
    if missing:
        ap.error("missing Blutter recovery files: " + ", ".join(missing))
    if not args.libapp.is_file():
        ap.error(f"missing libapp: {args.libapp}")

    files = []
    for relative, methods in TARGETS.items():
        files.append(process_file(network / relative, methods))
    files.append(process_file(login, LOGIN_METHODS))
    result = {
        "artifact": {
            "libapp": str(args.libapp.resolve()),
            "sha256": sha256(args.libapp),
            "bytes": args.libapp.stat().st_size,
        },
        "recovery_root": str(args.blutter_root.resolve()),
        "evidence_type": "Blutter static Dart AOT recovery output",
        "files": files,
        "interpretation_boundary": (
            "Addresses and names are navigation anchors. They do not establish a full protocol, "
            "server acceptance rules, or any credential value; validate behavioral claims with a "
            "sanitized trace from a controlled device."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    for entry in files:
        print(f"{entry['file']}: {len(entry['methods'])} method anchors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
