"""异步订单中枢：登记单状态机推进 + SSE 推送 + 按单回调（2026-09-29）。

职责切分：order_worker（消费执行）/ payment_events（支付收口）两个线程域都会推进
CustomerOrder 状态，统一收口在本模块——状态机 CAS 校验 + 全量字段回填 + events_bus
实时推送（topic "intake"）+ 按单 HMAC 签名回调，一处实现三处复用。

回调签名与 payment_events.dispatch_payment_callback 同一方案：
X-CHAGEE-Signature = hex(hmac_sha256(secret, raw_body))，secret 派生自
data/pay_callback_config.json 的 secret（缺省 data/secret.key），接收端按原始 body
字节验签。投递在独立 daemon 线程（1/2/4s 退避共 4 次），绝不阻塞 watcher/worker 主循环。

与 payment_events 的依赖方向：payment_events 在挂钩点函数内**惰性导入**本模块
（本模块不反向导入 payment_events），保持无环。
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime

from sqlalchemy.orm import Session

from models import CustomerOrder
from oplog import log_op
from services import events_bus

logger = logging.getLogger(__name__)

# services/x.py 上三层 = account_system/（与 database.DATA_DIR 同级定位）
_ACCOUNT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CALLBACK_CONFIG_PATH = os.path.join(_ACCOUNT_ROOT, "data", "pay_callback_config.json")
_SECRET_KEY_PATH = os.path.join(_ACCOUNT_ROOT, "data", "secret.key")

# 状态机合法迁移表（终态仅 failed/cancelled 可经人工重放回 enqueued；completed 不可逆）
_ALLOWED_TRANSITIONS = {
    "registered": {"enqueued", "processing", "cancelled"},
    "enqueued": {"processing", "cancelled", "failed"},
    "processing": {"awaiting_payment", "completed", "failed", "enqueued"},
    "awaiting_payment": {"completed", "failed"},
    "completed": set(),
    "failed": {"enqueued"},
    "cancelled": {"enqueued"},
}
_TERMINAL = ("completed", "failed", "cancelled")

# 回调投递退避（秒）：独立线程内执行，初始 + 3 次重试
_CALLBACK_RETRY_BACKOFF = (1.0, 2.0, 4.0)
# 直连 opener：绕过系统代理（回调目标多为内网/局域网地址，Windows 代理会劫持）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def transition(db: Session, co: CustomerOrder, new_status: str, *, step: str = "",
               progress: str | None = None, error: str | None = None,
               **fields) -> bool:
    """登记单状态推进（CAS 语义）：非法迁移拒绝并 WARN 留痕（返回 False），幂等重入
    （同状态）只补字段不重复推进终态时刻。field_updates 空值不覆盖已有值（同
    OrderRecord upsert 口径）。每次成功推进都向 events_bus "intake" topic 推一帧。"""
    current = co.status
    if new_status != current and new_status not in _ALLOWED_TRANSITIONS.get(current, set()):
        log_op(level="WARN", action="intake.transition_denied", target=co.customer_order_no,
               params={"from": current, "to": new_status})
        return False
    was_terminal = current in _TERMINAL
    co.status = new_status
    if step:
        co.step = step[:32]
    if progress is not None:
        co.progress = str(progress)[:255]
    if error is not None:
        co.error = str(error)[:2000]
    for k, v in fields.items():
        if v not in (None, ""):
            setattr(co, k, v)
    if new_status in _TERMINAL and not was_terminal:
        co.finished_at = datetime.now()
    db.commit()
    publish_status(co)
    return True


def publish_status(co: CustomerOrder) -> None:
    """登记单状态帧 → SSE（topic "intake"，事件名 order_status）；无订阅者时为 no-op。"""
    try:
        events_bus.publish("intake", "order_status", {
            "customer_order_no": co.customer_order_no,
            "status": co.status,
            "step": co.step or "",
            "progress": co.progress or "",
            "chagee_order_no": co.chagee_order_no or "",
            "pickup_no": co.pickup_no or "",
            "pay_url": co.pay_url or "",
            "error": (co.error or "")[:200],
            "updated_at": str(co.updated_at or ""),
        })
    except Exception:
        logger.warning("intake SSE 推送失败（忽略）", exc_info=True)


def find_by_chagee_order(db: Session, order_no: str) -> CustomerOrder | None:
    return (db.query(CustomerOrder)
              .filter(CustomerOrder.chagee_order_no == str(order_no or "").strip())
              .first())


# ---------------- pay-watcher 收口挂钩（payment_events 惰性调用） ----------------

def on_chagee_order_paid(db: Session, order_no: str, pickup_no: str) -> None:
    """茶姬单支付收口（watcher / H5 探针任一方 CAS 赢家触发）：登记单推进 completed、
    取餐码缓存回填、按单回调。幂等：已完成单只补取餐码（迟到码补发 pickup 事件）。"""
    co = find_by_chagee_order(db, order_no)
    if co is None:
        return
    moved = transition(db, co, "completed", step="paid",
                       progress="支付完成，订单收口", pickup_no=pickup_no)
    if moved:
        log_op(action="intake.order_completed", target=co.customer_order_no,
               params={"chagee_order_no": order_no, "pickup_no": pickup_no or ""})
        dispatch_intake_callback(db, co, "completed")
    elif pickup_no and not co.pickup_no:
        # 已 completed 但取餐码迟到（detail 探针失败后补回）：补缓存 + 单独回调
        co.pickup_no = pickup_no
        db.commit()
        dispatch_intake_callback(db, co, "pickup")


def on_chagee_order_cancelled(db: Session, order_no: str) -> None:
    """茶姬单取消/超时收口：登记单 failed（原因=支付取消/超时，券已由收口方回滚）。"""
    co = find_by_chagee_order(db, order_no)
    if co is None:
        return
    moved = transition(db, co, "failed", step="payment_cancelled",
                       progress="支付未完成，茶姬侧订单已取消",
                       error="支付取消/超时：茶姬侧订单已取消，用券已回滚")
    if moved:
        log_op(level="WARN", action="intake.order_failed", target=co.customer_order_no,
               params={"chagee_order_no": order_no, "reason": "payment_cancelled"})
        dispatch_intake_callback(db, co, "cancelled")


# ---------------- 按单回调（HMAC 签名，独立线程投递） ----------------

def _callback_secret() -> bytes:
    """与 payment_events._callback_secret 同源：config.secret 优先，缺省 data/secret.key。"""
    try:
        with open(_CALLBACK_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        secret = str((cfg or {}).get("secret") or "").strip()
        if secret:
            return secret.encode("utf-8")
    except Exception:
        pass
    try:
        with open(_SECRET_KEY_PATH, encoding="utf-8") as f:
            return f.read().strip().encode("utf-8")
    except Exception:
        return b""


def _post_signed(url: str, raw: bytes, customer_order_no: str) -> bool:
    secret = _callback_secret()
    headers = {"Content-Type": "application/json",
               "X-CHAGEE-Signature": hmac.new(secret, raw, hashlib.sha256).hexdigest()}
    for attempt in range(len(_CALLBACK_RETRY_BACKOFF) + 1):
        if attempt:
            time.sleep(_CALLBACK_RETRY_BACKOFF[min(attempt - 1, len(_CALLBACK_RETRY_BACKOFF) - 1)])
        try:
            req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
            with _OPENER.open(req, timeout=5) as resp:
                int(resp.getcode() or 0)
            return True
        except Exception as e:
            logger.warning("intake 回调投递失败 %s attempt=%s（%s）",
                           customer_order_no, attempt + 1, e)
    log_op(level="WARN", action="intake.callback_failed", target=customer_order_no,
           result="failed", params={"url": url[:200]})
    return False


def dispatch_intake_callback(db: Session, co: CustomerOrder, event: str,
                             extra: dict | None = None) -> None:
    """按单回调（co.callback_url 为空 = no-op）。事件：order_created（差额单链接下发）/
    completed（支付收口或零元单完成）/ pickup（迟到取餐码）/ cancelled（支付取消）。
    fire-and-forget：独立 daemon 线程投递，失败重试 3 次后仅留痕。"""
    url = str(co.callback_url or "").strip()
    if not url.startswith("http"):
        return
    body = {
        "event": event,
        "customer_order_no": co.customer_order_no,
        "chagee_order_no": co.chagee_order_no or "",
        "status": co.status,
        "pickup_no": co.pickup_no or "",
        "pay_url": co.pay_url or "",
        "pay_amount": co.pay_amount or "",
        "dispatched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        body.update(extra)
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    db.rollback()   # 确保无挂起事务泄漏给后台线程（回调线程只用网络，不再碰本会话）
    threading.Thread(target=_post_signed, args=(url, raw, co.customer_order_no),
                     daemon=True, name=f"intake-cb-{co.customer_order_no[:24]}").start()
