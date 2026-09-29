"""异步订单中枢：登记核心（接收层共用，2026-09-29）。

内部标准接口（routers/intake.py JWT）与外部 KFC 系适配接口（X-Api-Key）共用
register_order 一个登记入口：归一化 payload → 幂等检查（customer_order_no 唯一）
→ CustomerOrder + OrderMessage **同一事务**原子落库（这是选 SQLite 队列的核心收益，
外部 broker 做不到）→ SSE 推送。接收层只做毫秒级本地写 + 菜单库预检（必要时回源），
重活全部留给 worker。

预检（fail-fast，坏报文不进队列）：skuId 经菜单规格库 resolve_by_sku 命中
（新店/新品自动全店回源一次）；spec_texts 文案经 resolve_spec_texts 三级匹配
（精确/别名 → 模糊 → 歧义拒猜 422 带候选清单）——「半糖」类文案歧义绝不静默下单。
"""

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import CustomerOrder, IntakeApiKey
from oplog import log_op
from schemas import IntakeExternalOrderRequest, IntakeOrderRequest
from services import intake_notify, order_queue
from services.menu_spec import SpecResolveError, resolve_by_sku, resolve_spec_texts

logger = logging.getLogger(__name__)

# services/x.py 上三层 = account_system/（与 database.DATA_DIR 同级定位）
_ACCOUNT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INTAKE_CONFIG_PATH = os.path.join(_ACCOUNT_ROOT, "data", "intake_config.json")

RATE_LIMIT_PER_MINUTE = int(os.environ.get("CHAGEE_INTAKE_RATE_LIMIT", "120") or 120)
_KEY_LAST_USED_THROTTLE = 60.0   # last_used_at 节流写间隔（秒），高并发下不逐请求落库

# 每密钥滑窗限流（进程内存态；单进程部署约定，无需分布式）
_rate_windows: dict[int, deque] = {}
_rate_lock = threading.Lock()


def load_intake_config() -> dict:
    """data/intake_config.json（缺文件/解析失败回退空配置）：
    {"default_store_no": "CN00529", "allow_full_price": false}。"""
    try:
        with open(INTAKE_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


# ---------------- 归一化与预检 ----------------

def validate_and_normalize(db: Session, body: IntakeOrderRequest) -> dict:
    """内部标准报文 → 归一化 payload（worker 执行链的输入契约）+ 菜单库预检。
    预检失败抛 422（带候选清单），坏报文不进队列。"""
    try:
        hit = resolve_by_sku(db, body.store_no, body.sku_id)
        if body.spec_texts:
            resolve_spec_texts(db, body.store_no, hit["spu_id"], body.spec_texts)
    except SpecResolveError as e:
        detail = {"message": str(e)}
        if getattr(e, "candidates", None):
            detail["candidates"] = e.candidates
        raise HTTPException(422, detail)
    return {
        "store_no": body.store_no, "store_name": body.store_name,
        "sku_id": body.sku_id, "quantity": body.quantity,
        "spec_texts": list(body.spec_texts),
        "customer_price": body.customer_price,
        "allow_full_price": bool(body.allow_full_price),
        "plan_id": body.plan_id, "packet_id": body.packet_id,
        "drink_info": body.drink_info, "phone": body.phone, "remark": body.remark,
        "sku_resolved": {"spu_id": hit.get("spu_id"), "spu_name": hit.get("spu_name"),
                         "price": hit.get("price")},   # 预检快照（审计/展示用，执行以 decide 重解析为准）
    }


def external_to_internal(body: IntakeExternalOrderRequest) -> IntakeOrderRequest:
    """KFC 系外部报文 → 内部标准报文（字段映射见 IntakeExternalOrderRequest 文档；
    storeNo 缺省回落 intake_config.default_store_no，仍缺 → 422）。"""
    cfg = load_intake_config()
    store_no = str(body.storeNo or "").strip() or str(cfg.get("default_store_no") or "").strip()
    if not store_no:
        raise HTTPException(422, "storeNo 缺失且系统未配置默认门店"
                                 "（account_system/data/intake_config.json 的 default_store_no）")
    return IntakeOrderRequest(
        customer_order_no=body.orderNo,
        store_no=store_no,
        sku_id=body.linkId,
        quantity=body.count,
        spec_texts=list(body.specs or []),
        customer_price=body.payAmount,
        allow_full_price=bool(cfg.get("allow_full_price", False)),
        phone=body.phone or "",
        remark=body.remark or "",
        callback_url=body.callbackUrl or "",
    )


# ---------------- 幂等登记 + 同事务入队 ----------------

def register_order(db: Session, *, source: str, api_key_id: int,
                   payload: dict, raw_payload: dict,
                   callback_url: str) -> tuple[CustomerOrder, bool]:
    """登记 + 入队（同一事务，调用方 commit 或本函数内 commit）。
    幂等：customer_order_no 已存在 → 返回 (既有单, False)，不重复入队。
    新单：registered + 消息 pending → commit 后一次性推进 enqueued（SSE 两帧可合一，
    这里直接落 enqueued 状态再 commit，registered 仅为模型缺省值）。"""
    existing = (db.query(CustomerOrder)
                  .filter(CustomerOrder.customer_order_no == payload.get("customer_order_no"))
                  .first())
    if existing is not None:
        return existing, False
    co = CustomerOrder(
        customer_order_no=payload["customer_order_no"],
        source=source[:32], api_key_id=api_key_id,
        payload=payload, raw_payload=raw_payload,
        callback_url=(callback_url or "")[:512],
        status="enqueued", step="queued", progress="已入队，等待消费",
        trace_id="",
    )
    db.add(co)
    db.flush()                      # 拿 co.id 供消息关联
    order_queue.enqueue(db, co.id, payload={"customer_order_no": co.customer_order_no})
    db.commit()
    log_op(action="intake.order_received", target=co.customer_order_no,
           params={"source": source, "sku_id": payload.get("sku_id"),
                   "store_no": payload.get("store_no"),
                   "customer_price": payload.get("customer_price"),
                   "quantity": payload.get("quantity")})
    intake_notify.publish_status(co)
    return co, True


# ---------------- 接入密钥 ----------------

def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def create_api_key(db: Session, label: str, source: str) -> tuple[IntakeApiKey, str]:
    """创建密钥：明文仅本次返回，库内只存 sha256。"""
    plaintext = f"ck-{secrets.token_urlsafe(32)}"
    row = IntakeApiKey(key_hash=hash_key(plaintext), label=label[:64],
                       source=(source or "external")[:32])
    db.add(row)
    db.commit()
    return row, plaintext


def check_rate_limit(api_key_id: int) -> None:
    """每密钥滑窗限流（默认 120 次/分钟，CHAGEE_INTAKE_RATE_LIMIT 可调）：
    超限 429。进程内存态——单机部署下与 DB 口径等价。"""
    now = time.monotonic()
    with _rate_lock:
        window = _rate_windows.setdefault(api_key_id, deque())
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= RATE_LIMIT_PER_MINUTE:
            raise HTTPException(429, f"请求频率超限（每分钟 {RATE_LIMIT_PER_MINUTE} 单），请稍后重试")
        window.append(now)


def touch_key(db: Session, key: IntakeApiKey) -> None:
    """last_used_at 节流回写（>60s 才写一次），高并发接收路径不做多余写放大。"""
    now = datetime.now()
    if key.last_used_at is None or (now - key.last_used_at).total_seconds() > _KEY_LAST_USED_THROTTLE:
        try:
            key.last_used_at = now
            db.commit()
        except Exception:
            db.rollback()   # 审计字段写失败不影响接收主流程
