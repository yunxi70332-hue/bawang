"""官方收银台支付参数串：生成 / 存储 / 读取（独立域，一单一活跃记录）。

支付参数串 = 官方收银台 URL（mclient.alipay.com/cashierRoutePay.htm?...）的结构化
JSON 快照（v1 契约，字段集稳定，支付宝浏览器支付 Python 脚本 json.loads 即可消费）：

  {
    "v": 1,                          # 契约版本（解析端据此校验，不识别即拒收）
    "order_no": "2026...",           # 业务关联键（order_records / pay_sessions 同键）
    "pay_no": "CHP...", "out_trade_no": "...",
    "pay_amount": "16.00",           # 实付差额（支付宝侧金额）
    "total_amount": "19.00",         # 订单总额
    "cashier_url": "https://mclient.alipay.com/cashierRoutePay.htm?...",  # 原始完整 URL
    "base_url": "https://mclient.alipay.com/cashierRoutePay.htm",
    "params": {                      # URL query 全量参数（URL 解码后；键集开放，必需要素见下）
      "route_pay_from": "h5", "init_from": "SDKLite",
      "session": "...", "utdid": "...", "tid": "...", "cc": "y" },
    "source": "protocol-mint",       # protocol-mint|frida-mint|manual|static-config
    "generated_at": "2026-09-28 18:05:17",
    "expires_at": "2026-09-28 18:10:11"   # 支付截止（钳制后 pay_deadline；短窗链接过期重铸）
  }

必需参数 session/utdid/tid（mobilegw 设备级三元组，2026-09-27 定案）缺失即生成失败
——宁缺毋滥：半截参数对支付脚本毫无价值，load 端同样强校验。

写入时机（与 alipay_cashier_url 同步，三处）：
  ensure_pay_session 静态构造 / cashier_mint._fill_back 自动铸造回填 /
  POST /orders/{order_no}/cashier-url 人工回填。
读取出口：pay_link_payload_with_session 下单/续付响应、GET .../cashier 轮询、
GET .../pay-params 显式端点、scripts/read_pay_params.py（Python 脚本消费）。
"""

import json
import logging
import urllib.parse
from datetime import datetime

from sqlalchemy.orm import Session

from models import PayParamRecord, PaySession

logger = logging.getLogger(__name__)

# 契约常量（scripts/read_pay_params.py 保持同口径——脚本零依赖独立运行，两处勿漂移）
PARAM_STR_VERSION = 1
CASHIER_HOST = "mclient.alipay.com"
CASHIER_PATH = "/cashierRoutePay.htm"
REQUIRED_PARAMS = ("session", "utdid", "tid")   # mobilegw 设备级三元组，缺一不可

# 参数串来源枚举（PayParamRecord.source 取值域）
SOURCES = ("protocol-mint", "frida-mint", "manual", "static-config")


def parse_cashier_url(url: str) -> dict | None:
    """官方收银台 URL → {"base_url", "params"}（query 全量、URL 解码、键序保持）。

    形态不符（非 mclient.alipay.com/cashierRoutePay.htm）或缺必需三元组返回 None；
    本函数是「URL 是否携带完整支付参数」的唯一判官，人工回填端点之外的写入都经它把关。
    """
    url = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc != CASHIER_HOST or parts.path != CASHIER_PATH:
            return None
        # 收银台 URL 无重复键：parse_qs 的 list 值展平取首值（keep_blank_values 保留 cc=y 之外的空值键）
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        params = {k: v for k, v in pairs}
        if any(not params.get(k) for k in REQUIRED_PARAMS):
            return None
        return {"base_url": f"https://{CASHIER_HOST}{CASHIER_PATH}", "params": params}
    except Exception:
        return None


def build_param_str(*, order_no: str, cashier_url: str, pay_no: str = "",
                    out_trade_no: str = "", pay_amount: str = "", total_amount: str = "",
                    source: str = "", expires_at: str = "") -> str | None:
    """组装支付参数串（紧凑 JSON 单行，ensure_ascii=False；与库内存储格式一致）。

    参数不完整（parse_cashier_url 拒收）返回 None；调用方据此跳过落库（fail-soft）。
    """
    parsed = parse_cashier_url(cashier_url)
    if not parsed:
        return None
    doc = {
        "v": PARAM_STR_VERSION,
        "order_no": str(order_no or ""),
        "pay_no": str(pay_no or ""),
        "out_trade_no": str(out_trade_no or ""),
        "pay_amount": str(pay_amount or ""),
        "total_amount": str(total_amount or ""),
        "cashier_url": str(cashier_url or "").strip(),
        "base_url": parsed["base_url"],
        "params": parsed["params"],
        "source": str(source or ""),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": str(expires_at or ""),
    }
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def save_pay_params(db: Session, order_no: str, cashier_url: str, source: str = "",
                    sess: PaySession | None = None) -> PayParamRecord | None:
    """生成并 upsert 支付参数串（order_no 唯一一单一活跃；覆盖式更新）。

    会话上下文（流水/金额/截止）缺省从 PaySession 补齐——参数串自带完整支付语境，
    消费端无需再 join 业务表。URL 参数不完整返回 None 不落库（URL 本身由调用方负责）。
    短事务：本函数独立 commit，绝不与远程调用混在同一事务。
    """
    order_no = str(order_no or "").strip()
    url = str(cashier_url or "").strip()
    if not order_no or not url:
        return None
    if sess is None:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).first()
    s = sess
    param_str = build_param_str(
        order_no=order_no, cashier_url=url,
        pay_no=(s.pay_no or "") if s else "",
        out_trade_no=(s.out_trade_no or "") if s else "",
        pay_amount=(s.pay_amount or "") if s else "",
        total_amount=(s.total_amount or "") if s else "",
        source=source,
        expires_at=(s.pay_deadline.strftime("%Y-%m-%d %H:%M:%S")
                    if s and s.pay_deadline else ""))
    if not param_str:
        return None
    rec = db.query(PayParamRecord).filter(PayParamRecord.order_no == order_no).first()
    if not rec:
        rec = PayParamRecord(order_no=order_no)
        db.add(rec)
    if s:
        rec.account_id = s.account_id or 0
        # 与 pay_session.token_prefix 同口径（不 import 之，避免 pay_session ↔ pay_params 循环依赖）
        rec.pay_token_prefix = str(s.pay_token or "")[:8]
    rec.param_str = param_str
    rec.source = str(source or "")[:32]
    db.commit()
    return rec


def get_pay_param_str(db: Session, order_no: str) -> str:
    """读原文（无记录返回空串；parse/校验交给 load_pay_param_str——存取与解析分离，
    坏数据不静默吞，也不在读路径抛断调用方）。"""
    rec = db.query(PayParamRecord).filter(
        PayParamRecord.order_no == str(order_no or "").strip()).first()
    return rec.param_str if rec else ""


def load_pay_param_str(param_str: str) -> dict:
    """支付参数串 → dict（Python 支付脚本的消费入口；scripts/read_pay_params.py 同实现）。

    显式抛 ValueError（版本不符/缺必需参数/非 JSON）：支付脚本拿到半截参数比拿不到
    更危险，这里不做静默降级。
    """
    doc = json.loads(param_str)   # JSONDecodeError 是 ValueError 子类
    if not isinstance(doc, dict) or doc.get("v") != PARAM_STR_VERSION:
        raise ValueError(f"支付参数串版本不符：期望 v={PARAM_STR_VERSION}，"
                         f"实际 v={doc.get('v') if isinstance(doc, dict) else type(doc).__name__}")
    params = doc.get("params")
    if not isinstance(params, dict):
        raise ValueError("支付参数串缺少 params 对象")
    missing = [k for k in REQUIRED_PARAMS if not params.get(k)]
    if missing:
        raise ValueError("支付参数串缺少必需参数：" + "/".join(missing))
    return doc


def payload_fields(db: Session, order_no: str) -> dict:
    """API 响应字段组装：pay_params（解析后 dict，脚本直接取用）+ pay_param_str（原文，
    前端展示/复制与库内存储逐字一致）。无记录 → {None, ""}；坏数据同样兜底不抛。
    """
    s = get_pay_param_str(db, order_no)
    if not s:
        return {"pay_params": None, "pay_param_str": ""}
    try:
        return {"pay_params": load_pay_param_str(s), "pay_param_str": s}
    except ValueError:
        logger.warning("支付参数串解析失败（原文透出）order_no=%s", order_no, exc_info=True)
        return {"pay_params": None, "pay_param_str": s}
