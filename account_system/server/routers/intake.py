"""异步订单中枢接收层（2026-09-29，契约 docs/intake_system.md）。

两套鉴权、同一登记核心（services/intake_registry.register_order）：
  内部标准  POST /api/intake/orders        （JWT feature:order——工作台/内部系统）
  外部适配  POST /api/intake/v1/orders     （X-Api-Key——KFC 系客户平台，适配层换字段名）

高并发原理：接收路径只做「Pydantic 校验 + 菜单库预检（本地，必要时回源）+ 毫秒级
SQLite 短事务（登记+入队原子提交）」即返 202，重活全在 worker——吞吐瓶颈在 SQLite
单写者（WAL + busy_timeout=5000 已就位），压测口径见 test_intake_stress.py。

管理端点（intake:manage）：登记单列表/详情/取消/重放、队列统计、死信列表/重放、
接入密钥管理。状态实时性：SSE topic "intake"（GET /api/events?topics=intake）逐状态
迁移推送，前端订单中枢页订阅即得。
"""

import os
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import (INTAKE_STATUS, INTAKE_STATUS_LABELS, CustomerOrder, IntakeApiKey,
                    OrderMessage, SystemUser)
from oplog import log_op
from schemas import IntakeExternalOrderRequest, IntakeKeyRequest, IntakeOrderRequest
from security import require_perm
from services import intake_registry, order_queue
from services.intake_notify import publish_status, transition

router = APIRouter(prefix="/api/intake", tags=["intake"])           # 内部（JWT）
v1_router = APIRouter(prefix="/api/intake/v1", tags=["intake"])     # 外部（X-Api-Key）


def _co_row(co: CustomerOrder) -> dict:
    return {
        "customer_order_no": co.customer_order_no,
        "source": co.source, "status": co.status,
        "status_label": INTAKE_STATUS_LABELS.get(co.status, co.status),
        "step": co.step or "", "progress": co.progress or "",
        "sku_id": (co.payload or {}).get("sku_id", ""),
        "store_no": (co.payload or {}).get("store_no", ""),
        "quantity": (co.payload or {}).get("quantity", 1),
        "customer_price": (co.payload or {}).get("customer_price", ""),
        "goods_snapshot": ((co.payload or {}).get("sku_resolved") or {}),
        "account_id": co.account_id, "chagee_order_no": co.chagee_order_no,
        "pickup_no": co.pickup_no, "pay_url": co.pay_url, "pay_amount": co.pay_amount,
        "coupon_code": co.coupon_code, "attempts": co.attempts,
        "error": co.error or "", "trace_id": co.trace_id or "",
        "callback_url": co.callback_url or "",
        "created_at": co.created_at, "updated_at": co.updated_at,
        "started_at": co.started_at, "finished_at": co.finished_at,
    }


# ---------------- 内部标准接口（JWT feature:order） ----------------

@router.post("/orders", status_code=202)
def intake_submit(body: IntakeOrderRequest, request: Request,
                  db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("feature:order"))):
    """登记客户订单（内部标准格式）：幂等（customer_order_no 重复返回 200 现状）、
    预检 422（sku/规格文案/金额）、成功 202 已入队。"""
    payload = intake_registry.validate_and_normalize(db, body)
    payload["customer_order_no"] = body.customer_order_no
    co, created = intake_registry.register_order(
        db, source="internal", api_key_id=0, payload=payload,
        raw_payload=body.model_dump(), callback_url=body.callback_url)
    if not created:
        return {"duplicate": True, **_co_row(co),
                "note": "该客户单号已登记（幂等命中），未重复入队"}
    log_audit(db, request, user, "intake.submit", co.customer_order_no,
              {"source": "internal", "sku_id": body.sku_id, "store_no": body.store_no,
               "customer_price": body.customer_price})
    return {"duplicate": False, **_co_row(co), "query": f"/api/intake/orders/{co.customer_order_no}"}


@router.get("/orders")
def intake_list(
    keyword: str = Query("", max_length=64),
    status: str = Query("", pattern="^(registered|enqueued|processing|awaiting_payment|completed|failed|cancelled)?$"),
    source: str = Query("", max_length=32),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: SystemUser = Depends(require_perm("feature:order")),
):
    q = db.query(CustomerOrder)
    if status:
        q = q.filter(CustomerOrder.status == status)
    if source:
        q = q.filter(CustomerOrder.source.like(f"%{source}%"))
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(CustomerOrder.customer_order_no.like(like)
                     | CustomerOrder.chagee_order_no.like(like)
                     | CustomerOrder.pickup_no.like(like)
                     | CustomerOrder.source.like(like))
    total = q.count()
    rows = (q.order_by(CustomerOrder.id.desc())
              .offset((page - 1) * page_size).limit(page_size).all())
    by_status: dict[str, int] = {}
    for st, n in db.query(CustomerOrder.status, func.count(CustomerOrder.id)) \
            .group_by(CustomerOrder.status).all():
        by_status[st] = n
    return {"total": total,
            "stats": {"by_status": by_status},
            "items": [_co_row(r) for r in rows]}


@router.get("/orders/{customer_order_no}")
def intake_detail(customer_order_no: str, db: Session = Depends(get_db),
                  _: SystemUser = Depends(require_perm("feature:order"))):
    co = db.query(CustomerOrder).filter(
        CustomerOrder.customer_order_no == customer_order_no).first()
    if not co:
        raise HTTPException(404, "登记单不存在")
    return _co_row(co)


@router.post("/orders/{customer_order_no}/cancel")
def intake_cancel(customer_order_no: str, request: Request,
                  db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("intake:manage"))):
    """消费前取消（registered/enqueued 可取消；processing 及之后 409——执行中/已成单
    的取消走茶姬订单管理，不能在此撤回）。同时清掉尚未消费的 pending 消息。"""
    co = db.query(CustomerOrder).filter(
        CustomerOrder.customer_order_no == customer_order_no).first()
    if not co:
        raise HTTPException(404, "登记单不存在")
    if co.status not in ("registered", "enqueued"):
        raise HTTPException(409, f"当前状态 {co.status} 不可取消（仅入队前可取消；"
                                 f"已成单请到订单管理处理茶姬侧订单）")
    moved = transition(db, co, "cancelled", step="cancelled",
                       progress="人工取消（消费前）")
    # 未消费消息一并作废：pending → dead（防 worker 随后拾起执行）
    db.execute(text("UPDATE order_messages SET status='dead', last_error='cancelled by user', "
                    "done_at=:now WHERE customer_order_id=:cid AND status IN ('pending','processing')"),
               {"now": datetime.now(), "cid": co.id})
    db.commit()
    log_audit(db, request, user, "intake.cancel", co.customer_order_no, {"moved": moved})
    log_op(action="intake.order_cancelled", actor=user.username, target=co.customer_order_no)
    return _co_row(co)


@router.post("/orders/{customer_order_no}/requeue")
def intake_requeue(customer_order_no: str, request: Request,
                   db: Session = Depends(get_db),
                   user: SystemUser = Depends(require_perm("intake:manage"))):
    """失败/取消单重新入队（发全新消息，重试额度重置）：决策 blocked 调整配置后、
    支付取消后想重新下单的场景。completed 不可重放（茶姬单已成，重下=重复下单）。"""
    co = db.query(CustomerOrder).filter(
        CustomerOrder.customer_order_no == customer_order_no).first()
    if not co:
        raise HTTPException(404, "登记单不存在")
    if co.status not in ("failed", "cancelled"):
        raise HTTPException(409, f"当前状态 {co.status} 不可重放（仅 failed/cancelled；"
                                 f"completed 已成单严禁重下）")
    moved = transition(db, co, "enqueued", step="queued",
                       progress="人工重放入队", error="")
    if not moved:
        raise HTTPException(409, "状态迁移被拒绝")
    co.error = ""
    db.flush()
    db.add(OrderMessage(customer_order_id=co.id,
                        payload={"customer_order_no": co.customer_order_no}))
    db.commit()
    log_audit(db, request, user, "intake.requeue", co.customer_order_no, {})
    log_op(action="intake.requeue", actor=user.username, target=co.customer_order_no)
    publish_status(co)
    return _co_row(co)


# ---------------- 队列与死信管理（intake:manage） ----------------

@router.get("/queue/stats")
def intake_queue_stats(_: SystemUser = Depends(require_perm("intake:manage"))):
    stats = order_queue.queue_stats()
    workers = int(os.environ.get("CHAGEE_ORDER_WORKERS", "2") or 2)
    alive = [t.name for t in threading.enumerate() if t.name.startswith("order-worker")]
    stats["workers_configured"] = workers
    stats["workers_alive"] = alive
    return stats


@router.get("/queue/messages")
def intake_queue_messages(
    status: str = Query("dead", pattern="^(pending|processing|done|dead)$"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: SystemUser = Depends(require_perm("intake:manage")),
):
    """队列消息分页查询（默认查死信）；带出关联登记单号便于处置。"""
    q = db.query(OrderMessage).filter(OrderMessage.status == status)
    total = q.count()
    rows = (q.order_by(OrderMessage.id.desc())
              .offset((page - 1) * page_size).limit(page_size).all())
    co_ids = {r.customer_order_id for r in rows if r.customer_order_id}
    co_map = {c.id: c.customer_order_no for c in
              db.query(CustomerOrder).filter(CustomerOrder.id.in_(co_ids)).all()} if co_ids else {}
    return {"total": total, "items": [{
        "id": r.id, "topic": r.topic, "customer_order_id": r.customer_order_id,
        "customer_order_no": co_map.get(r.customer_order_id, ""),
        "status": r.status, "attempts": r.attempts, "max_attempts": r.max_attempts,
        "last_error": r.last_error, "next_visible_at": r.next_visible_at,
        "locked_by": r.locked_by, "created_at": r.created_at, "done_at": r.done_at,
    } for r in rows]}


@router.post("/queue/messages/{msg_id}/requeue")
def intake_dead_requeue(msg_id: int, request: Request,
                        db: Session = Depends(get_db),
                        user: SystemUser = Depends(require_perm("intake:manage"))):
    """死信重放：消息回 pending（额度重置）；关联登记单若处终态同步回 enqueued。"""
    msg = db.get(OrderMessage, msg_id)
    if not msg or msg.status != "dead":
        raise HTTPException(404, "死信消息不存在")
    ok = order_queue.requeue_dead(msg_id)
    if not ok:
        raise HTTPException(409, "重放失败（消息已不在死信态）")
    co = db.get(CustomerOrder, msg.customer_order_id) if msg.customer_order_id else None
    if co is not None and co.status in ("failed", "cancelled"):
        co.status = "enqueued"
        co.step = "queued"
        co.progress = "死信重放入队"
        co.error = ""
        db.commit()
        publish_status(co)
    log_audit(db, request, user, "intake.dead_requeue", f"msg#{msg_id}",
              {"customer_order_no": co.customer_order_no if co else ""})
    log_op(action="queue.dead_requeue", actor=user.username, target=f"msg#{msg_id}")
    return {"requeued": True, "msg_id": msg_id}


# ---------------- 接入密钥管理（intake:manage） ----------------

@router.get("/keys")
def intake_keys(db: Session = Depends(get_db),
                _: SystemUser = Depends(require_perm("intake:manage"))):
    rows = db.query(IntakeApiKey).order_by(IntakeApiKey.id.desc()).all()
    return {"total": len(rows), "items": [{
        "id": r.id, "label": r.label, "source": r.source, "active": r.active,
        "key_preview": r.key_hash[:12] + "…",   # 指纹展示（明文不可再现）
        "last_used_at": r.last_used_at, "created_at": r.created_at,
    } for r in rows]}


@router.post("/keys")
def intake_keys_create(body: IntakeKeyRequest, request: Request,
                       db: Session = Depends(get_db),
                       user: SystemUser = Depends(require_perm("intake:manage"))):
    row, plaintext = intake_registry.create_api_key(db, body.label, body.source)
    log_audit(db, request, user, "intake.key_create", f"key#{row.id}",
              {"label": body.label, "source": body.source})
    return {"id": row.id, "label": row.label, "source": row.source,
            "api_key": plaintext,
            "note": "明文仅此一次返回，请立即交付对接方并妥善保管"}


@router.post("/keys/{key_id}/disable")
def intake_keys_disable(key_id: int, request: Request,
                        db: Session = Depends(get_db),
                        user: SystemUser = Depends(require_perm("intake:manage"))):
    row = db.get(IntakeApiKey, key_id)
    if not row:
        raise HTTPException(404, "密钥不存在")
    row.active = False
    db.commit()
    log_audit(db, request, user, "intake.key_disable", f"key#{key_id}", {})
    return {"id": key_id, "active": False}


# ---------------- 外部适配接口（X-Api-Key，KFC 系报文） ----------------

def _require_api_key(x_api_key: str = Header("", alias="X-Api-Key"),
                     db: Session = Depends(get_db)) -> IntakeApiKey:
    if not x_api_key:
        raise HTTPException(401, "缺少 X-Api-Key 请求头")
    row = (db.query(IntakeApiKey)
             .filter(IntakeApiKey.key_hash == intake_registry.hash_key(x_api_key),
                     IntakeApiKey.active.is_(True)).first())
    if row is None:
        raise HTTPException(401, "X-Api-Key 无效或已吊销")
    intake_registry.check_rate_limit(row.id)
    intake_registry.touch_key(db, row)
    return row


@v1_router.post("/orders", status_code=202)
def intake_submit_v1(body: IntakeExternalOrderRequest,
                     key: IntakeApiKey = Depends(_require_api_key),
                     db: Session = Depends(get_db)):
    """外部平台登记（KFC 系格式）：linkId→skuId、specs 文案解析、storeNo 缺省回落
    配置默认门店；错误 422（带 message/candidates）、幂等 200、成功 202。"""
    internal = intake_registry.external_to_internal(body)
    payload = intake_registry.validate_and_normalize(db, internal)
    payload["customer_order_no"] = internal.customer_order_no
    source = f"external:{key.source or 'default'}"
    co, created = intake_registry.register_order(
        db, source=source, api_key_id=key.id, payload=payload,
        raw_payload=body.model_dump(), callback_url=internal.callback_url)
    if not created:
        return {"duplicate": True, "orderNo": co.customer_order_no,
                "status": co.status, "pickupNo": co.pickup_no or "",
                "chageeOrderNo": co.chagee_order_no or "",
                "note": "该单号已登记（幂等命中），未重复入队"}
    return {
        "duplicate": False, "orderNo": co.customer_order_no, "status": co.status,
        "query": f"/api/intake/v1/orders/{co.customer_order_no}",
        "note": "已入队，异步执行；轮询查询接口或等待回调（order_created/completed/cancelled）",
    }


@v1_router.get("/orders/{customer_order_no}")
def intake_query_v1(customer_order_no: str,
                    key: IntakeApiKey = Depends(_require_api_key),
                    db: Session = Depends(get_db)):
    """外部平台状态查询：处理进度/取餐码/支付链接（KFC 系字段命名）。"""
    co = db.query(CustomerOrder).filter(
        CustomerOrder.customer_order_no == customer_order_no,
        CustomerOrder.api_key_id == key.id).first()
    if not co:
        raise HTTPException(404, "订单不存在（注意单号归属密钥隔离）")
    return {
        "orderNo": co.customer_order_no, "status": co.status,
        "statusLabel": INTAKE_STATUS_LABELS.get(co.status, co.status),
        "step": co.step or "", "progress": co.progress or "",
        "chageeOrderNo": co.chagee_order_no or "", "pickupNo": co.pickup_no or "",
        "payUrl": co.pay_url or "", "payAmount": co.pay_amount or "",
        "error": co.error or "", "attempts": co.attempts,
        "createdAt": str(co.created_at or ""), "finishedAt": str(co.finished_at or ""),
    }
