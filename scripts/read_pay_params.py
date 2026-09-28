"""官方收银台支付参数串读取 CLI（支付宝浏览器支付 Python 脚本的数据入口，零第三方依赖）。

数据源：account_system/data/app.db 的 pay_param_records 表（param_str = JSON v1 紧凑原文，
与服务端 services/pay_params.py 同契约；本脚本不 import 服务端模块——独立进程/环境可运行）。

用法：
    python scripts/read_pay_params.py <order_no>                        # 打印紧凑 JSON 参数串（与库内存储逐字一致）
    python scripts/read_pay_params.py <order_no> --pretty               # 缩进展示（人读）
    python scripts/read_pay_params.py <order_no> --field session        # 取单个支付参数（session/utdid/tid/...）
    python scripts/read_pay_params.py <order_no> --url                  # 只取原始收银台 URL
    python scripts/read_pay_params.py --url-parse "<cashier_url>"       # 直接解析收银台 URL（不查库）

可 import 复用（后续浏览器支付脚本）：
    from read_pay_params import load_param_str, parse_cashier_url
    doc = load_param_str(param_str_or_db_row)   # → dict，doc["params"]["session"] 等

退出码：0 成功；1 未找到/参数不全；2 用法错误。
"""

import argparse
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path

# 契约常量（与 account_system/server/services/pay_params.py 同口径，两处勿漂移）
PARAM_STR_VERSION = 1
CASHIER_HOST = "mclient.alipay.com"
CASHIER_PATH = "/cashierRoutePay.htm"
REQUIRED_PARAMS = ("session", "utdid", "tid")   # mobilegw 设备级三元组，缺一不可

DB_PATH = Path(__file__).resolve().parents[1] / "account_system" / "data" / "app.db"


def parse_cashier_url(url: str) -> dict | None:
    """官方收银台 URL → {"base_url", "params"}；形态不符/缺必需三元组返回 None。"""
    url = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc != CASHIER_HOST or parts.path != CASHIER_PATH:
            return None
        params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        if any(not params.get(k) for k in REQUIRED_PARAMS):
            return None
        return {"base_url": f"https://{CASHIER_HOST}{CASHIER_PATH}", "params": params}
    except Exception:
        return None


def load_param_str(param_str: str) -> dict:
    """支付参数串（JSON v1）→ dict；格式不符抛 ValueError（显式失败，不做静默降级）。"""
    doc = json.loads(param_str)   # JSONDecodeError 是 ValueError 子类
    if not isinstance(doc, dict) or doc.get("v") != PARAM_STR_VERSION:
        raise ValueError(f"支付参数串版本不符：期望 v={PARAM_STR_VERSION}")
    params = doc.get("params")
    if not isinstance(params, dict):
        raise ValueError("支付参数串缺少 params 对象")
    missing = [k for k in REQUIRED_PARAMS if not params.get(k)]
    if missing:
        raise ValueError("支付参数串缺少必需参数：" + "/".join(missing))
    return doc


def fetch_param_str(order_no: str, db_path: Path = DB_PATH) -> str:
    """按订单号读库取参数串原文；无库/无表/无记录抛 FileNotFoundError（表由服务端
    init_db() 的 create_all 建——服务未重启升级时表尚不存在属正常态）。"""
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)   # 只读打开，不锁业务库
    try:
        try:
            row = conn.execute(
                "SELECT param_str FROM pay_param_records WHERE order_no = ?", (order_no,)
            ).fetchone()
        except sqlite3.OperationalError as e:   # no such table：服务端尚未重启建表
            raise FileNotFoundError(
                f"pay_param_records 表尚未建立（服务端升级重启后自动创建）：{e}") from e
    finally:
        conn.close()
    if not row or not row[0]:
        raise FileNotFoundError(f"订单 {order_no} 无支付参数串记录（尚未捕获/铸造官方收银台链接）")
    return row[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="官方收银台支付参数串读取（JSON v1）")
    ap.add_argument("order_no", nargs="?", help="系统订单号（order_records.order_no）")
    ap.add_argument("--pretty", action="store_true", help="缩进展示（默认紧凑原文）")
    ap.add_argument("--field", help="取 params 中的单个支付参数（如 session/utdid/tid）")
    ap.add_argument("--url", action="store_true", help="只输出原始收银台 URL（cashier_url）")
    ap.add_argument("--url-parse", dest="url_parse", help="直接解析给定收银台 URL（不查库）")
    args = ap.parse_args(argv)

    if args.url_parse:
        parsed = parse_cashier_url(args.url_parse)
        if not parsed:
            print("err: 收银台 URL 形态不符或缺必需参数（session/utdid/tid）", file=sys.stderr)
            return 1
        print(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")))
        return 0

    if not args.order_no:
        ap.print_usage()
        print("err: 需要 order_no（或 --url-parse "<url>"）", file=sys.stderr)
        return 2

    try:
        param_str = fetch_param_str(args.order_no)
        doc = load_param_str(param_str)
    except FileNotFoundError as e:
        print(f"err: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"err: {e}", file=sys.stderr)
        return 1

    if args.url:
        print(doc.get("cashier_url", ""))
        return 0
    if args.field:
        v = (doc.get("params") or {}).get(args.field)
        if v is None:
            print(f"err: params 中无字段 {args.field}", file=sys.stderr)
            return 1
        print(v)
        return 0
    print(json.dumps(doc, ensure_ascii=False, indent=2) if args.pretty else param_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
