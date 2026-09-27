#!/usr/bin/env python3
"""Produce a concise Markdown learning note from the Blutter AOT recovery index."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("index", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    data = json.loads(args.index.read_text(encoding="utf-8"))
    lines = [
        "# Flutter AOT 恢复索引（Blutter）",
        "",
        f"- 原始 `libapp.so`：`{data['original_libapp']['path']}`",
        f"- SHA-256：`{data['original_libapp']['sha256']}`",
        f"- 大小：{data['original_libapp']['bytes']} bytes",
        "- 说明：输出为 AOT 恢复辅助材料，不等同于原始 Dart 源码。定位到的逻辑应通过原始 SO、Frida 调用栈或脱敏日志交叉验证。",
        "",
        "## 关键词统计",
        "",
    ]
    for key, count in data["keyword_totals"].items():
        lines.append(f"- `{key}`: {count}")
    lines += ["", "## 优先阅读文件", "", "| 文件 | 命中 |", "|---|---:|"]
    for row in data["files_with_keyword_hits"][:40]:
        hits = ", ".join(f"{k}:{v}" for k, v in row["hits"].items())
        lines.append(f"| `{row['file']}` | {hits} |")
    lines += [
        "",
        "## 下一步验证顺序",
        "",
        "1. 先阅读 `chagee_base_network` 的 header、interceptor、network 和 request/response 模型。",
        "2. 再将登录模块的路由和字段名与公开登录前流程的脱敏 runtime log 对照。",
        "3. 对任何认证令牌、设备标识、签名材料只保留脱敏证据；不把真实账号状态或原始敏感值写进仓库。",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
