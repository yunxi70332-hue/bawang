#!/usr/bin/env python3
"""审计 Android 设备上的根证书库，找出"抓包 CA 暴露面"。

回答的问题：这台设备上有没有非原厂的根证书？装在哪个证书库？
（系统库 `/system/etc/security/cacerts` vs 用户库
`/data/misc/user/0/cacerts-added`）

判定规则：
  * 用户库里的任何一张证书 —— 一律标红。市售设备的用户库通常是空的，
    只要非空，就是"这台机器被手动装过根证书"的直接证据。
  * 系统库里 subject/issuer 命中中间人工具字样的 —— 标红。
  * 系统库里 notBefore 明显晚于设备镜像构建时间的 —— 标黄（疑似后期塞入）。

只读操作，不上网，不改设备。用法:
    python scripts/audit_device_ca.py <目录或文件> [<目录或文件> ...]
"""

import argparse
import hashlib
import pathlib
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes

MITM_PATTERNS = (
    "mitmproxy", "reqable", "charles", "fiddler", "burp", "portswigger",
    "httpcanary", "proxypin", "whistle", "anyproxy", "frida", "httptoolkit",
    "zaproxy", "owasp", "wiremock", "testca", "do not trust", "untrusted",
)

SYSTEM_STORE_HINTS = ("/system/etc/security/cacerts", "cacerts", "sys")
USER_STORE_HINTS = ("cacerts-added", "user")


def load_cert(path: pathlib.Path):
    raw = path.read_bytes()
    for loader in (x509.load_der_x509_certificate, x509.load_pem_x509_certificate):
        try:
            return loader(raw)
        except Exception:  # noqa: BLE001
            continue
    return None


def store_kind(path: pathlib.Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    text = str(path).replace("\\", "/").lower()
    if "cacerts-added" in text:
        return "user"
    for hint in SYSTEM_STORE_HINTS:
        if hint in text:
            return "system"
    return "unknown"


def collect(targets):
    out = []
    for t in targets:
        p = pathlib.Path(t)
        if p.is_dir():
            out.extend(sorted(f for f in p.iterdir() if f.is_file()))
        elif p.is_file():
            out.append(p)
        else:
            print(f"[!] 跳过不存在的路径: {p}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", help="证书目录或单个证书文件")
    ap.add_argument("--user-file", default=None, help="显式声明为用户库文件")
    ap.add_argument("--quiet", action="store_true", help="只输出可疑项")
    args = ap.parse_args()

    files = collect(args.targets)
    rows, suspicious = [], []

    for f in files:
        cert = load_cert(f)
        kind = store_kind(f, "user" if args.user_file and str(f) == args.user_file else None)
        if cert is None:
            rows.append((kind, f.name, "<无法解析>", "", "", ""))
            continue
        subject = cert.subject.rfc4514_string()
        issuer = cert.issuer.rfc4514_string()
        fp = hashlib.sha256(cert.tbs_certificate_bytes).hexdigest()[:16]
        rows.append((
            kind,
            f.name,
            subject,
            issuer,
            cert.not_valid_before_utc.strftime("%Y-%m-%d"),
            fp,
        ))

        blob = (subject + " " + issuer).lower()
        hit = [p for p in MITM_PATTERNS if p in blob]
        if kind == "user":
            suspicious.append((f.name, subject, "位于用户证书库（MITM 安装痕迹）"))
        elif hit:
            suspicious.append((f.name, subject, f"subject/issuer 命中抓包工具字样: {','.join(hit)}"))

    print(f"== 扫描 {len(rows)} 个证书 ==")
    by_kind = {}
    for r in rows:
        by_kind[r[0]] = by_kind.get(r[0], 0) + 1
    for k, v in sorted(by_kind.items()):
        print(f"   {k:<8} {v}")

    if not args.quiet:
        print("\n== 明细 ==")
        for kind, name, subject, issuer, nb, fp in rows:
            if kind == "system":
                continue
            print(f"   [{kind}] {name}")
            print(f"        subject: {subject}")
            print(f"        issuer : {issuer}")
            print(f"        notBefore: {nb}   fp: {fp}")

    print(f"\n== 可疑项 {len(suspicious)} 条 ==")
    if not suspicious:
        print("   （无）")
    for name, subject, why in suspicious:
        print(f"   [!] {name}  ->  {why}")
        print(f"       {subject}")

    print("\n提示：用户库非空 = 设备已被手动装过根证书，这是最容易被风控识别的形态。")
    print("      系统库中若非原厂证书，应改用 bind-mount 方式临时挂载，用完即卸。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
