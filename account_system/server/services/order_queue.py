"""SQLite 持久化消息队列（2026-09-29 异步订单中枢，docs/intake_system.md）。

为什么自建而不引 broker：单机部署（主 API 8000 单进程），现役栈即 SQLite + WAL +
daemon 线程；队列行落 order_messages 表后，「订单登记 + 入队」可在同一事务原子提交
（外部 broker 做不到），重试退避 / 死信 / 宕机恢复全在应用层可控，零外部服务依赖。

可靠性语义（at-least-once + 消费幂等）：
  - 认领原子性：单条 UPDATE…RETURNING（SQLite 单写者天然无 check-then-act 竞态），
    attempts 在认领时 +1——进程崩溃在认领后、执行中，计数不丢，毒消息终会耗尽进死信；
  - 崩溃恢复：processing 超 visibility timeout 的行由 reclaim_orphans 复位 pending
    （worker 池每轮兜底调用，消息必被重投）；
  - 退避重试：fail 按 (30s, 120s, 300s) 阶梯回 pending（next_visible_at 延迟可见），
    attempts ≥ max_attempts 置 dead（死信，管理端点重放）；
  - 消费幂等由调用方（order_worker）以 CustomerOrder 状态机 CAS 保证：重复投递时
    看到终态/已成单直接 ack，绝不重复下单。

线程模型：enqueue 走调用方事务（接收层请求线程）；claim/ack/fail/reclaim 自开短会话
（worker 线程 + 回收线程），绝不与远程调用混在同一事务。
"""

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal
from models import OrderMessage
from oplog import log_op

logger = logging.getLogger(__name__)

# 重试退避阶梯（秒）：第 1/2/3+ 次失败后的延迟可见时间
RETRY_BACKOFF_SECONDS = (30, 120, 300)
DEFAULT_MAX_ATTEMPTS = 3

# 认领后超过该时长未 ack/fail 视为 worker 崩溃，消息复位 pending 重投
VISIBILITY_TIMEOUT_SECONDS = float(os.environ.get("CHAGEE_ORDER_MSG_VISIBILITY_TIMEOUT", "600") or 600)

TOPIC_ORDER_CREATE = "order.create"


def enqueue(db: Session, customer_order_id: int, payload: dict | None = None,
            topic: str = TOPIC_ORDER_CREATE, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> OrderMessage:
    """入队（走调用方事务：与 CustomerOrder 登记同一 commit，原子生效）。"""
    msg = OrderMessage(topic=topic, customer_order_id=customer_order_id,
                       payload=payload or {}, max_attempts=max_attempts)
    db.add(msg)
    return msg   # commit 由调用方统一执行


def claim_one(worker_id: str) -> dict | None:
    """原子认领一条可见消息：单条 UPDATE…RETURNING，status pending→processing、
    attempts+1、locked_by/locked_at 落worker 标识。无可认领返回 None。
    多 worker 并发调用由 SQLite 单写者串行化，不重不漏。"""
    now = datetime.now()
    stmt = text(
        "UPDATE order_messages SET status='processing', locked_by=:wid, locked_at=:now, "
        "       attempts = attempts + 1 "
        "WHERE id = (SELECT id FROM order_messages "
        "            WHERE status='pending' AND next_visible_at <= :vis "
        "            ORDER BY id LIMIT 1) "
        "RETURNING id, topic, customer_order_id, payload, attempts, max_attempts"
    )
    with SessionLocal() as db:
        row = db.execute(stmt, {"wid": worker_id[:64], "now": now,
                                "vis": now}).mappings().first()
        if row is None:
            return None
        db.commit()
        return dict(row)


def ack(msg_id: int) -> None:
    """确认完成：置 done（幂等：非 processing 态时静默不动，防重放误伤）。"""
    with SessionLocal() as db:
        db.execute(text("UPDATE order_messages SET status='done', done_at=:now, "
                        "locked_by='' WHERE id=:mid AND status='processing'"),
                   {"now": datetime.now(), "mid": msg_id})
        db.commit()


def fail(msg_id: int, error: str, attempts: int, max_attempts: int) -> str:
    """消费失败：attempts < max_attempts 按阶梯退避回 pending；否则置 dead（死信）。
    返回 "pending" | "dead"。"""
    err = str(error or "")[:2000]
    now = datetime.now()
    if attempts >= max_attempts:
        with SessionLocal() as db:
            db.execute(text("UPDATE order_messages SET status='dead', last_error=:err, "
                            "locked_by='', done_at=:now WHERE id=:mid AND status='processing'"),
                       {"err": err, "now": now, "mid": msg_id})
            db.commit()
        log_op(level="ERROR", action="queue.dead", target=f"msg#{msg_id}", error=err,
               params={"attempts": attempts, "max_attempts": max_attempts})
        return "dead"
    delay = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
    visible_at = now + timedelta(seconds=delay)
    with SessionLocal() as db:
        db.execute(text("UPDATE order_messages SET status='pending', last_error=:err, "
                        "next_visible_at=:vis, locked_by='', locked_at=NULL "
                        "WHERE id=:mid AND status='processing'"),
                   {"err": err, "vis": visible_at, "mid": msg_id})
        db.commit()
    log_op(level="WARN", action="queue.retry", target=f"msg#{msg_id}", error=err,
           params={"attempts": attempts, "max_attempts": max_attempts,
                   "retry_in_seconds": delay})
    return "pending"


def reclaim_orphans(timeout_seconds: float | None = None) -> int:
    """孤儿回收（崩溃恢复）：processing 超 visibility timeout 的行复位 pending。
    next_visible_at 也推到 now——复位即立刻可见重投；attempts 已在认领时计过数，
    反复崩溃的毒消息仍会自然耗尽进死信。返回回收条数。"""
    timeout = VISIBILITY_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    cutoff = datetime.now() - timedelta(seconds=timeout)
    now = datetime.now()
    with SessionLocal() as db:
        result = db.execute(
            text("UPDATE order_messages SET status='pending', next_visible_at=:now, "
                 "locked_by='', locked_at=NULL, "
                 "last_error=CASE WHEN last_error='' THEN :err ELSE last_error END "
                 "WHERE status='processing' AND locked_at < :cutoff"),
            {"now": now, "cutoff": cutoff,
             "err": "reclaimed: worker crash or timeout"}).rowcount
        db.commit()
    if result:
        log_op(level="WARN", action="queue.orphan_reclaim", target="order_messages",
               params={"reclaimed": result, "timeout_seconds": timeout})
    return result or 0


def kill(msg_id: int, error: str) -> None:
    """致命失败直达死信（不消耗退避重试）：决策 blocked / createOrder 结果不确定等
    确定性拒绝——重试不可能成功，直接进死信等人工处置（重放端点可恢复）。"""
    with SessionLocal() as db:
        db.execute(text("UPDATE order_messages SET status='dead', last_error=:err, "
                        "locked_by='', done_at=:now WHERE id=:mid AND status='processing'"),
                   {"err": str(error or "")[:2000], "now": datetime.now(), "mid": msg_id})
        db.commit()
    log_op(level="ERROR", action="queue.dead", target=f"msg#{msg_id}",
           error=error, params={"reason": "fatal"})


def requeue_dead(msg_id: int) -> bool:
    """死信重放：dead → pending，attempts/last_error 归零（重新给满重试额度）。"""
    with SessionLocal() as db:
        row = db.execute(text("UPDATE order_messages SET status='pending', attempts=0, "
                              "last_error='', next_visible_at=:now, done_at=NULL "
                              "WHERE id=:mid AND status='dead' "
                              "RETURNING customer_order_id"),
                         {"now": datetime.now(), "mid": msg_id}).mappings().first()
        db.commit()
        return row is not None


def queue_stats() -> dict:
    """队列深度/健康统计（监控端点 + log_monitor 规则数据源；fail-soft 永不抛）。"""
    try:
        with SessionLocal() as db:
            rows = db.execute(text(
                "SELECT status, COUNT(*) AS n, MIN(created_at) AS oldest_created, "
                "       MIN(next_visible_at) AS oldest_visible "
                "FROM order_messages GROUP BY status")).mappings().all()
        counts = {r["status"]: int(r["n"]) for r in rows}
        oldest_pending = next((r["oldest_created"] for r in rows
                               if r["status"] == "pending"), None)
        oldest_visible = next((r["oldest_visible"] for r in rows
                               if r["status"] == "pending"), None)
        return {
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0),
            "done": counts.get("done", 0),
            "dead": counts.get("dead", 0),
            "oldest_pending_created_at": str(oldest_pending or ""),
            "oldest_pending_visible_at": str(oldest_visible or ""),
        }
    except Exception as e:
        logger.warning("队列统计查询失败（降级空值）: %s", e)
        return {"pending": 0, "processing": 0, "done": 0, "dead": 0,
                "oldest_pending_created_at": "", "oldest_pending_visible_at": "",
                "error": str(e)[:200]}
