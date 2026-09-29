"""异步订单中枢消费执行器（order-worker 线程池，2026-09-29）。

全自动执行链（复用 F5 手动下单同一套 core 实现，零重复开发）：
  claim 消息 → decide_core（选号选券，DecisionLog 留痕）→ settle_core（试算 draft）
  → create_core（auto_fallback=True 券自动切换下单）→
    零元单  → completed + 取餐码回填 + 按单回调
    差额单  → awaiting_payment + H5 收银台链接回调 → 现有 pay-watcher（2s）自动收口
              → intake_notify.on_chagee_order_paid 推进 completed + 取餐码 + 回调

失败分类（core 一律抛 HTTPException，语义与 HTTP 路由完全一致）：
  可重试（fail→退避回 pending）：账号占用 409 / 凭证失效 409 / 协议 502 / 券耗尽 400
    ——重跑 decide 自动换号换券；
  致命（kill→死信）：决策 blocked（利润/库存确定性拒绝）、createOrder 结果不确定
    （OrderHang，严禁自动重试防重复下单）、一致性校验中止。

消费幂等（at-least-once 兜底）：消息认领后先查登记单——终态或已成单（chagee_order_no
非空）直接 ack，绝不重复下单；「单次一单」检查（create_core 内）是最后防线。

线程惯例：与 order_reconcile / pay-watcher 相同的 daemon 线程 + heartbeat +
永不退出模式；worker 只跑主 API 进程（pay_portal 8000→8010 进程不启动，双进程职责约定）。
开关：CHAGEE_ORDER_WORKERS（默认 2，<=0 不启动——离线测试用，测试内直接调
process_once 做确定性验证）。
"""

import logging
import os
import threading
import time

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from models import ChageeAccount, CustomerOrder
from oplog import heartbeat, log_op, new_trace_id, reset_trace_id, set_trace_id
from schemas import OrderCreateRequest, OrderSettleRequest, DecideRequest
from services import intake_notify, order_queue
# 路由层 core 函数单向导入（orders/decision 不反向导入本模块，无环；monkeypatch 点
# 保持在 routers.orders / routers.decision 模块属性上，离线测试同款手法）
from routers.decision import decide_core
from routers.orders import create_core, settle_core

logger = logging.getLogger(__name__)

WORKER_ACTOR = "order-worker"
POLL_SECONDS = 1.0            # 空轮询间隔
RECLAIM_EVERY_ROUNDS = 30     # 每N轮兜底一次孤儿回收（≈30s）


def _classify_failure(exc: Exception) -> tuple[bool, str]:
    """(retryable, reason)：HTTPException 语义分类（与 _fail 异常链同口径）。"""
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        # createOrder 结果不确定 / 一致性中止：自动重试可能重复下单，必须人工查单
        if "结果不确定" in detail or "一致性校验失败" in detail:
            return False, detail
        # 其余（券耗尽/账号占用/凭证失效/门店打烊/协议错误/422参数…）重跑 decide
        # 换号换券可能解决 → 可重试（受 max_attempts 约束，耗尽进死信）
        return True, detail
    return True, f"{type(exc).__name__}: {exc}"


def _fail_transition(db: Session, co: CustomerOrder, exc: Exception, step: str):
    """执行异常 → 登记单状态推进 + (retry|kill, reason)。"""
    retryable, reason = _classify_failure(exc)
    if retryable:
        intake_notify.transition(db, co, "enqueued", step=step, error=reason,
                                 progress=f"{step} 执行失败，等待重试")
        return ("retry", reason)
    intake_notify.transition(db, co, "failed", step=step, error=reason,
                             progress=f"{step} 失败（需人工处理）")
    return ("kill", reason)


def _execute(db: Session, msg: dict, trace: str) -> tuple[str, str | None]:
    """单消息执行链。返回 ("done", None) | ("retry", reason) | ("kill", reason)。"""
    co = db.get(CustomerOrder, msg["customer_order_id"])
    if co is None:
        return ("done", None)   # 登记单缺失（理论不发生）：ack 防毒消息空转
    # 幂等闸门：终态或已成单不再执行（at-least-once 重复投递/孤儿重投场景）
    if co.status in ("completed", "cancelled") or co.chagee_order_no:
        return ("done", None)
    payload = co.payload or {}
    intake_notify.transition(db, co, "processing", step="decide",
                             progress="决策评估中（选号选券）",
                             trace_id=trace, attempts=msg["attempts"])

    # ① decide：自动选号选券（复用决策引擎：套餐/成本规则/方案/阈值全口径）
    decide_req = DecideRequest(
        sku_id=str(payload.get("sku_id") or ""),
        quantity=int(payload.get("quantity") or 1),
        spec_list=list(payload.get("spec_texts") or []),
        store_no=str(payload.get("store_no") or ""),
        customer_price=str(payload.get("customer_price") or ""),
        packet_id=int(payload.get("packet_id") or 0),
        allow_full_price=bool(payload.get("allow_full_price") or False),
        deep=False,
        plan_id=int(payload.get("plan_id") or 0),
    )
    try:
        resp = decide_core(db, decide_req)
    except HTTPException as e:
        return _fail_transition(db, co, e, "decide")
    except Exception as e:
        return _fail_transition(db, co, e, "decide")
    if resp.get("verdict") != "pass":
        # 决策拦截（利润不足/无券/方案超限）：确定性拒绝，不自动重试——调整配置/补券后
        # 可在死信管理手动重放
        reason = str(resp.get("blocked_reason") or "决策评估未通过")
        intake_notify.transition(db, co, "failed", step="decide", error=reason,
                                 progress="决策拦截（blocked）")
        return ("kill", reason)

    account_id = int(resp.get("account_id") or 0)
    account = db.get(ChageeAccount, account_id) if account_id else None
    if account is None or account.status != "online":
        return _fail_transition(db, co, HTTPException(
            409, f"decide 推荐账号不可用（account_id={account_id}）"), "decide")
    coupon_code = ((resp.get("coupon") or {}).get("coupon_code") or None)
    decision_log_id = int(resp.get("decision_log_id") or 0)
    prefill = resp.get("settle_prefill") or {}

    # ② settle：试算生成 draft（同进程共享 _drafts 缓存，与手动路径同机制）
    intake_notify.transition(db, co, "processing", step="settle",
                             progress="试算中", account_id=account_id)
    settle_req = OrderSettleRequest(
        store_no=str(payload.get("store_no") or ""),
        store_name=str(payload.get("store_name") or ""),
        spu_id=str(prefill.get("spu_id") or ""), spu_name=str(prefill.get("spu_name") or ""),
        sku_id=str(prefill.get("sku_id") or ""), sku_name=str(prefill.get("sku_name") or ""),
        item_sku_id=str(prefill.get("item_sku_id") or ""),
        quantity=int(prefill.get("quantity") or 1),
        spec_list=list(prefill.get("spec_list") or []),
        attribute_list=list(prefill.get("attribute_list") or []),
        extra_list=list(prefill.get("extra_list") or []),
        sale_price=float(prefill.get("sale_price") or 0),
        spu_type=str(prefill.get("spu_type") or "stand"),
        drink_info=str(payload.get("drink_info") or ""),
    )
    try:
        settle_resp = settle_core(db, account, settle_req)
    except HTTPException as e:
        return _fail_transition(db, co, e, "settle")
    except Exception as e:
        return _fail_transition(db, co, e, "settle")

    # ③ create：auto_fallback 券自动切换下单（成单边界不重试，core 内部语义）
    intake_notify.transition(db, co, "processing", step="create", progress="提交下单")
    create_req = OrderCreateRequest(
        draft_id=settle_resp["draft_id"],
        coupon_code=coupon_code,
        decision_log_id=decision_log_id,
        auto_fallback=True,
        plan_id=int(payload.get("plan_id") or 0),
    )
    try:
        result = create_core(db, account, create_req)
    except HTTPException as e:
        return _fail_transition(db, co, e, "create")
    except Exception as e:
        return _fail_transition(db, co, e, "create")

    order_no = str(result.get("order_no") or "")
    if result.get("result") == "zero":
        intake_notify.transition(db, co, "completed", step="done",
                                 progress="零元单已成，取餐码已回填",
                                 chagee_order_no=order_no,
                                 pickup_no=result.get("pickup_no") or "",
                                 pay_amount=str(result.get("pay_amount") or ""),
                                 coupon_code=result.get("coupon_code") or "")
        log_op(action="intake.order_completed", target=co.customer_order_no,
               params={"scenario": "zero", "chagee_order_no": order_no,
                       "pickup_no": result.get("pickup_no") or "",
                       "coupon": result.get("coupon_code") or None})
        intake_notify.dispatch_intake_callback(db, co, "completed")
        return ("done", None)

    # 差额单：H5 链接下发，等 pay-watcher 收口（on_chagee_order_paid 自动推进+回调）
    pay_url = str(result.get("h5_url") or "")
    intake_notify.transition(db, co, "awaiting_payment", step="awaiting_payment",
                             progress="已成差额单，等待支付（收口自动完成）",
                             chagee_order_no=order_no, pay_url=pay_url,
                             pay_amount=str(result.get("total_amount") or ""),
                             coupon_code=result.get("coupon_code") or "")
    log_op(action="intake.order_awaiting_payment", target=co.customer_order_no,
           params={"scenario": "partial", "chagee_order_no": order_no,
                   "pay_amount": str(result.get("total_amount") or ""),
                   "pay_url": pay_url[:120]})
    intake_notify.dispatch_intake_callback(db, co, "order_created", extra={
        "pay_url": pay_url, "total_amount": str(result.get("total_amount") or ""),
        "expire_note": "支付窗口10分钟，超时茶姬侧自动取消"})
    return ("done", None)


def process_once(worker_id: str = "order-worker-0") -> str:
    """认领并处理一条消息（测试可确定性直接调用）。
    返回 idle / done / retry / dead / crash。"""
    msg = order_queue.claim_one(worker_id)
    if msg is None:
        return "idle"
    trace = new_trace_id()
    token = set_trace_id(trace)   # worker 侧独立 trace：链路在 op_log 可整链查询
    try:
        try:
            with SessionLocal() as db:
                action, reason = _execute(db, msg, trace)
        except Exception as e:   # 执行器意外（DB等）：按可重试失败处理，不让毒消息杀线程
            logger.exception("worker 执行器异常 msg#%s", msg["id"])
            action, reason = "retry", f"worker-crash: {type(e).__name__}: {e}"
        if action == "done":
            order_queue.ack(msg["id"])
            return "done"
        if action == "retry":
            order_queue.fail(msg["id"], reason, msg["attempts"], msg["max_attempts"])
            return "retry"
        order_queue.kill(msg["id"], reason or "fatal")
        return "dead"
    finally:
        reset_trace_id(token)


# ---------------- 后台 worker 线程池（app.py startup 挂载，仅主 API 进程） ----------------

_started = False


def _worker_loop(worker_id: str) -> None:
    rounds = 0
    while True:
        time.sleep(POLL_SECONDS)
        heartbeat(worker_id)   # 线程存活心跳（log_monitor heartbeat_stale 据此判卡死）
        try:
            outcome = process_once(worker_id)
            if outcome in ("retry", "dead"):
                logger.warning("worker %s 处理结果：%s", worker_id, outcome)
        except Exception as e:
            logger.exception("worker 线程轮次异常，继续下一轮")
            log_op("system.thread_error", level="ERROR", params={"thread": worker_id}, error=e)
        rounds += 1
        if rounds % RECLAIM_EVERY_ROUNDS == 0:
            try:
                order_queue.reclaim_orphans()   # 崩溃恢复兜底
            except Exception:
                logger.warning("孤儿回收轮次失败（下轮重试）", exc_info=True)


def start_order_workers() -> None:
    """幂等启动 worker 线程池（name=order-worker-N）。数量读环境变量
    CHAGEE_ORDER_WORKERS（默认 2；<=0 不启动，供离线测试彻底关闭）。"""
    global _started
    if _started:
        return
    try:
        workers = int(os.environ.get("CHAGEE_ORDER_WORKERS", "2"))
    except ValueError:
        workers = 2
    if workers <= 0:
        logger.info("订单 worker 池未启用（CHAGEE_ORDER_WORKERS<=0）")
        return
    _started = True
    for i in range(workers):
        threading.Thread(target=_worker_loop, args=(f"order-worker-{i}",),
                         daemon=True, name=f"order-worker-{i}").start()
    logger.info("订单 worker 池已启动（%d 条线程，1s 轮询）", workers)
