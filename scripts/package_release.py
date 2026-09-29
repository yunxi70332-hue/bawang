#!/usr/bin/env python3
"""一键打包线上发布产物（v1.0，配套 docs/线上部署手册_v1.0.md）。

用法：python scripts/package_release.py
产物：output/release_v1.0_<时间戳>.zip，内含 server/ + web/dist/ + scripts/chagee_*.py
运行时协议层 + requirements.txt + 部署/接口/模块三份文档。
不含 data/（密钥/凭证/代理配置）、不含 __pycache__/测试/取证工具。

清单口径与手册 §1 一致：解压到服务器 → venv → pip → uvicorn 起 8000/8010 两进程。
"""

import os
import time
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = os.path.join(_ROOT, "account_system", "server")
_DIST = os.path.join(_ROOT, "account_system", "web", "dist")
_DOCS = os.path.join(_ROOT, "account_system", "docs")
_SCRIPTS = os.path.join(_ROOT, "scripts")
_OUT_DIR = os.path.join(_ROOT, "output")

# (磁盘路径, 包内前缀, 过滤函数)
_TARGETS = [
    (_SERVER, "server", lambda p, n: n.endswith(".py") or n.endswith(".html")
     or p.split(os.sep)[-2] in ("static", "templates")),
    (_DIST, "web/dist", None),  # 构建产物全量
    (_SCRIPTS, "scripts", lambda p, n: n.startswith("chagee_") and n.endswith(".py")),
]

_DOCS_INCLUDE = [
    "线上部署手册_v1.0.md", "intake_api_reference.md", "模块功能说明.md",
    "代码规范审计报告_20260930.md", "intake_system.md", "decision_api_contract.md",
]


def _add_dir(zf: zipfile.ZipFile, src: str, prefix: str, keep=None) -> int:
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, src)
            if keep and not keep(path, f):
                continue
            zf.write(path, os.path.join(prefix, rel))
            n += 1
    return n


def main() -> None:
    os.makedirs(_OUT_DIR, exist_ok=True)
    out = os.path.join(_OUT_DIR, f"release_1.0_{time.strftime('%Y%m%d_%H%M')}.zip")
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, prefix, keep in _TARGETS:
            if os.path.isdir(src):
                total += _add_dir(zf, src, prefix, keep)
        for name in _DOCS_INCLUDE:
            p = os.path.join(_DOCS, name)
            if os.path.isfile(p):
                zf.write(p, os.path.join("docs", name))
                total += 1
        req = os.path.join(_ROOT, "requirements.txt")
        if os.path.isfile(req):
            zf.write(req, "requirements.txt")
            total += 1
    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"OK {out}（{total} 个文件，{size_mb:.1f} MB）")


if __name__ == "__main__":
    main()
