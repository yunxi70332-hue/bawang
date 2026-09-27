#!/usr/bin/env python3
"""Generate an evidence-only request/response crypto-flow note from Blutter output.

No network traffic, key material, token, or activation data is read by this
tool.  It only indexes names, literals and call markers emitted by Blutter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ADDR_RE = re.compile(r"^\s*// \*\* addr: (0x[0-9a-f]+), size: (0x[0-9a-f]+)", re.I)
LITERAL_RE = re.compile(r'\br\d+ = "([^"\\]*(?:\\.[^"\\]*)*)"')
CALL_RE = re.compile(r"; \[([^\]]+)\] ([A-Za-z0-9_:<>.]+)")

METHODS = {
    "request": ("_handleRequestPostBody", "buildEncryptedDataFromOriginal"),
    "response": ("_decryptResponseIfNeeded", "decryptFieldsInResponse"),
    "transport": ("_handleRequestHeaders", "_handleCustomBaseUrl"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_header(line: str) -> str | None:
    stripped = line.strip()
    if not (stripped.endswith("{") and "(" in stripped and ")" in stripped):
        return None
    prefix = stripped.split("(", 1)[0]
    names = re.findall(r"[A-Za-z_]\w*", prefix)
    return names[-1] if names else None


def blocks(text: str) -> dict[str, dict[str, object]]:
    lines = text.splitlines()
    located: list[tuple[str, int, str, str]] = []
    for index, line in enumerate(lines):
        name = is_header(line)
        if not name:
            continue
        address = size = None
        for candidate in lines[index + 1:index + 5]:
            match = ADDR_RE.match(candidate)
            if match:
                address, size = match.groups()
                break
        if address:
            located.append((name, index, address, size))
    result: dict[str, dict[str, object]] = {}
    for index, (name, start, address, size) in enumerate(located):
        end = located[index + 1][1] if index + 1 < len(located) else len(lines)
        body = "\n".join(lines[start:end])
        literals = []
        for raw in LITERAL_RE.findall(body):
            try:
                value = bytes(raw, "utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                value = raw
            if value not in literals:
                literals.append(value)
        calls = []
        for match in CALL_RE.finditer(body):
            candidate = f"[{match.group(1)}] {match.group(2)}"
            if candidate not in calls:
                calls.append(candidate)
        result[name] = {
            "address": address,
            "size": size,
            "literals": literals,
            "calls": calls,
        }
    return result


def select(method: dict[str, object], markers: tuple[str, ...]) -> dict[str, list[str]]:
    literals = [value for value in method["literals"] if any(mark in value for mark in markers)]
    calls = [value for value in method["calls"] if any(mark in value for mark in markers)]
    return {"literals": literals, "calls": calls}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--libapp", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--md-out", type=Path, required=True)
    args = parser.parse_args()
    if not args.source.is_file() or not args.libapp.is_file():
        parser.error("source or libapp missing")

    methods = blocks(args.source.read_text(encoding="utf-8", errors="replace"))
    expected = [name for group in METHODS.values() for name in group]
    missing = [name for name in expected if name not in methods]
    if missing:
        parser.error("Blutter method headers missing: " + ", ".join(missing))

    marker_set = ("Encrypt", "encrypt", "Decrypt", "decrypt", "AES", "base64", "Key", "Encrypted", "sign", "Header", "header", "BaseUrl", "baseUrl", "custom", "sk")
    result = {
        "evidence_type": "static Blutter AOT recovery only",
        "libapp": {"path": str(args.libapp.resolve()), "sha256": sha256(args.libapp)},
        "source": str(args.source.resolve()),
        "methods": {name: {**{"address": methods[name]["address"], "size": methods[name]["size"]}, **select(methods[name], marker_set)} for name in expected},
        "limits": [
            "No key, IV, token, identifier or packet payload is collected or emitted.",
            "Static call markers establish client code paths, not server acceptance rules.",
            "Runtime claims require a sanitized trace captured from a controlled test device.",
        ],
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def method_line(name: str) -> str:
        entry = result["methods"][name]
        return f"`{name}` — RVA `{entry['address']}`, size `{entry['size']}`"

    lines = [
        "# Flutter AOT 请求/响应字段处理：静态证据笔记",
        "",
        f"- 样本：`{args.libapp.name}`",
        f"- SHA-256：`{result['libapp']['sha256']}`",
        "- 证据范围：Blutter 产生的伪 Dart/ARM64 注释；没有读取真实请求、密钥、令牌或个人数据。",
        "",
        "## 请求侧",
        "",
        f"- {method_line('_handleRequestPostBody')}：读取请求扩展配置中的 `requestEncryptFields`、`sk`、`signFields`、`secretKey` 与 `sign` 标记；静态调用标记含 `base64Decode`、`Key`、`Encrypted.fromUtf8`、`AES`，随后调入 `buildEncryptedDataFromOriginal`。",
        f"- {method_line('buildEncryptedDataFromOriginal')}：存在 Map/List 递归与 `Encrypter::encrypt` / `Encrypted::base64` 调用标记。因此可把它标为“按字段选择、递归转换、输出 Base64 文本”的客户端处理函数；具体输入选择与参数仍须运行时确认。",
        "",
        "## 响应侧",
        "",
        f"- {method_line('_decryptResponseIfNeeded')}：读取 `responseEncryptFields` 与 `sk`，并存在 `base64Decode`、`Key`、`Encrypted.fromUtf8`、`AES` 标记，再调用 `decryptFieldsInResponse`。",
        f"- {method_line('decryptFieldsInResponse')}：存在 Map/List 遍历、递归回调与 `Encrypter::decrypt64` 标记，符合“仅解密配置字段、并深入嵌套对象”的结构。",
        "",
        "## 传输配置",
        "",
        f"- {method_line('_handleRequestHeaders')}：读取 `customHeaders` 并调用 `NetHeaderService::headers`。",
        f"- {method_line('_handleCustomBaseUrl')}：读取 `customBaseUrl` 并设置请求 `baseUrl`。",
        "",
        "## 结论边界",
        "",
        "这些结果只证明 APK 内存在上述客户端代码路径与字段名称；它们不证明固定算法参数、密钥来源、服务端校验规则或任何服务端接受条件。下一步应当是用脱敏的未登录启动日志验证调用次序，而不是保存真实业务请求。",
    ]
    args.md_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
