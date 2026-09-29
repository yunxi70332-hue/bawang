"""待支付订单轮询 · 官方收银台半自动拉起（人工付款，授权保留）

定位（2026-09-29）：支付终端 V4「通道 A」的**半自动**形态——自动化只做
「发现待付订单 + 拉起收银台 + 提醒」，支付密码永远由人工输入（授权不旁路；
密码注入/无人值守扣款不做，见 docs/payment_terminal_v4_analysis_20260928.md §6）。

数据源：本地 SQLite 只读（pay_sessions issued 态 × pay_param_records），零上游协议
调用、零登录态依赖；收银台 URL 未铸造时静默等待，铸出后自动开页。

用法：
    python scripts/pay_watchdog.py                     # 常驻轮询（10s），铸出即开页+响铃
    python scripts/pay_watchdog.py --once --dry-run    # 单扫，只打印不动作
    python scripts/pay_watchdog.py --interval 5 --no-open   # 只提醒不开页
状态去重 output/pay_watchdog_state.json：同单重铸新链接会重新拉起（短窗过期的正确行为）。
"""

import argparse
import json
import sqlite3
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "account_system" / "data" / "app.db"
STATE_PATH = ROOT / "output" / "pay_watchdog_state.json"
TIME_FMT = "%Y-%m-%d %H:%M:%S"

try:
    import winsound  # Windows 提示音；非 Windows 静默降级

    def _beep():
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
except ImportError:  # pragma: no cover
    def _beep():
        pass


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"opened": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def pending_orders(db_path: Path) -> list[dict]:
    """issued 且未过支付截止的会话，按截止时间升序（最紧急在前）。
    收银台 URL 优先取 pay_param_records 解析结果（强校验过的短窗），回退会话列。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT s.order_no, s.account_id, s.pay_amount, s.pay_deadline, s.alipay_cashier_url,"
            "       p.param_str, a.label AS account_label"
            " FROM pay_sessions s"
            " LEFT JOIN pay_param_records p ON p.order_no = s.order_no"
            " LEFT JOIN chagee_accounts a ON a.id = s.account_id"
            " WHERE s.status = 'issued' ORDER BY s.pay_deadline ASC").fetchall()
    finally:
        conn.close()
    now = datetime.now()
    out = []
    for r in rows:
        try:
            deadline = datetime.strptime(str(r["pay_deadline"]), TIME_FMT)
        except (TypeError, ValueError):
            deadline = None
        if deadline is not None and deadline <= now:
            continue   # 已过窗（后台校准线程会收口），跳过
        cashier_url = ""
        if r["param_str"]:
            try:
                doc = json.loads(r["param_str"])
                if doc.get("v") == 1:
                    cashier_url = doc.get("cashier_url", "")
            except ValueError:
                pass
        out.append({"order_no": r["order_no"], "account": r["account_label"] or f"#{r['account_id']}",
                    "pay_amount": r["pay_amount"] or "", "deadline": deadline,
                    "cashier_url": cashier_url or (r["alipay_cashier_url"] or "")})
    return out


def _fmt_remaining(deadline: datetime | None) -> str:
    if deadline is None:
        return "--:--"
    sec = max(0, int((deadline - datetime.now()).total_seconds()))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def scan(state: dict, *, open_pages: bool, max_opens: int, beep: bool, dry_run: bool) -> int:
    """一轮扫描：打印待付单；对「未拉起过或链接已重铸」的已捕获单开页。返回待付单数。"""
    orders = pending_orders(DB_PATH)
    if not orders:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 无待支付订单")
        return 0
    opened = state.setdefault("opened", {})
    n_open = 0
    for o in orders:
        flag = "收银台已捕获" if o["cashier_url"] else "收银台铸造中…"
        print(f"[待支付] {o['order_no']} 账号={o['account']} 金额=¥{o['pay_amount']} "
              f"剩余={_fmt_remaining(o['deadline'])} {flag}")
        url = o["cashier_url"]
        if not url or not open_pages or dry_run:
            continue
        url_key = url[:120]   # 同链接只开一次；重铸后链接变化会再开（短窗语义）
        if opened.get(o["order_no"]) == url_key:
            continue
        if n_open >= max_opens:
            print(f"          （本轮开页已达上限 {max_opens}，下轮继续）")
            break
        webbrowser.open(url)
        opened[o["order_no"]] = url_key
        n_open += 1
        print(f"          → 已在浏览器拉起官方收银台，请人工输入支付密码完成付款")
    if n_open and beep:
        _beep()
    if n_open and not dry_run:
        _save_state(STATE_PATH, state)
    return len(orders)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="待支付订单轮询 + 官方收银台半自动拉起（人工付款）")
    ap.add_argument("--interval", type=float, default=10.0, help="轮询间隔秒（默认 10）")
    ap.add_argument("--once", action="store_true", help="单次扫描后退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不开页不响铃不写状态")
    ap.add_argument("--no-open", action="store_true", help="只提醒（响铃+打印），不开浏览器")
    ap.add_argument("--no-beep", action="store_true", help="关闭提示音")
    ap.add_argument("--max-opens", type=int, default=3, help="单轮最多开页数（防标签风暴，默认 3）")
    args = ap.parse_args(argv)

    if not DB_PATH.exists():
        print(f"err: 数据库不存在 {DB_PATH}", file=sys.stderr)
        return 1
    state = _load_state(STATE_PATH)
    try:
        while True:
            scan(state, open_pages=not args.no_open, max_opens=args.max_opens,
                 beep=not args.no_beep, dry_run=args.dry_run)
            if args.once:
                return 0
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print("\n已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
