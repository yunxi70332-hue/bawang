#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_voucher_order_flow — 「霸王茶姬」10 元代金券指定饮品下单全链路实弹测试。

用纯协议（无 App/无浏览器自动化）发起一笔「使用 10 元代金券」的自取饮品订单，
创建后等待人工支付（支付宝侧扣款不在纯协议范围，本脚本绝不自动支付），
并把全链路的请求/响应/时间线/事件流/断言归档为一份 JSON 实证。

复用设施（不重复造轮子）：
  - scripts/chagee_trade_api.py    settle_direct / calculate_price / create_order /
                                   order_status / order_detail / order_list / cancel /
                                   verify / pick_coupon / coupon_face / PayLink / SettleResult
  - scripts/chagee_menu_api.py     store_goods_menu / goods_detail（游客无 token）
  - account_system/server/services/chagee_bridge.py   trade_api(account)（DB 是凭证唯一事实源）
  - account_system/server/services/pay_session.py     ensure_pay_session / build_h5_url /
                                   mark_session / record_event / upsert_order_record / EVENT_*
  - account_system/server/services/order_reconcile.py rollback_coupon_usage
  - 选品 OrderTarget 构造对齐 scripts/run_features_234.py + routers/orders.order_settle

流程（main 内 STEP 1~8）：
  前置（待支付单检查 / --cancel-pending 取消退券）→ 选品 → 试算选券（10 元代金券优先）→
  下单 createOrder + verify → 落库 OrderRecord + 铸支付会话 → 支付指引大字输出 →
  轮询等待（优先 8010 收银台 /pay/{token}/status，否则直连探针）→ 终态断言 + 归档。

用法（建议在仓库根 C:\\baidunetdiskdownload\\霸王茶姬 下运行）：
  .venv_verify\\Scripts\\python.exe scripts\\test_voucher_order_flow.py --drink 伯牙绝弦
  .venv_verify\\Scripts\\python.exe scripts\\test_voucher_order_flow.py --drink 伯牙绝弦 --cancel-pending --wait-minutes 20
  .venv_verify\\Scripts\\python.exe scripts\\test_voucher_order_flow.py --random --account-id 2
（从其它目录运行时用 venv 绝对路径即可；归档目录默认固定 <仓库根>\\output，与 CWD 无关。）

硬性约束：--drink 与 --random 必须显式给出其一；脚本不自动支付、不起 8000/8010 服务。
归档脱敏口径：token/sk 只留前 16 位；mobileEncrypt/mobile 密文截断；order_str 内 sign 截断。
主函数外只留常量与纯函数（离线可单测 sanitize/build_archive 等）；网络步骤均在 main 内包 try。
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
SERVER_DIR = os.path.join(PROJECT_ROOT, "account_system", "server")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from chagee_client import ChageeError, SessionExpiredError                  # noqa: E402
from chagee_menu_api import ChageeMenuApi                                    # noqa: E402
from chagee_trade_api import (                                               # noqa: E402
    ChageeTradeApi, ConsistencyError, OrderHangError, OrderOutcome,
)
# PayLink 类型仅用于文档语境（link = create_order 差额单返回值），不直接引用

# ---------------- 常量 ----------------

SCRIPT_VERSION = "1.0.0"
ARCHIVE_PREFIX = "test_voucher_order_"
DEFAULT_STORE_NO = "CN08121"
DEFAULT_STORE_NAME = "广东佛山顺德龙江沃达百货店"
PAY_PORTAL_BASE = "http://127.0.0.1:8010"
PORTAL_PROBE_TIMEOUT = 3.0          # 门户在线探测超时（3s）
PORTAL_STATUS_TIMEOUT = 6.0         # /pay/{token}/status 轮询超时
POLL_SECONDS = 2.0                  # 支付等待轮询间隔
EVENT_SNAPSHOT_SECONDS = 5.0        # PayEventLog 快照间隔
PORTAL_RECHECK_SECONDS = 30.0       # 门户离线时的重检间隔
OPERATOR = "test-script"
COUPON_KEYWORD = "10元代金券"
ORDER_STATUS_LABELS = {1: "待支付", 3: "制作中", 6: "已完成", 7: "已取消"}
SENSITIVE_KEYS = {"token", "authorization", "sk", "mobileencrypt", "mobile"}
_SIGN_LONG_RE = re.compile(r"sign=([^&]{33,})")
CALLBACK_CONFIG_PATH = os.path.join(PROJECT_ROOT, "account_system", "data",
                                    "pay_callback_config.json")


class AbortFlow(Exception):
    """流程中止（前置不满足/致命断言失败），携带面向用户的中文说明。"""


# ---------------- 纯函数（主函数外只留常量与纯函数；离线可单测） ----------------

def dec(v) -> Decimal:
    """宽松金额转 Decimal：None/'' 等异常输入回退 0。"""
    try:
        return Decimal(str(v if v not in (None, "") else "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def fmt_money(v) -> str:
    """金额两位小数字符串（展示用；异常回退原文）。"""
    try:
        return f"{Decimal(str(v)).quantize(Decimal('0.01'))}"
    except (InvalidOperation, ValueError, TypeError):
        return str(v or "")


def mask_token(s: str, keep: int = 16) -> str:
    """token/sk 脱敏：只留前 keep 位 + 长度标注。"""
    s = str(s or "")
    return (s[:keep] + f"...len={len(s)}") if len(s) > keep else s


def mask_phone(p: str) -> str:
    """手机号脱敏 3+4。"""
    p = str(p or "")
    return (p[:3] + "****" + p[-4:]) if len(p) >= 8 else p


def trunc_sign_in_str(s: str, keep: int = 32) -> str:
    """字符串内 order_str 的 sign 值截断（保留前 keep 字符 + 长度）。"""
    def _sub(m):
        return "sign=" + m.group(1)[:keep] + f"...(sign截断,len={len(m.group(1))})"
    return _SIGN_LONG_RE.sub(_sub, s)


def sanitize(obj):
    """递归脱敏：敏感键（token/authorization/sk/mobileEncrypt/mobile）掩码；字符串内 sign 截断。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and str(k).lower() in SENSITIVE_KEYS:
                if str(k).lower() in ("mobileencrypt", "mobile"):
                    out[k] = (v[:8] + "...(截断)") if len(v) > 8 else v
                else:
                    out[k] = mask_token(v)
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [sanitize(x) for x in obj]
    if isinstance(obj, str):
        return trunc_sign_in_str(obj)
    if isinstance(obj, (bool, int, float)) or obj is None:
        return obj
    return str(obj)


def expected_pay_amount(total, face) -> Decimal:
    """券后应付差额 = max(总额 − 券面额, 0)（面额≥总额时为零元单）。"""
    return max(dec(total) - dec(face), Decimal("0"))


def pick_voucher_coupon(available: list, total_trade_price, now_ms: int):
    """settlePrice 可用券里选「10元代金券」：templateName 含关键词者优先（多个取面额
    核验后最大者），否则回退 benefitText 面额最大的可用券。返回 (券条目或 None, 中文说明)。
    纯函数：now_ms 由调用方传入。"""
    total = dec(total_trade_price)
    usable = [e for e in (available or [])
              if e.get("canDiscount") is not False
              and (not e.get("useEndTime") or int(e["useEndTime"]) > now_ms)
              and ChageeTradeApi.coupon_threshold_ok(e, total)]
    if not usable:
        return None, (f"试算可用券 {len(available or [])} 张，均不可用/已过期/未达门槛")
    ten = [e for e in usable if COUPON_KEYWORD in str(e.get("templateName") or "")]
    if ten:
        best = max(ten, key=ChageeTradeApi.coupon_face)
        return best, (f"命中「{COUPON_KEYWORD}」候选 {len(ten)} 张，"
                      f"取 {best.get('templateName')}（benefitText={best.get('benefitText')}）")
    best = max(usable, key=ChageeTradeApi.coupon_face)
    return best, (f"无「{COUPON_KEYWORD}」，回退面额最大券 "
                  f"{best.get('templateName')}（benefitText={best.get('benefitText')}）")


def check_event_chain(events: list) -> dict:
    """事件链完整性（「回调通知完整性与准确性」的实证判据）：
    必须按序出现 link_issued → paid_detected → pickup_fetched；
    （page_opened/probe）为可选中段（探针成功路径本身不记 probe 事件）。"""
    names = [str(e.get("event")) for e in events]
    first: dict = {}
    for i, n in enumerate(names):
        first.setdefault(n, i)
    required = ["link_issued", "paid_detected", "pickup_fetched"]
    missing = [n for n in required if n not in first]
    order_ok = (not missing) and first["link_issued"] < first["paid_detected"] < first["pickup_fetched"]
    middle = [n for n in ("page_opened", "probe", "remint") if n in first]
    return {"ok": bool(order_ok), "missing": missing, "middle_observed": middle,
            "observed_sequence": names}


def build_archive(meta: dict, steps: list, timeline: list, events_snapshot: list,
                  assertions: list, verdict: str) -> dict:
    """归档 JSON 顶层结构组装（内容已由调用方 sanitize）。"""
    return {"meta": meta, "steps": steps, "timeline": timeline,
            "events_snapshot": events_snapshot, "assertions": assertions,
            "verdict": verdict}


def parse_deadline(expire_at):
    """支付宝 time_expire 文本 → datetime（与 pay_session._parse_deadline 同口径）。"""
    try:
        return datetime.strptime(str(expire_at or ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def price_positive(v) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def sku_bookable(sku: dict) -> bool:
    """可下单 SKU：salePrice>0 且 stock>0 且有规格组合。"""
    return (price_positive(sku.get("salePrice"))
            and int(sku.get("stock") or 0) > 0
            and bool(sku.get("specOptionInfos")))


def build_order_target(store_no: str, store_name: str, spu: dict, detail: dict, sku: dict) -> dict:
    """选品 → OrderTarget（对齐 run_features_234.feature3 与 routers/orders.order_settle）：
    SKU 原价 / specList 三键模板 / attributeList 默认项优先（名称保留原始空格）。"""
    spec_list = [{"specId": o.get("specId"), "specOptionId": o.get("specOptionId"),
                  "specOptionName": o.get("specOptionName")}
                 for o in (sku.get("specOptionInfos") or []) if o.get("specOptionId")]
    attr_list = []
    for g in detail.get("attributeInfos") or []:
        opts = g.get("attrOptions") or []
        chosen = next((o for o in opts if o.get("defaulted")), opts[0] if opts else None)
        if chosen:
            attr_list.append({
                "attributeId": str(g.get("attributeId") or ""),
                "attributeName": g.get("name") or "",
                "attributeOptionId": str(chosen.get("attributeOptionId") or ""),
                "attributeOptionName": chosen.get("name") or "",
            })
    spec_desc = "/".join(str(o.get("specOptionName") or "")
                         for o in (sku.get("specOptionInfos") or []))
    spu_name = str(spu.get("name") or "")
    images = detail.get("imageUrlList")
    image_url = str(images[0] or "") if isinstance(images, list) and images else ""
    return {
        "storeNo": store_no,
        "storeName": store_name,
        "spuId": spu.get("spuId"),
        "spuName": spu_name,
        "skuId": sku.get("skuId"),
        "skuName": sku.get("name") or spu_name,       # [99997] 校验：SKU 名缺失回退 SPU 名
        "itemSkuId": sku.get("itemSkuId"),
        "quantity": 1,
        "salePrice": sku.get("salePrice"),            # SKU 原价（calculatePrice 入参）
        "specList": spec_list,
        "attributeList": attr_list,
        "imageUrl": image_url,
        "spuType": detail.get("spuType") or "stand",
        "nutritionInfo": detail.get("nutritionInfo") or sku.get("nutritionInfo"),
        "specDesc": spec_desc,
    }


# ---------------- 主流程（网络步骤全部在 main 内，包 try 给中文修复提示） ----------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="test_voucher_order_flow.py",
        description="「霸王茶姬」10 元代金券指定饮品下单全链路实弹测试（纯协议，只建单等待人工支付，绝不自动扣款）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("运行示例（建议在仓库根目录下）:\n"
                "  .venv_verify\\Scripts\\python.exe scripts\\test_voucher_order_flow.py --drink 伯牙绝弦\n"
                "  .venv_verify\\Scripts\\python.exe scripts\\test_voucher_order_flow.py --drink 伯牙绝弦 "
                "--cancel-pending --wait-minutes 20\n"
                "  .venv_verify\\Scripts\\python.exe scripts\\test_voucher_order_flow.py --random --store-no CN08121\n"
                "归档: <仓库根>\\output\\test_voucher_order_<时间戳>.json"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--drink", help="饮品名称关键词（子串匹配 SPU name；多命中取首个并打印全部命中）")
    g.add_argument("--random", action="store_true",
                   help="全菜单随机选一个在售单品（与 --drink 二选一，必给其一）")
    ap.add_argument("--account-id", type=int, default=None,
                    help="DB 账号 ID（默认取首个 online 账号）")
    ap.add_argument("--store-no", default=DEFAULT_STORE_NO,
                    help=f"门店编号（默认 {DEFAULT_STORE_NO} {DEFAULT_STORE_NAME}）")
    ap.add_argument("--cancel-pending", action="store_true",
                    help="存在待支付单时先取消并退券（operator=test-script），否则遇待支付单直接中止")
    ap.add_argument("--wait-minutes", type=int, default=15,
                    help="支付等待窗口（分钟，默认 15；窗口内每 2s 轮询）")
    ap.add_argument("--archive-dir", default=None, help="归档目录（默认 <仓库根>/output）")
    args = ap.parse_args()
    if args.wait_minutes <= 0:
        ap.error("--wait-minutes 必须为正整数")

    try:  # Windows 控制台中文/¥ 符号兜底
        if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    archive_dir = args.archive_dir or os.path.join(PROJECT_ROOT, "output")
    started = datetime.now()
    steps: list = []
    timeline: list = []
    assertions: list = []
    events_snapshot: list = []
    verdict = "error"
    account = None
    db = None
    portal_online = False

    def record_step(name: str, req=None, resp=None):
        steps.append({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                      "name": name, "request": sanitize(req), "response": sanitize(resp)})

    def tl(kind: str, **kw):
        entry = {"ts": datetime.now().strftime("%H:%M:%S"), "kind": kind}
        entry.update(kw)
        timeline.append(entry)

    def check(name: str, ok, detail="") -> bool:
        assertions.append({"name": name, "pass": bool(ok), "detail": str(detail)[:600]})
        print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))
        return bool(ok)

    def banner(title: str):
        print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)

    def http_get_json(url: str, timeout: float) -> dict:
        """urllib GET → JSON（venv 无 requests，一律 urllib）。"""
        req = urllib.request.Request(url, method="GET",
                                     headers={"user-agent": "test_voucher_order_flow/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def portal_alive(timeout: float = PORTAL_PROBE_TIMEOUT) -> bool:
        """检测 8010 收银台门户是否在线（GET /，3s 超时，任何异常视为离线）。"""
        try:
            return bool(http_get_json(PAY_PORTAL_BASE + "/", timeout).get("ok"))
        except Exception:
            return False

    def read_callback_urls() -> list:
        """读 account_system/data/pay_callback_config.json 的回调 URLs（缺文件/无 URLs 为空）。"""
        try:
            with open(CALLBACK_CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            return [str(u) for u in (cfg.get("urls") or cfg.get("URLs") or [])]
        except Exception:
            return []

    def finish() -> None:
        """写归档 JSON（无论成败必写）并打印收尾摘要；main 的 finally 唯一调用点。
        steps 已在 record_step 时脱敏；timeline/events/assertions 在此统一脱敏。"""
        os.makedirs(archive_dir, exist_ok=True)
        meta = {
            "script": "test_voucher_order_flow.py",
            "script_version": SCRIPT_VERSION,
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0],
            "argv": {k: v for k, v in vars(args).items()},
            "account": ({"id": account.id, "label": account.label,
                         "nickname": account.nickname, "phone": mask_phone(account.phone),
                         "status": account.status,
                         "token_fingerprint": mask_token(account.token)}
                        if account is not None else None),
            "store": {"store_no": args.store_no,
                      "store_name": (DEFAULT_STORE_NAME
                                     if args.store_no == DEFAULT_STORE_NO else "")},
        }
        archive = build_archive(meta, steps, sanitize(timeline), sanitize(events_snapshot),
                                sanitize(assertions), verdict)
        path = os.path.join(
            archive_dir, f"{ARCHIVE_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(archive, f, ensure_ascii=False, indent=2, default=str)
        passed = sum(1 for a in assertions if a["pass"])
        print("\n" + "=" * 78)
        print(f"归档已写入: {path}")
        print(f"断言: {passed}/{len(assertions)} 通过 | 结论: {verdict}")
        print("=" * 78)

    print(f"test_voucher_order_flow v{SCRIPT_VERSION} — 10 元代金券下单全链路实弹测试")
    print(f"参数: drink={args.drink!r} random={args.random} store={args.store_no} "
          f"account_id={args.account_id or '(首个online)'} cancel_pending={args.cancel_pending} "
          f"wait={args.wait_minutes}min")

    try:
        # ---- 服务端模块懒加载（路径注入方式参考 chagee_bridge；DB 是凭证唯一事实源）----
        banner("[STEP 1] 前置：账号加载 + 待支付单检查")
        if SERVER_DIR not in sys.path:
            sys.path.insert(0, SERVER_DIR)
        from database import SessionLocal                                   # noqa: E402
        from models import (ChageeAccount, CouponRecord, CouponUsageLog,    # noqa: E402
                            OrderRecord, PayEventLog)
        from services import chagee_bridge as bridge                        # noqa: E402
        from services import pay_session as ps                              # noqa: E402
        from services.order_reconcile import rollback_coupon_usage          # noqa: E402

        db = SessionLocal()

        if args.account_id:
            account = db.get(ChageeAccount, args.account_id)
            if account is None:
                raise AbortFlow(f"DB 中不存在 account_id={args.account_id}")
        else:
            account = (db.query(ChageeAccount)
                       .filter(ChageeAccount.status == "online")
                       .order_by(ChageeAccount.id).first())
            if account is None:
                raise AbortFlow("DB 无 online 账号：请先在管理系统完成短信登录，或用 --account-id 指定")
        if account.status == "disabled":
            raise AbortFlow(f"账号 #{account.id} 已停用，无法执行协议操作")
        if not (account.token or ""):
            raise AbortFlow(f"账号 #{account.id} 无 token（尚未登录）")
        record_step("前置-账号加载", {"account_id": args.account_id or "(auto:首个online)"},
                    {"id": account.id, "label": account.label, "nickname": account.nickname,
                     "phone": mask_phone(account.phone), "customer_id": account.customer_id,
                     "status": account.status, "token_fingerprint": mask_token(account.token)})
        print(f"  账号: #{account.id} {account.label}（{account.nickname}，{mask_phone(account.phone)}）")

        api = bridge.trade_api(account)

        # ---- 闭包：本地探针 / 事件流 / 券状态（口径对齐 payportal._probe_remote 与 reconcile）----

        def local_coupon_of(order_no: str) -> str:
            db.rollback()
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).first()
            if rec and rec.coupon_code:
                return str(rec.coupon_code)
            sess0 = ps.get_by_order_no(db, order_no)
            return str(sess0.coupon_code) if (sess0 and sess0.coupon_code) else ""

        def token_pfx_of(order_no: str) -> str:
            db.rollback()
            sess0 = ps.get_by_order_no(db, order_no)
            return ps.token_prefix(sess0.pay_token) if sess0 else ""

        def read_events(order_no: str) -> list:
            db.rollback()   # 结束读事务，跨进程（8010/watcher）写入的事件对本会话可见
            rows = (db.query(PayEventLog).filter(PayEventLog.order_no == order_no)
                    .order_by(PayEventLog.id).all())
            return [{"id": r.id,
                     "ts": r.created_at.strftime("%H:%M:%S") if r.created_at else "",
                     "event": r.event, "payload": r.payload or {}} for r in rows]

        def coupon_state_of(coupon_code: str) -> dict:
            db.rollback()
            rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).first()
            if not rec:
                return {"exists": False}
            return {"exists": True, "bucket": rec.bucket,
                    "last_used_at": (rec.last_used_at.strftime("%Y-%m-%d %H:%M:%S")
                                     if rec.last_used_at else None),
                    "last_order_no": rec.last_order_no,
                    "reset_ok": rec.last_used_at is None and rec.bucket == "effective"}

        def sync_remote_state(order_no: str, coupon_code: str, source: str):
            """直连探针 + 跃迁联动（口径同 payportal._probe_remote；8010 离线/失败时兜底）：
            3/6 → CAS 置 paid + order_detail 取 pickupNo 回写；7 → cancelled + 券回滚。
            返回 (会话态, pickup)。"""
            pfx = token_pfx_of(order_no)
            try:
                st = int(api.order_status(order_no) or 0)
            except Exception as e:
                ps.record_event(db, order_no, pfx, ps.EVENT_PROBE,
                                {"ok": False, "source": source,
                                 "error": f"{type(e).__name__}: {e}"[:200]})
                return "issued", None
            if st in (3, 6):
                ps.record_event(db, order_no, pfx, ps.EVENT_PAID_DETECTED,
                                {"order_status": st, "source": source})
                ps.mark_session(db, order_no, "paid", paid_at=datetime.now())
                db.rollback()
                sess = ps.get_by_order_no(db, order_no)
                pickup = str(sess.pickup_no) if (sess and sess.pickup_no) else ""
                detail = {}
                if sess and not sess.pickup_no:
                    try:
                        detail = api.order_detail(order_no)
                        if detail.get("pickupNo"):
                            sess.pickup_no = str(detail["pickupNo"])
                            db.commit()
                            pickup = sess.pickup_no
                    except Exception as e:
                        ps.record_event(db, order_no, pfx, ps.EVENT_PROBE,
                                        {"ok": False, "stage": "order_detail", "source": source,
                                         "error": f"{type(e).__name__}: {e}"[:200]})
                if detail:
                    ps.upsert_order_record(db, account.id, order_no, status=st,
                                           status_label=ORDER_STATUS_LABELS.get(st, str(st)),
                                           pickup_no=pickup or None,
                                           pay_amount=str(detail.get("payAmount") or "") or None,
                                           total_amount=str(detail.get("totalAmount") or "") or None)
                ps.record_event(db, order_no, pfx, ps.EVENT_PICKUP_FETCHED,
                                {"pickup_no": pickup or "", "order_status": st, "source": source})
                return "paid", pickup
            if st == 7:
                if ps.mark_session(db, order_no, "cancelled"):
                    ps.record_event(db, order_no, pfx, ps.EVENT_ORDER_CANCELLED,
                                    {"source": source, "order_status": 7})
                    ps.upsert_order_record(db, account.id, order_no, status=7,
                                           status_label=ORDER_STATUS_LABELS[7])
                    if coupon_code:
                        try:
                            if rollback_coupon_usage(db, coupon_code, order_no, operator=OPERATOR):
                                ps.record_event(db, order_no, pfx, ps.EVENT_ROLLED_BACK,
                                                {"coupon_code": coupon_code, "operator": OPERATOR})
                        except Exception:
                            db.rollback()
                return "cancelled", None
            return "issued", None

        def cancel_order_and_rollback(order_no: str, coupon_code: str, source: str) -> dict:
            """取消订单 + 本地置取消 + 券回滚 + 会话置 cancelled + 事件落库（operator=test-script）。"""
            cancel_resp = api.cancel(order_no)
            ps.upsert_order_record(db, account.id, order_no, status=7,
                                   status_label=ORDER_STATUS_LABELS[7])
            pfx = token_pfx_of(order_no)
            ps.record_event(db, order_no, pfx, ps.EVENT_ORDER_CANCELLED,
                            {"source": source, "operator": OPERATOR})
            rolled = False
            if coupon_code:
                try:
                    rolled = rollback_coupon_usage(db, coupon_code, order_no, operator=OPERATOR)
                except Exception:
                    db.rollback()
                if rolled:
                    ps.record_event(db, order_no, pfx, ps.EVENT_ROLLED_BACK,
                                    {"coupon_code": coupon_code, "operator": OPERATOR})
            ps.mark_session(db, order_no, "cancelled")
            return {"cancel_response": cancel_resp, "coupon_rolled_back": rolled,
                    "coupon_state": coupon_state_of(coupon_code) if coupon_code else None}

        def run_final_assertions(final: str, order_no: str, coupon_code: str,
                                 expected_pay, final_detail: dict):
            """STEP 8 终态断言（paid/zero_paid 与 cancelled 两套口径，逐条 pass/fail）。"""
            db.rollback()
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).first()
            sessf = ps.get_by_order_no(db, order_no)
            events = read_events(order_no)
            names = [e["event"] for e in events]
            promos = final_detail.get("orderPromotions") or []
            promo_hit = [p for p in promos if str(p.get("promotionId")) == str(coupon_code)]
            if final in ("paid", "zero_paid"):
                pickup = (str(final_detail.get("pickupNo") or "")
                          or (str(sessf.pickup_no) if sessf else ""))
                if final == "paid":
                    check("终态-取餐号非空", bool(pickup), pickup or "(空)")
                else:
                    check("终态-取餐号", True,
                          pickup or "(零元单制作中，取餐码可稍后在订单详情查看)")
                check("终态-实付金额=试算应付",
                      dec(final_detail.get("payAmount")) == dec(expected_pay),
                      f"实际 {final_detail.get('payAmount')}，期望 {fmt_money(expected_pay)}")
                check("终态-券核销promotionId==券码", bool(promo_hit),
                      f"promotions={[str(p.get('promotionId')) for p in promos]}，券码={coupon_code}")
                if final == "paid":
                    chain = check_event_chain(events)
                    check("终态-事件链完整(link_issued→(page_opened/probe)→paid_detected→pickup_fetched)",
                          chain["ok"],
                          f"observed={'->'.join(names) or '(空)'}；missing={chain['missing']}；"
                          f"middle={chain['middle_observed']}")
                    urls = read_callback_urls()
                    if portal_online and urls:
                        check("终态-回调已分发(callback_dispatched)",
                              "callback_dispatched" in names,
                              f"callback_urls={urls}；observed={'->'.join(names) or '(空)'}")
                    else:
                        check("终态-回调分发(条件不满足跳过)", True,
                              f"portal_online={portal_online}，callback_urls={urls}")
                check("终态-OrderRecord回填一致",
                      bool(rec) and int(rec.status or 0) in (3, 6)
                      and (rec.pickup_no or "") == (pickup or "")
                      and dec(rec.pay_amount) == dec(expected_pay),
                      f"rec.status={getattr(rec, 'status', None)}，"
                      f"rec.pickup_no={getattr(rec, 'pickup_no', None)}，"
                      f"rec.pay_amount={getattr(rec, 'pay_amount', None)}")
            else:
                check("终态-订单已取消", final in ("cancelled", "expired"),
                      f"final={final}，order_status={final_detail.get('orderStatus')}")
                check("终态-rolled_back事件", "rolled_back" in names,
                      f"observed={'->'.join(names) or '(空)'}")
                cstate = coupon_state_of(coupon_code)
                check("终态-CouponRecord复位(last_used_at清空/bucket=effective)",
                      bool(cstate.get("reset_ok")), json.dumps(cstate, ensure_ascii=False))

        # ---- STEP 1 续：order_list(today) 查待支付单 ----
        today_rows = api.order_list("today")
        pendings = [r for r in today_rows if int(r.get("orderStatus") or 0) == 1]
        handled, blocked = [], []
        for r in pendings:
            ono = str(r.get("orderNo") or "")
            if not ono:
                continue
            st = int(api.order_status(ono) or 0)   # reconcile 口径探针（getOrderStatus 裸 int）
            cc = local_coupon_of(ono)
            if st != 1:
                sess_st, _ = sync_remote_state(ono, cc, source="precheck")
                handled.append({"order_no": ono, "remote_status": st,
                                "action": f"synced->{sess_st}", "coupon": cc or None})
                continue
            if not args.cancel_pending:
                blocked.append({"order_no": ono, "coupon": cc or None})
                continue
            res = cancel_order_and_rollback(ono, cc, source="cancel-pending")
            handled.append({"order_no": ono, "action": "cancelled+rollback",
                            "coupon": cc or None, **res})
            print(f"  已取消待支付单 {ono}（退券: {res['coupon_rolled_back']}，"
                  f"券复位: {res['coupon_state'].get('reset_ok') if res['coupon_state'] else 'N/A(无券单)'}）")
        record_step("前置-待支付检查", {"tabType": "today", "orderType": "1", "channelCode": "android"},
                    {"today_orders": len(today_rows), "pending_total": len(pendings),
                     "handled": handled, "blocked": blocked})
        if blocked:
            raise AbortFlow(f"账号今日存在待支付订单 {[b['order_no'] for b in blocked]}："
                            f"请先人工支付，或加 --cancel-pending 由本脚本取消并退券后重跑")
        print(f"  今日订单 {len(today_rows)} 笔，待支付 {len(pendings)} 笔"
              + ("（已全部处理）" if pendings else "，无阻塞"))

        # ---- STEP 2: 选品（游客菜单链，自取 saleType="1"）----
        banner("[STEP 2] 选品：门店菜单（自取）→ SPU → 可下单 SKU")
        menu_api = ChageeMenuApi()
        menu = menu_api.store_goods_menu(args.store_no)
        spus = [spu for cat in menu for spu in (cat.get("spuList") or [])]
        onsale = [s for s in spus if not s.get("saleOut") and str(s.get("name") or "").strip()]
        if not onsale:
            raise AbortFlow(f"门店 {args.store_no} 菜单无在售单品（共 {len(spus)} SPU）——"
                            f"门店可能打烊/菜单未上架，请换营业时段或 --store-no")
        if args.random:
            spu = random.choice(onsale)
            pick_note = f"随机选中（在售 {len(onsale)} 个 SPU）"
        else:
            kw = args.drink.strip()
            hits = [s for s in onsale if kw in str(s.get("name") or "")]
            if not hits:
                raise AbortFlow(f"菜单中未找到名称含「{kw}」的在售单品（在售 SPU 共 {len(onsale)} 个）："
                                f"请更换关键词/门店，或改用 --random")
            spu = hits[0]
            pick_note = f"关键词「{kw}」命中 {len(hits)} 个，取首个"
            print("  命中列表: " + " / ".join(str(s.get("name")) for s in hits))
        store_name = (DEFAULT_STORE_NAME if args.store_no == DEFAULT_STORE_NO
                      else f"store:{args.store_no}")
        detail = menu_api.goods_detail(spu["spuId"], args.store_no)
        sku = next((s for s in (detail.get("skuInfos") or []) if sku_bookable(s)), None)
        if sku is None:
            raise AbortFlow(f"「{spu.get('name')}」无可下单 SKU（需 salePrice>0 且 stock>0 且有规格）："
                            f"请换品或 --random")
        target = build_order_target(args.store_no, store_name, spu, detail, sku)
        goods_desc = (f"{target['spuName']} x{target['quantity']}"
                      + (f"（{target['specDesc']}）" if target["specDesc"] else ""))[:255]
        record_step("选品-菜单+SPU",
                    {"storeNo": args.store_no, "saleType": "1", "saleChannel": "2"},
                    {"categories": len(menu), "spu_total": len(spus), "onsale": len(onsale),
                     "pick": pick_note,
                     "spu": {"spuId": spu.get("spuId"), "name": spu.get("name"),
                             "stock": spu.get("stock"), "saleOut": spu.get("saleOut")}})
        record_step("选品-SKU", {"spuId": spu["spuId"], "storeNo": args.store_no},
                    {"skuId": sku.get("skuId"), "itemSkuId": sku.get("itemSkuId"),
                     "salePrice": sku.get("salePrice"), "stock": sku.get("stock"),
                     "specOptionInfos": sku.get("specOptionInfos"),
                     "attributeList": target["attributeList"], "goods_desc": goods_desc})
        print(f"  选中: {target['spuName']} | 规格[{target['specDesc']}] | "
              f"SKU价 ¥{fmt_money(sku.get('salePrice'))} | 库存 {sku.get('stock')} | {pick_note}")

        # ---- STEP 3: 试算选券 ----
        banner("[STEP 3] 试算：calculatePrice → settlePrice(无券) → 选 10 元代金券 → settlePrice(带券)")
        price = api.calculate_price(target)
        settle_base = api.settle_direct(target, price)   # 无券（recommendCoupon=True）
        record_step("试算-calculatePrice",
                    {"skuInfo": {"spuId": target["spuId"], "skuId": target["skuId"],
                                 "num": target["quantity"], "salePrice": target["salePrice"],
                                 "specList": target["specList"],
                                 "attributeList": target["attributeList"]},
                     "storeNo": args.store_no},
                    {"totalGoodsItemPrice": price.get("totalGoodsItemPrice"),
                     "totalTradePrice": price.get("totalTradePrice"),
                     "raw_keys": sorted(price.keys())})
        avail = settle_base.available_coupons
        record_step("试算-settlePrice(无券)",
                    {"goods": {"skuId": target["skuId"], "quantity": target["quantity"]},
                     "coupon": None, "recommendCoupon": True},
                    {"totalTradePrice": settle_base.total_trade_price,
                     "buyerRealPrice": settle_base.buyer_real_price,
                     "availableCouponCount": len(avail),
                     "confirmOrderKey": settle_base.confirm_order_key,
                     "raw": settle_base.raw})
        coupon_entry, coupon_note = pick_voucher_coupon(
            avail, settle_base.total_trade_price, int(time.time() * 1000))
        if coupon_entry is None:
            record_step("试算-选券", {"available": len(avail)}, {"chosen": None, "note": coupon_note})
            raise AbortFlow(f"无代金券可用于本单（{coupon_note}）：请确认账号已领 10 元代金券且在有效期内、"
                            f"满足门槛（当前总额 ¥{fmt_money(settle_base.total_trade_price)}），"
                            f"或换更贵饮品/门店后重试")
        coupon_code = str(coupon_entry.get("couponCode"))
        face = ChageeTradeApi.coupon_face(coupon_entry)
        record_step("试算-选券",
                    {"available": len(avail),
                     "strategy": f"templateName 含「{COUPON_KEYWORD}」优先，否则 benefitText 面额最大"},
                    {"chosen": coupon_entry, "face": str(face), "note": coupon_note,
                     "available_all": avail})
        print(f"  选券: {coupon_entry.get('templateName')}（code={coupon_code}，面额 {face}）— {coupon_note}")

        settle_coupon = api.settle_direct(target, price, coupon_entry=coupon_entry)
        total_c = dec(settle_coupon.total_trade_price)
        pay_c = dec(settle_coupon.buyer_real_price)
        est_ded = ChageeTradeApi.expected_deduction(coupon_entry, total_c)
        expected_pay = expected_pay_amount(total_c, est_ded)
        record_step("试算-settlePrice(带券)",
                    {"goods": {"skuId": target["skuId"], "quantity": target["quantity"]},
                     "coupon": {"couponCode": coupon_code,
                                "templateName": coupon_entry.get("templateName"),
                                "discountAmount": str(est_ded)},
                     "recommendCoupon": False},
                    {"totalTradePrice": settle_coupon.total_trade_price,
                     "buyerRealPrice": settle_coupon.buyer_real_price,
                     "totalDiscountAmount": (settle_coupon.trade_fund_info or {}).get("totalDiscountAmount"),
                     "discountList": settle_coupon.discount_list,
                     "expected_pay": str(expected_pay),
                     "raw": settle_coupon.raw})
        print(f"  无券试算: 总额 ¥{fmt_money(settle_base.total_trade_price)} "
              f"应付 ¥{fmt_money(settle_base.buyer_real_price)}")
        print(f"  带券试算: 总额 ¥{fmt_money(settle_coupon.total_trade_price)} "
              f"券抵 ¥{fmt_money(est_ded)} 应付 ¥{fmt_money(settle_coupon.buyer_real_price)}")
        trial_start = len(assertions)
        check("试算-带券总额与无券一致",
              total_c == dec(settle_base.total_trade_price),
              f"带券 {settle_coupon.total_trade_price} vs 无券 {settle_base.total_trade_price}")
        check(f"试算-应付=max(总额-预估抵扣,0)（券型感知）",
              pay_c == expected_pay,
              f"实际应付 {settle_coupon.buyer_real_price}，期望 {expected_pay}")
        if not all(a["pass"] for a in assertions[trial_start:]):
            raise AbortFlow("试算金额断言未通过（金额/抵扣口径不符），已中止，未创建订单")
        check("试算-选中券为10元代金券",
              COUPON_KEYWORD in str(coupon_entry.get("templateName") or "") or face == Decimal("10"),
              f"templateName={coupon_entry.get('templateName')}，"
              f"benefitText={coupon_entry.get('benefitText')}（回退券时此条为 FAIL，不影响流程）")

        # ---- STEP 4: 下单 + verify 口径 ----
        banner("[STEP 4] 下单：createOrder（带券折扣行）→ PayLink + verify 口径核对")
        # 抵扣行优先取服务端回填（折扣率券金额由茶姬计算回填），无回填退本地折率感知预估
        rows = settle_coupon.discount_rows_for(coupon_code) or [
            ChageeTradeApi.build_discount_row(
                coupon_entry,
                ChageeTradeApi.expected_deduction(coupon_entry, settle_coupon.total_trade_price))]
        try:
            outcome = api.create_order(settle_coupon, args.store_no, store_name, rows)
        except OrderHangError as e:
            record_step("下单-createOrder", {"storeNo": args.store_no, "discountList": rows},
                        {"error": f"OrderHangError: {e}"})
            raise AbortFlow(f"createOrder 结果不确定（{e}）：请先用订单列表核实是否成单；"
                            f"残留待支付单下次运行加 --cancel-pending 清理") from e

        if isinstance(outcome, OrderOutcome):
            # 零元直通分支（券面额≥总额）：无支付腿，直接制作中——走零元终态断言后收尾
            record_step("下单-createOrder(零元直通)",
                        {"storeNo": args.store_no, "storeName": store_name, "discountList": rows},
                        {"orderNo": outcome.order_no, "status": outcome.status,
                         "payAmount": outcome.pay_amount, "pickupNo": outcome.pickup_no})
            print(f"  零元单直通: {outcome.order_no} 状态 {outcome.status}"
                  f"({outcome.status_text}) 取餐码 {outcome.pickup_no or '(制作中稍后有)'}")
            try:
                verify_report = api.verify(outcome.order_no, outcome.pay_amount, coupon_code)
                check("下单-verify口径(payAmount+promotionId==券码)", True,
                      json.dumps(verify_report, ensure_ascii=False))
            except ConsistencyError as e:
                check("下单-verify口径(payAmount+promotionId==券码)", False, str(e))
            ps.upsert_order_record(db, account.id, outcome.order_no, store_no=args.store_no,
                                   store_name=store_name, goods_desc=goods_desc,
                                   quantity=target["quantity"], coupon_code=coupon_code,
                                   total_amount=settle_coupon.total_trade_price,
                                   pay_amount=outcome.pay_amount, scenario="zero",
                                   status=outcome.status,
                                   status_label=(outcome.status_text
                                                 or ORDER_STATUS_LABELS.get(outcome.status, "")),
                                   pickup_no=outcome.pickup_no)
            final_detail = api.order_detail(outcome.order_no)
            run_final_assertions("zero_paid", outcome.order_no, coupon_code,
                                 expected_pay, final_detail)
            events_snapshot = read_events(outcome.order_no)
            verdict = "pass" if all(a["pass"] for a in assertions) else "fail"
        else:
            link = outcome   # PayLink：差额单，等待人工支付
            record_step("下单-createOrder",
                        {"storeNo": args.store_no, "storeName": store_name,
                         "paymentInfo": {"payType": 60, "channelCode": "UnionPay",
                                         "payAmount": settle_coupon.buyer_real_price},
                         "discountList": rows,
                         "confirmOrderKey": settle_coupon.confirm_order_key},
                        {"orderNo": link.order_no, "payNo": link.pay_no,
                         "out_trade_no": link.out_trade_no,
                         "total_amount(支付宝侧实付)": link.total_amount,
                         "expire_at": link.expire_at, "order_str": link.order_str})
            print(f"  成单: orderNo={link.order_no} payNo={link.pay_no} "
                  f"支付宝流水={link.out_trade_no} 到期 {link.expire_at}")
            try:
                verify_report = api.verify(link.order_no, settle_coupon.buyer_real_price, coupon_code)
                check("下单-verify口径(payAmount+promotionId==券码)", True,
                      json.dumps(verify_report, ensure_ascii=False))
            except ConsistencyError as e:
                check("下单-verify口径(payAmount+promotionId==券码)", False, str(e))
                raise AbortFlow(f"成单校验未通过：{e}——订单已创建，请人工核对；"
                                f"下次运行加 --cancel-pending 可取消并退券")

            # ---- STEP 5: 落库 + 支付会话 ----
            banner("[STEP 5] 落库：OrderRecord + 券档案/使用日志 + ensure_pay_session")
            snapshot = {"target": target, "extra_list": [], "store_no": args.store_no,
                        "store_name": store_name, "goods_desc": goods_desc}
            ps.upsert_order_record(db, account.id, link.order_no, store_no=args.store_no,
                                   store_name=store_name, goods_desc=goods_desc,
                                   quantity=target["quantity"], coupon_code=coupon_code,
                                   total_amount=settle_coupon.total_trade_price,
                                   pay_amount=settle_coupon.buyer_real_price, scenario="partial",
                                   status=1, status_label=ORDER_STATUS_LABELS[1],
                                   out_trade_no=link.out_trade_no,
                                   pay_deadline=parse_deadline(link.expire_at),
                                   order_target=snapshot)
            # 券档案 + 使用痕迹（本地镜像 routers/orders 的 _upsert_coupon/_mark_coupon_used/
            # _log_coupon_usage 语义，operator 固定 test-script，供终态回滚断言有据可查）
            db.rollback()
            crec = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).first()
            if not crec:
                crec = CouponRecord(coupon_code=coupon_code)
                db.add(crec)
            crec.account_id = account.id
            crec.token_fingerprint = mask_token(account.token)
            crec.template_name = str(coupon_entry.get("templateName") or "")[:255]
            crec.benefit_text = str(coupon_entry.get("benefitText") or "")[:64]
            crec.amount = "" if ChageeTradeApi.coupon_kind(coupon_entry) == "rate" \
                else str(ChageeTradeApi.coupon_face(coupon_entry))
            crec.threshold_tips = str(coupon_entry.get("thresholdTips") or "")[:128]
            crec.use_end_time = (coupon_entry.get("useEndTime")
                                 if isinstance(coupon_entry.get("useEndTime"), int) else None)
            crec.bucket = "settle_available"
            crec.synced_from = "settle"
            crec.last_used_at = datetime.now()
            crec.last_order_no = link.order_no
            db.add(CouponUsageLog(
                coupon_code=coupon_code,
                coupon_name=str(coupon_entry.get("templateName") or "")[:255],
                account_id=account.id, account_label=f"{account.label}#{account.id}",
                operator=OPERATOR, order_no=link.order_no,
                deduction=fmt_money(min(face, total_c)), total_amount=fmt_money(total_c),
                pay_amount=fmt_money(pay_c), scenario="partial", result="success"))
            db.commit()
            sess = ps.ensure_pay_session(db, account.id, link.order_no, link,
                                         mode="partial", coupon_code=coupon_code)
            portal_url = ps.build_h5_url(sess.pay_token)
            cashier_url = sess.alipay_cashier_url or ""
            record_step("落库-OrderRecord+支付会话",
                        {"order_no": link.order_no, "coupon_code": coupon_code,
                         "mode": "partial", "order_target_snapshot": snapshot},
                        {"pay_token": sess.pay_token, "portal_url": portal_url,
                         "alipay_cashier_url": cashier_url,
                         "pay_deadline": str(sess.pay_deadline or ""),
                         "coupon_record": coupon_state_of(coupon_code)})
            print("  OrderRecord 已落库，支付会话已铸造（mode=partial，"
                  f"pay_token={sess.pay_token[:8]}...）")

            # ---- STEP 6: 支付指引（大字输出三样 + 门户检测）----
            banner("[STEP 6] 人工支付指引（本脚本不会自动扣款）")
            portal_online = portal_alive()
            record_step("指引-收银台门户在线检测",
                        {"url": PAY_PORTAL_BASE + "/", "timeout": PORTAL_PROBE_TIMEOUT},
                        {"online": portal_online})
            print("=" * 78)
            print("【方式一】支付宝官方收银台（浏览器打开 → 登录支付宝 → 付款）:")
            print("  " + (cashier_url if cashier_url
                          else "（未构造成功：autopay_config.json 缺收银台凭证，请用方式二/三）"))
            print("【方式二】壳页 portal_url（拉起App / 复制支付串 / 查看取餐码）:")
            print("  " + portal_url)
            if not portal_online:
                print(f"  !! 检测到收银台门户（{PAY_PORTAL_BASE}）离线：方式二暂不可用，")
                print("     建议先运行 account_system\\start_payportal.bat 再刷新壳页")
            print("【方式三】order_str（复制用，手机支付宝「扫一扫→相册/粘贴」）:")
            print("  " + link.order_str)
            print("=" * 78)
            print(f"※ 请在手机完成支付（金额 ¥{fmt_money(settle_coupon.buyer_real_price)}）"
                  f"—— 每 {POLL_SECONDS:.0f}s 轮询，最长等待 {args.wait_minutes} 分钟 ※")

            # ---- STEP 7: 支付等待 + 事件流快照 ----
            banner("[STEP 7] 支付等待（轮询 + PayEventLog 事件链观测）")
            status_url = f"{PAY_PORTAL_BASE}/pay/{sess.pay_token}/status"
            deadline = time.time() + args.wait_minutes * 60
            last_snap, last_names, last_portal_check = 0.0, [], time.time()
            last_printed_status, poll_n, final = None, 0, None
            while time.time() < deadline:
                poll_n += 1
                cur_status = None
                direct_probe = False
                if portal_online:
                    try:
                        resp = http_get_json(status_url, PORTAL_STATUS_TIMEOUT)
                        cur_status = str(resp.get("status") or "")
                        tl("poll", via="portal", n=poll_n, session_status=cur_status,
                           pickup_no=resp.get("pickup_no") or "",
                           remaining_seconds=resp.get("remaining_seconds"))
                        if cur_status == "paid":
                            final = "paid"
                            break
                        if cur_status in ("cancelled", "expired"):
                            final = cur_status
                            break
                    except Exception as e:
                        # 门户瞬断：本轮退回直连探针（mark_session 的 CAS 保证与门户探针并发安全）
                        tl("poll", via="portal", n=poll_n,
                           error=f"{type(e).__name__}: {e}"[:160], fallback="direct-probe")
                        direct_probe = True
                else:
                    if time.time() - last_portal_check >= PORTAL_RECHECK_SECONDS:
                        last_portal_check = time.time()
                        if portal_alive():
                            portal_online = True
                            tl("portal_recheck", online=True)
                if direct_probe or (not portal_online and cur_status is None):
                    sess_st, _pk = sync_remote_state(link.order_no, coupon_code,
                                                     source="test-script-poll")
                    cur_status = sess_st
                    tl("poll", via="direct", n=poll_n, session_status=sess_st)
                    if sess_st == "paid":
                        final = "paid"
                        break
                    if sess_st == "cancelled":
                        final = "cancelled"
                        break
                if time.time() - last_snap >= EVENT_SNAPSHOT_SECONDS:
                    last_snap = time.time()
                    events_snapshot = read_events(link.order_no)
                    names = [e["event"] for e in events_snapshot]
                    tl("events", count=len(names), events=names)
                    if names != last_names:
                        print(f"  [事件链] {' -> '.join(names) if names else '(暂无)'}")
                        last_names = names
                    if cur_status != last_printed_status or poll_n % 15 == 0:
                        print(f"  [{datetime.now():%H:%M:%S}] 轮询#{poll_n} "
                              f"status={cur_status}（剩余 "
                              f"{max(0, int(deadline - time.time()))}s）")
                        last_printed_status = cur_status
                time.sleep(POLL_SECONDS)
            tl("wait_end", final=final or "timeout", polls=poll_n, portal_online=portal_online)
            if final is None:
                # 超时仍未终态：最后一探；仍待支付则脚本主动取消并退券（autoCancel 等价清理）
                st = int(api.order_status(link.order_no) or 0)
                if st in (3, 6, 7):
                    final, _ = sync_remote_state(link.order_no, coupon_code, source="timeout-final")
                else:
                    print(f"  等待超时（{args.wait_minutes} 分钟）且订单仍待支付 → 执行取消+退券清理")
                    tl("timeout_cleanup", order_status=st)
                    res = cancel_order_and_rollback(link.order_no, coupon_code,
                                                    source="timeout-cancel")
                    record_step("超时-取消退券", {"order_no": link.order_no,
                                                "coupon_code": coupon_code}, res)
                    final = "cancelled"

            # ---- STEP 8: 终态断言 ----
            banner(f"[STEP 8] 终态断言（final={final}）")
            final_detail = api.order_detail(link.order_no)
            run_final_assertions(final, link.order_no, coupon_code, expected_pay, final_detail)
            events_snapshot = read_events(link.order_no)
            verdict = "pass" if all(a["pass"] for a in assertions) else "fail"

    except AbortFlow as e:
        print(f"\n[中止] {e}")
        verdict = "aborted"
    except SessionExpiredError as e:
        print(f"\n[错误] 账号凭证失效（401/12320120400401）: {e}")
        print("  修复提示: 请在管理系统对该账号重新短信登录（单会话语义，无 refresh），再重跑本脚本")
        verdict = "error"
    except ConsistencyError as e:
        print(f"\n[错误] 一致性断言失败: {e}")
        print("  修复提示: 金额/抵扣口径不符，多为门店改价或券状态变化；请重跑（残留待支付单加 --cancel-pending）")
        verdict = "error"
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        print(f"\n[错误] 网络异常: {type(e).__name__}: {e}")
        print("  修复提示: 检查外网连通；若仅在试算/下单阶段报 502/超时，门店可能打烊，请换营业时段或 --store-no")
        verdict = "error"
    except ChageeError as e:
        print(f"\n[错误] 茶姬接口失败: {e}")
        hint = ("账号凭证失效（401）：请在管理系统重新短信登录该账号后重跑"
                if str(getattr(e, "errcode", "")) in ("401", "12320120400401")
                else "网络异常/服务端拒绝：若 502 或超时多为门店打烊，请换营业时段或 --store-no 重试")
        print(f"  修复提示: {hint}")
        verdict = "error"
    except Exception as e:
        print(f"\n[错误] 未预期异常: {type(e).__name__}: {e}")
        print("  修复提示: 检查 DB/账号/门店参数；本脚本未自动支付，如已建单可加 --cancel-pending 清理")
        verdict = "error"
    finally:
        # 归档兜底（本地文件 IO；任何失败不影响已打印的结论）
        try:
            finish()
        except Exception as e:  # noqa: BLE001
            print(f"[归档失败] {type(e).__name__}: {e}")
        try:
            if db is not None:
                db.close()
        except Exception:
            pass
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
