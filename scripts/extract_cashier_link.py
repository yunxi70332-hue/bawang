#!/usr/bin/env python3
"""从 mitmproxy flows 抓包中提取云手机茶姬 App 内嵌支付宝 H5 收银台链接。

链路背景（2026-09-26/27 抓包定案）
==================================
茶姬 App 拉起支付时，App 内 WebView 会 GET：
    https://mclient.alipay.com/cashierRoutePay.htm?route_pay_from=h5&init_from=SDKLite
        &session=<mobilegw 会话>&utdid=<设备>&tid=<隧道>&cc=y
该 URL 就是"App 内 H5 收银台"的入口（App 内登录态 + 支付密码完成实付；会话短窗有效，
新设备/浏览器打开有登录墙与设备风控）。本脚本从 flows 抓包文件中把这条链接提取出来，
可选回填到管理系统该订单的支付会话（--feed，短窗内浏览器可直开）。

为什么不用 mitmproxy 库：本机唯一 venv（.venv_verify）未装 mitmproxy；flows 文件里
request.path 等字段以「长度前缀帧」明文存储（形如 b"4:path;220:/cashierRoutePay..."），
按长度切片即可零依赖精确提取（payload 内可含任意字节包括逗号，长度切片不受影响）。

用法
====
    # 扫描 capture/ 下最新的 flows 文件，列出全部收银台链接（含时间戳）
    python scripts/extract_cashier_link.py

    # 扫描全部 flows 文件
    python scripts/extract_cashier_link.py --all

    # 指定文件 + 轮询等待新链接（云手机拉起支付时自动捕获），最长 10 分钟
    python scripts/extract_cashier_link.py --flows capture\\xxx.flows --follow --deadline 600

    # 提取并回填到最新待支付订单的支付会话（operator=cloud-capture，记 cashier_updated 事件）
    python scripts/extract_cashier_link.py --feed
    python scripts/extract_cashier_link.py --feed --order-no 202609270910110025334248250
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_DIR = REPO_ROOT / "capture"

# request.path 字段帧：键名 "path"（4 字节）+ 长度前缀 + payload（切片精确，payload 可含任意字节）
_PATH_FIELD = re.compile(rb"4:path;(\d+):")
# 帧内时间戳（timestamp_start，秒；键名自身也是长度帧，数字部分取窗口内就近匹配）
_TS_FIELD = re.compile(rb"timestamp_start.{0,3}?(\d{10})\d*")
_LANDING_LOC = re.compile(rb"https://mclient\.alipay\.com/h5pay/landing/index\.html\?[^\x00-\x1f\"',]+")


def _slice_payloads(raw: bytes, key_re: re.Pattern[bytes], starts: bytes) -> list[bytes]:
    """按长度前缀切片提取指定键的 payload（精确长度，不受 payload 内特殊字节影响）。"""
    out = []
    for m in key_re.finditer(raw):
        n = int(m.group(1))
        start = m.end()
        payload = raw[start:start + n]
        if payload.startswith(starts):
            out.append((m.start(), payload))
    return out


def _nearest_ts(raw: bytes, offset: int, window: int = 4000) -> int | None:
    """就近向前找 timestamp_start（同一 flow 记录内必然存在，取窗口内最近一个）。"""
    lo = max(0, offset - window)
    best = None
    for m in _TS_FIELD.finditer(raw, lo, offset):
        best = int(m.group(1))
    return best


def extract_from_file(path: str | Path) -> list[dict]:
    """提取单个 flows 文件中的全部收银台链接（去重保序，按抓包时间排序）。"""
    raw = Path(path).read_bytes()
    hits = []
    seen_url: set[bytes] = set()
    for offset, payload in _slice_payloads(raw, _PATH_FIELD, b"/cashierRoutePay.htm?"):
        url = b"https://mclient.alipay.com" + payload
        if url in seen_url:
            continue
        seen_url.add(url)
        hits.append({
            "file": os.path.basename(str(path)),
            "ts": _nearest_ts(raw, offset),
            "url": url.decode("utf-8", "replace"),
        })
    hits.sort(key=lambda h: h["ts"] or 0)
    return hits


def _landing_of(path: str | Path) -> list[str]:
    raw = Path(path).read_bytes()
    return [m.group(0).decode("utf-8", "replace") for m in _LANDING_LOC.finditer(raw)][:5]


def newest_flows() -> Path | None:
    files = sorted(CAPTURE_DIR.glob("*.flows"), key=os.path.getmtime)
    return files[-1] if files else None


# ---------------- 回填到管理系统支付会话 ----------------

def feed_session(order_no: str | None, url: str, note: str = "") -> dict:
    """把提取的收银台 URL 写入最新待支付订单（或 --order-no 指定单）的 PaySession。"""
    sys.path.insert(0, str(REPO_ROOT / "account_system" / "server"))
    import database  # noqa: E402
    import models  # noqa: E402
    from services.pay_session import EVENT_CASHIER_UPDATED, get_by_order_no, record_event, token_prefix  # noqa: E402
    with database.SessionLocal() as db:
        if order_no:
            sess = get_by_order_no(db, order_no)
        else:
            sess = (db.query(models.PaySession)
                      .filter(models.PaySession.status == "issued")
                      .order_by(models.PaySession.id.desc()).first())
        if not sess:
            return {"ok": False, "error": "没有待支付（issued）的支付会话；先在管理端获取支付串"}
        sess.alipay_cashier_url = url[:512]
        db.commit()
        record_event(db, sess.order_no, token_prefix(sess.pay_token), EVENT_CASHIER_UPDATED,
                     {"url_prefix": url[:80], "operator": "cloud-capture", "note": note})
        return {"ok": True, "order_no": sess.order_no, "pay_token": sess.pay_token,
                "alipay_cashier_url": sess.alipay_cashier_url,
                "hint": "短窗有效：尽快在浏览器打开完成支付；壳页/info 已带该链接"}


# ---------------- 链接存活探测（只读 GET，与 alipay_autopay 只读口径一致） ----------------

def probe_url(url: str) -> dict:
    """GET 收银台链接判存活（2026-09-27 实证形态）：
    会话有效 → 302 → h5pay/landing（最终 URL 含 landing）；会话过期 → 200 原地渲染
    出错了/超时/systemError（最终 URL 仍是 cashierRoutePay，错误文案在页面中后段）。"""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; HUAWEI NXT-AL10) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/119.0.6045.134 Mobile Safari/537.36"})
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        with opener.open(req, timeout=10) as r:
            body = r.read(65536).decode("utf-8", "replace")
            alive = "h5pay/landing" in (r.url or "")
            gw = re.search(r"mobileclientgw-[\d-]+", body)
            err = ("出错了" in body) or ("systemError" in body) or ("超时" in body)
            return {"alive": alive, "final_url": r.url, "http": r.status,
                    "error_code": gw.group(0) if gw else "", "error_page": err}
    except Exception as e:
        return {"alive": False, "error": f"{type(e).__name__}: {e}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="提取云手机茶姬 App 内嵌支付宝 H5 收银台链接（零依赖 flows 扫描）")
    ap.add_argument("--flows", help="flows 文件路径（缺省取 capture/ 最新一个）")
    ap.add_argument("--all", action="store_true", help="扫描 capture/ 全部 flows 文件")
    ap.add_argument("--follow", action="store_true", help="轮询等待新收银台链接出现（云手机拉起支付时）")
    ap.add_argument("--deadline", type=int, default=600, help="--follow 最长等待秒数（默认 600）")
    ap.add_argument("--feed", action="store_true", help="提取后回填到支付会话（最新待支付单，或 --order-no 指定）")
    ap.add_argument("--order-no", help="--feed 指定订单号（缺省取最新 issued 会话）")
    ap.add_argument("--probe", action="store_true", help="对提取到的链接做只读存活探测"
                   "（302→landing=有效；出错页/超时=会话过期）")
    args = ap.parse_args(argv)

    if args.all:
        files = sorted(CAPTURE_DIR.glob("*.flows"), key=os.path.getmtime)
    else:
        f = Path(args.flows) if args.flows else newest_flows()
        files = [f] if f and f.exists() else []
    if not files:
        print("未找到 flows 文件（capture/ 目录为空或 --flows 路径不存在）")
        return 2

    known: set[str] = set()
    if not args.follow:
        alive_latest = None
        for f in files:
            for h in extract_from_file(f):
                known.add(h["url"])
                ts = time.strftime("%m-%d %H:%M:%S", time.localtime(h["ts"])) if h["ts"] else "?"
                print(f"[{ts}] {h['file']}\n  {h['url']}")
                if args.probe:
                    p = probe_url(h["url"])
                    print(f"    探测: alive={p['alive']}" + (f" {p.get('error_code')}" if p.get('error_code') else ""))
                    if p["alive"]:
                        alive_latest = h["url"]
        landings = _landing_of(files[-1])
        if landings:
            print("\n（同文件内 302 落地页样本，佐证链路：）")
            for l in landings[:1]:
                print("  ", l[:160], "...")
        if args.feed:
            target = alive_latest or (sorted(known)[-1] if known else None)
            if target:
                print("\n[feed]", json.dumps(feed_session(args.order_no, target),
                                             ensure_ascii=False, indent=1))
            else:
                print("\n[feed] 无可回填链接")
        return 0 if known else 1

    # --follow：轮询等待新链接（云手机上点支付 → WebView GET 出现新 session 链接）
    print(f"[follow] 轮询 {files[-1]}（每 2s，最长 {args.deadline}s）——请在云手机茶姬 App 内拉起支付…")
    deadline = time.time() + args.deadline
    base = {h["url"] for f in files for h in extract_from_file(f)}
    while time.time() < deadline:
        time.sleep(2)
        # 新抓包会话常开新 .flows 文件：每轮重选最新文件，自动跟随切换
        newest = newest_flows()
        if newest and str(newest) != str(files[-1]):
            print(f"[follow] 切换到更新的抓包文件: {newest.name}")
            files[-1] = newest
        for f in files[-1:]:
            for h in extract_from_file(f):
                if h["url"] in base:
                    continue
                ts = time.strftime("%H:%M:%S", time.localtime(h["ts"])) if h["ts"] else "?"
                print(f"[新收银台链接 {ts}]\n  {h['url']}")
                if args.probe:
                    p = probe_url(h["url"])
                    print(f"  探测: alive={p['alive']}" + (f" {p.get('error_code')}" if p.get('error_code') else ""))
                    if not p["alive"]:
                        print("  （会话无效，不回填；等下一次拉起支付的新 session）")
                        base.add(h["url"])
                        continue
                if args.feed:
                    print("[feed]", json.dumps(feed_session(args.order_no, h["url"]),
                                               ensure_ascii=False, indent=1))
                return 0
    print("[follow] 超时未捕获（确认云手机抓包正在写入该 flows 文件）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
