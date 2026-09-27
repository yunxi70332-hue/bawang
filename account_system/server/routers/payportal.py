"""H5 收银台公开路由（/pay/*）——token 即凭证，无 JWT。

为什么公开：收银台链接要能在任意手机浏览器直接打开（聊天转发/扫码），无法携带系统
JWT；pay_token（secrets.token_urlsafe(32)，43 字符随机）本身就是足够长的 bearer 凭证，
且一单一链接、随支付窗过期。页面 pay_cashier.html 由前端子代理提供，本路由只负责：
  GET  /pay/{token}                  收银台页面（templates/pay_cashier.html）
  GET  /pay/{token}/info             订单/金额/券抵扣/支付串 展示数据（记 page_opened 事件）
  GET  /pay/{token}/status           支付状态轮询（2s 节流远程探针，跃迁联动回写/回滚）
  GET  /pay/{token}/events           SSE 状态流（sync 心跳 + paid/cancelled/expired 终态即闭流）
  POST /pay/{token}/remint           续付重铸支付串（token 不变）
  POST /pay/{token}/switch-full-price 券差额单 → 原价单（取消旧单原价重下）

另有进程内通知端点（仅收银台进程挂载，见 internal_router）：
  POST /internal/broadcast           主 API → 收银台的会话状态变更通知（X-Internal-Token 鉴权）

异常兜底：协议层错误统一 502 带中文说明；SessionExpiredError 把账号置 expired 并记
session_expired 事件（与主 API 行为一致，无请求上下文所以走事件流而非审计日志）。
"""

import asyncio
import json
import logging
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import SessionLocal, get_db
from models import (PAY_SESSION_STATUS_LABELS, ChageeAccount, CouponRecord, OrderRecord, PaySession)
from oplog import log_op
from security import client_ip
from services import chagee_bridge as bridge
from services import pay_broadcast
from services.pay_session import (
    EVENT_ORDER_CANCELLED, EVENT_PAGE_OPENED, EVENT_PAID_DETECTED, EVENT_PICKUP_FETCHED,
    EVENT_PROBE, EVENT_ROLLED_BACK, EVENT_SESSION_EXPIRED, ORDER_STATUS_LABELS, PaySessionError,
    build_h5_url, deduction_amount, ensure_pay_session, get_by_order_no, get_by_token, is_active,
    mark_session, pay_deadline_ts, record_event, remaining_seconds, server_now_ms,
    switch_order_to_full_price, token_prefix, upsert_order_record,
)
from services.order_reconcile import rollback_coupon_usage

logger = logging.getLogger(__name__)

pay_router = APIRouter(prefix="/pay", tags=["pay-portal"])
# 主 API → 收银台进程的内部通知端点（不挂在 /pay 前缀下：不是公开收银台面，
# 仅 127.0.0.1 进程间调用 + X-Internal-Token 鉴权；由 pay_portal.py 单独挂载）
internal_router = APIRouter(prefix="/internal", tags=["pay-portal-internal"])

# 收银台页面：前端子代理将替换该文件；路由只做静态分发，模板缺失时给出可诊断的 503
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "pay_cashier.html"

# 远程探针节流（进程内内存态）：{order_no: last_probe_ts}。
# 为什么不落库：节流只是防止 H5 页面高频轮询打死茶姬接口，属进程本地关注点；
# 即使多进程（8000+8010）各自探针，远端 getOrderStatus 也是幂等只读
_last_probe: dict[str, float] = {}
PROBE_MIN_INTERVAL = 2.0


def _session_or_404(db: Session, token: str) -> PaySession:
    sess = get_by_token(db, token)
    if not sess:
        raise HTTPException(404, "支付链接不存在")
    return sess


def _account_or_404(db: Session, sess: PaySession) -> ChageeAccount:
    account = db.get(ChageeAccount, sess.account_id)
    if not account:
        raise HTTPException(404, "支付链接对应的账号不存在")
    return account


def _mark_account_expired(db: Session, account: ChageeAccount, sess: PaySession, error) -> None:
    """凭证失效：账号置 expired + session_expired 事件（公开路由无审计用户上下文）。"""
    try:
        account.status = "expired"
        db.commit()
    except Exception as e:
        db.rollback()
        log_op("account.mark_expired", level="WARN", actor="pay-portal",
               target=sess.order_no, result="failed", error=e)
    record_event(db, sess.order_no, token_prefix(sess.pay_token), EVENT_SESSION_EXPIRED,
                 {"error": str(error)[:200]})


def _bump_fail(db: Session, sess: PaySession) -> None:
    """探针/协议失败计数 +1（失败也要能落库；落库失败仅告警）。"""
    try:
        sess.fail_count = (sess.fail_count or 0) + 1
        db.commit()
    except Exception as e:
        db.rollback()
        log_op("pay.probe_fail_bump", level="WARN", actor="pay-portal",
               target=sess.order_no, result="failed", error=e)


# ---------------- 页面 ----------------

@pay_router.get("/{token}")
def pay_cashier(token: str):
    """收银台页面（FileResponse 静态分发，渲染逻辑全在前端模板内）。"""
    if not TEMPLATE_PATH.is_file():
        raise HTTPException(503, "收银台页面未部署（templates/pay_cashier.html 缺失）")
    return FileResponse(TEMPLATE_PATH)


# ---------------- 展示数据 / 状态轮询 ----------------

def _info_payload(db: Session, sess: PaySession) -> dict:
    """info/remint 共用的展示数据组装（读侧：OrderRecord 补门店/商品描述，券档案补名称）。"""
    rec = db.query(OrderRecord).filter(OrderRecord.order_no == sess.order_no).first()
    coupon = None
    if sess.coupon_code:
        coupon = db.query(CouponRecord).filter(CouponRecord.coupon_code == sess.coupon_code).first()
    # expire_at 统一取**钳制后**的 pay_deadline（与 remaining_seconds/pay_deadline_ts 同源）；
    # 此前取 PayAttempt 的支付宝 time_expire 原文（下单+30min），比官方 10min autoCancel 窗
    # 长出的 20 分钟里会「页面倒计时未走完、茶姬侧已取消」，与倒计时自相矛盾
    # （2026-09-28 时间戳同步改造定案，契约 docs/pay_timesync_design_20260928.md）
    expire_at = (sess.pay_deadline.strftime("%Y-%m-%d %H:%M:%S")
                 if sess.pay_deadline else None)
    return {
        "order_no": sess.order_no,
        "store_name": (rec.store_name if rec else "") or "",
        "goods_desc": (rec.goods_desc if rec else "") or "",
        "total_amount": sess.total_amount,
        "pay_amount": sess.pay_amount,
        "coupon_code": sess.coupon_code or "",
        "coupon_name": (coupon.template_name if coupon else None) or "",
        "deduction": deduction_amount(sess),
        "mode": sess.mode,
        "status": sess.status,
        "status_label": PAY_SESSION_STATUS_LABELS.get(sess.status, sess.status),
        "pay_no": sess.pay_no,
        "out_trade_no": sess.out_trade_no,
        "expire_at": expire_at,
        "remaining_seconds": remaining_seconds(sess),
        # 时间戳同步契约：server_time 做时钟偏移校正，pay_deadline_ts 做绝对时间倒计时锚点
        "server_time": server_now_ms(),
        "pay_deadline_ts": pay_deadline_ts(sess),
        "order_str": sess.order_str,
        # 套壳跳转目标：官方收银台 URL（可空——凭证缺失/构造失败时页面自动降级）
        "alipay_cashier_url": sess.alipay_cashier_url or "",
        "portal_url": build_h5_url(sess.pay_token),
        # 只有券差额单且仍在待支付态才允许切换原价重下（无券单/已终态无意义）
        "can_switch_full_price": sess.mode == "partial" and sess.status == "issued",
    }


@pay_router.get("/{token}/info")
def pay_info(token: str, request: Request, db: Session = Depends(get_db)):
    sess = _session_or_404(db, token)
    record_event(db, sess.order_no, token_prefix(sess.pay_token), EVENT_PAGE_OPENED, {
        "ua": (request.headers.get("user-agent") or "")[:128],   # UA 摘要（截断 128）
        "ip": client_ip(request),
    })
    return _info_payload(db, sess)


@pay_router.get("/{token}/status")
def pay_status(token: str, db: Session = Depends(get_db)):
    sess = _session_or_404(db, token)
    if is_active(sess) and time.time() - _last_probe.get(sess.order_no, 0.0) >= PROBE_MIN_INTERVAL:
        _last_probe[sess.order_no] = time.time()
        account = db.get(ChageeAccount, sess.account_id)
        if account:
            _probe_remote(db, sess, account)
            db.refresh(sess)
    return {
        "status": sess.status,
        "status_label": PAY_SESSION_STATUS_LABELS.get(sess.status, sess.status),
        "pay_amount": sess.pay_amount,
        "pickup_no": sess.pickup_no or "",
        "remaining_seconds": remaining_seconds(sess),
        # 时间戳同步契约（与 /info 同源）：每次轮询都刷新权威时钟锚点
        "server_time": server_now_ms(),
        "pay_deadline_ts": pay_deadline_ts(sess),
        "paid_at": sess.paid_at,
    }


def _probe_remote(db: Session, sess: PaySession, account: ChageeAccount) -> None:
    """远程状态探针 + 跃迁联动（内部吞掉全部异常——探针失败时 /status 返回本地态）。

    跃迁规则（与后台 watcher 同口径）：
      3/6 已支付 → CAS 置 paid + order_detail 取 pickupNo 回写会话与 OrderRecord
      7   已取消 → CAS 置 cancelled + OrderRecord 同步 + 有券回滚券使用（rolled_back）
      1/未知     → 不动（下一轮再探）
    """
    prefix = token_prefix(sess.pay_token)
    try:
        api = bridge.trade_api(account)
        st = int(api.order_status(sess.order_no) or 0)
    except bridge.SessionExpiredError as e:
        _mark_account_expired(db, account, sess, e)
        _bump_fail(db, sess)
        return
    except Exception as e:
        _bump_fail(db, sess)
        record_event(db, sess.order_no, prefix, EVENT_PROBE,
                     {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
        return
    try:
        sess.fail_count = 0
        db.commit()
    except Exception:
        db.rollback()

    if st in (3, 6):
        # 已支付：CAS 防并发重复触发（watcher 与本探针同时发现时只有一方成功；
        # paid_detected 只由 CAS 成功方记录——事件计数与收口动作严格一对一，与 watcher 同口径）
        if mark_session(db, sess.order_no, "paid", paid_at=datetime.now()):
            record_event(db, sess.order_no, prefix, EVENT_PAID_DETECTED, {"order_status": st})
        db.refresh(sess)
        detail: dict = {}
        if not sess.pickup_no:
            try:
                detail = bridge.trade_api(account).order_detail(sess.order_no)
                if detail.get("pickupNo"):
                    sess.pickup_no = str(detail["pickupNo"])
                    db.commit()
            except Exception as e:
                record_event(db, sess.order_no, prefix, EVENT_PROBE,
                             {"ok": False, "stage": "order_detail",
                              "error": f"{type(e).__name__}: {e}"[:200]})
        if detail:
            # 详情字段回填 OrderRecord（低耦合 upsert：不 import 路由模块私有函数）
            upsert_order_record(db, sess.account_id, sess.order_no, status=st,
                                status_label=ORDER_STATUS_LABELS.get(st, str(st)),
                                pickup_no=sess.pickup_no or None,
                                pay_amount=str(detail.get("payAmount") or "") or None,
                                total_amount=str(detail.get("totalAmount") or "") or None)
        record_event(db, sess.order_no, prefix, EVENT_PICKUP_FETCHED,
                     {"pickup_no": sess.pickup_no or "", "order_status": st})
    elif st == 7:
        if mark_session(db, sess.order_no, "cancelled"):
            record_event(db, sess.order_no, prefix, EVENT_ORDER_CANCELLED,
                         {"source": "probe", "order_status": 7})
            upsert_order_record(db, sess.account_id, sess.order_no, status=7,
                                status_label=ORDER_STATUS_LABELS[7])
            if sess.coupon_code:
                try:
                    rollback_coupon_usage(db, sess.coupon_code, sess.order_no,
                                          operator="pay-portal")
                    record_event(db, sess.order_no, prefix, EVENT_ROLLED_BACK,
                                 {"coupon_code": sess.coupon_code})
                except Exception as e:
                    db.rollback()
                    logger.warning("订单 %s 取消后券回滚失败（下轮 watcher 会重试）",
                                   sess.order_no, exc_info=True)
                    log_op("coupon.rollback", level="WARN", actor="pay-portal",
                           target=sess.order_no, result="failed", error=e,
                           params={"coupon_code": sess.coupon_code})


# ---------------- 动作：续付重铸 / 原价切换 ----------------

@pay_router.post("/{token}/remint")
def pay_remint(token: str, db: Session = Depends(get_db)):
    """续付重铸：调 continuePay 换新支付串（token 不变，旧链接继续有效）。"""
    sess = _session_or_404(db, token)
    if sess.status != "issued" or not is_active(sess):
        raise HTTPException(409, "当前支付会话不可续付（已支付/已取消/已过期）")
    account = _account_or_404(db, sess)
    try:
        link = bridge.trade_api(account).continue_pay(sess.order_no)
    except bridge.SessionExpiredError as e:
        _mark_account_expired(db, account, sess, e)
        raise HTTPException(409, f"账号凭证已失效，请管理员重新登录后重试: {e}")
    except Exception as e:
        _bump_fail(db, sess)
        raise HTTPException(502, f"续付重铸失败，请稍后重试: {type(e).__name__}: {e}")
    # ensure 原地更新 + 追加 PayAttempt + remint 事件
    ensure_pay_session(db, sess.account_id, sess.order_no, link,
                       mode=sess.mode, coupon_code=sess.coupon_code)
    db.refresh(sess)
    log_op("pay.portal_remint", actor="pay-portal", target=sess.order_no)
    return _info_payload(db, sess)


@pay_router.post("/{token}/switch-full-price")
def pay_switch_full_price(token: str, db: Session = Depends(get_db)):
    """券差额单 → 原价单（公开版）：放弃用券、取消旧单、按原价重下并返回新支付链接。"""
    sess = _session_or_404(db, token)
    if sess.mode != "partial" or sess.status != "issued":
        raise HTTPException(409, "当前支付会话不可切换原价（仅待支付中的券差额单可切换）")
    rec = db.query(OrderRecord).filter(OrderRecord.order_no == sess.order_no).first()
    if not rec:
        raise HTTPException(400, "订单记录不存在，无法原价重下")
    if int(rec.status or 0) != 1:
        raise HTTPException(409, f"订单当前状态不允许切换（status={rec.status}）")
    if rec.scenario != "partial" or not rec.coupon_code:
        raise HTTPException(400, "该订单非券差额单")
    if not (rec.order_target or {}).get("target"):
        raise HTTPException(400, "历史订单缺少商品快照，无法原价重下")
    account = _account_or_404(db, sess)
    try:
        result = switch_order_to_full_price(db, account, rec, operator="pay-portal")
    except bridge.SessionExpiredError as e:
        _mark_account_expired(db, account, sess, e)
        raise HTTPException(409, f"账号凭证已失效，请管理员重新登录后重试: {e}")
    except PaySessionError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # 502 必须留痕：2026-09-27 实弹中 switch 半途失败（旧单已取消、新单未建）无任何日志可查
        logger.warning("原价重下失败 order_no=%s: %s: %s", sess.order_no, type(e).__name__, e,
                       exc_info=True)
        record_event(db, sess.order_no, token_prefix(sess.pay_token), "switch_full_price",
                     {"ok": False, "stage": "recreate", "error": f"{type(e).__name__}: {e}"[:200]})
        log_op("order.switch_full_price", level="WARN", actor="pay-portal",
               target=sess.order_no, result="failed", error=e)
        raise HTTPException(502, f"原价重下失败: {type(e).__name__}: {e}")
    log_op("pay.portal_switch_full_price", actor="pay-portal", target=sess.order_no,
           params={"new_order_no": result.get("new_order_no")
                   if isinstance(result, dict) else None})
    return result


# ---------------- SSE 实时状态流（/pay/{token}/events） ----------------
# 事件协议（契约 docs/pay_timesync_design_20260928.md，壳页按此实现）：
#   连接建立即推 event: sync（data 含权威时钟锚点，前端据此做偏移校正）；
#   状态跃迁推对应事件：paid / cancelled / expired（data 结构同 sync），推完关闭流；
#   issued 态下每 5s 推 sync 兼作心跳（兼探活，代理/浏览器闲置断链可被及时发现）。
# 跨进程联动：主 API（8000）侧 mark_session 收口后经 pay_broadcast POST /internal/broadcast
# 通知本进程 _publish；通知丢失（进程重启窗口/网络抖动）由 1s 兜底轮询比对
# (status, pay_deadline_ts) 变化补推——两层保证，通知只是加速器不是关键路径。

# 进程内连接注册表：order_no → 该单所有订阅队列（多端同开一单各自独立收流）
_sse_subs: dict[str, set[asyncio.Queue]] = {}
_sse_lock = threading.Lock()
# 上次已推送的会话指纹（order_no → (status, pay_deadline_ts)）：兜底轮询的变化比对基准
_sse_last_seen: dict[str, tuple] = {}
_sse_loop: asyncio.AbstractEventLoop | None = None   # 主事件循环（线程侧投递经它落回 loop）
_sse_reconciler: asyncio.Task | None = None          # 兜底轮询 task（无订阅时退出，标志复位）

_SSE_TERMINAL = ("paid", "cancelled", "expired")    # 终态：推完对应事件即关闭流
_SSE_HEARTBEAT = 5.0                                 # issued 态 sync 心跳间隔（秒）
_SSE_POLL = 1.0                                      # 兜底轮询间隔（秒）


def _sse_data(sess: PaySession) -> dict:
    """SSE 事件 data 载荷（sync 与终态事件同构——前端一个解析器通吃）。"""
    return {"server_time": server_now_ms(),
            "pay_deadline_ts": pay_deadline_ts(sess),
            "status": sess.status,
            "remaining_seconds": remaining_seconds(sess)}


def _sse_frame(event: str, data: dict) -> str:
    """data → SSE 帧（event: xxx + 单行 JSON data，标准 text/event-stream 格式）。"""
    return (f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n")


def _sse_snapshot(order_no: str) -> dict | None:
    """独立短事务查会话当前态 → 事件载荷（会话不存在返回 None；不借用请求级 db——
    广播路由与兜底轮询都在请求生命周期/事件循环之外，必须自管会话）。"""
    with SessionLocal() as db:
        sess = get_by_order_no(db, order_no)
        return _sse_data(sess) if sess else None


def _sse_query_batch(order_nos: list[str]) -> list[tuple[str, dict | None]]:
    """兜底轮询的批量查询（to_thread 里跑的同步函数，独立短事务一次查完）。"""
    out: list[tuple[str, dict | None]] = []
    with SessionLocal() as db:
        for order_no in order_nos:
            sess = get_by_order_no(db, order_no)
            out.append((order_no, _sse_data(sess) if sess else None))
    return out


def _publish(order_no: str, event: str, data: dict, *, close: bool = False) -> None:
    """向该单全部订阅队列投递事件。

    线程边界：/internal/broadcast 是同步路由（线程池线程），队列属主是主事件循环——
    必须经 loop.call_soon_threadsafe 落回 loop 线程再 put（loop 内调用同样安全）。
    同时刷新 _sse_last_seen（兜底轮询据此去重，避免广播成功后 1s 内重复推）。
    """
    loop = _sse_loop
    if loop is None:
        return
    item = {"event": event, "data": data, "close": close}
    with _sse_lock:
        queues = list(_sse_subs.get(order_no) or ())
        _sse_last_seen[order_no] = (data.get("status"), data.get("pay_deadline_ts"))
    for q in queues:
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except Exception:
            # 队列所属 loop 已关（进程停机窗口）：丢弃即可，订阅侧会随断流自清理
            logger.debug("SSE 队列投递失败（忽略）order_no=%s event=%s", order_no, event)


async def _sse_reconciler_loop() -> None:
    """兜底轮询：每 1s 查订阅单会话状态，与 _sse_last_seen 比对，变化即推对应事件。

    为什么必须有：notify_session_change 是 fire-and-forget（丢通知/主 API 重启窗口/
    requests 未装），轮询比对是最终一致的保底；查询走 asyncio.to_thread 不阻塞事件循环
    （同步 SQLAlchemy 会话绝不能在 loop 线程里跑长查询）。无订阅时退出并复位标志，
    下个 /events 连接重新拉起（空闲零开销）。"""
    global _sse_reconciler
    try:
        while True:
            await asyncio.sleep(_SSE_POLL)
            with _sse_lock:
                order_nos = list(_sse_subs.keys())
                if not order_nos:
                    break
            try:
                rows = await asyncio.to_thread(_sse_query_batch, order_nos)
            except Exception:
                logger.debug("SSE 兜底轮询查询失败（下轮再试）", exc_info=True)
                continue
            for order_no, data in rows:
                if data is None:
                    continue   # 会话已删（测试清扫等）：不推事件，等订阅自然断开
                with _sse_lock:
                    if _sse_last_seen.get(order_no) == (data.get("status"),
                                                       data.get("pay_deadline_ts")):
                        continue
                status = data.get("status")
                if status in _SSE_TERMINAL:
                    _publish(order_no, status, data, close=True)
                else:
                    _publish(order_no, "sync", data)
    finally:
        with _sse_lock:
            for order_no in list(_sse_subs):
                if not _sse_subs.get(order_no):
                    _sse_subs.pop(order_no, None)
                    _sse_last_seen.pop(order_no, None)
        _sse_reconciler = None


def _ensure_sse_runtime() -> None:
    """SSE 运行时装配（首个 /events 请求时调用，须在事件循环线程内）：
    记录主 loop 引用（_publish 线程侧投递用）并拉起兜底轮询 task（幂等）。"""
    global _sse_loop, _sse_reconciler
    _sse_loop = asyncio.get_running_loop()
    if _sse_reconciler is None or _sse_reconciler.done():
        _sse_reconciler = asyncio.create_task(_sse_reconciler_loop())


@pay_router.get("/{token}/events")
async def pay_events(token: str, db: Session = Depends(get_db)):
    """SSE 状态流：连接即推 sync（时钟锚点），状态跃迁推终态事件后关闭流。

    async 路由（本文件唯一的 async 端点）：SSE 长连接必须挂在事件循环上， StreamingResponse
    的 generator 与订阅队列同 loop；db 仅用于入口的 token 校验与首包载荷，流期间查询
    全部走兜底轮询的独立短事务——绝不把请求级会话拖进分钟级长连接。"""
    sess = get_by_token(db, token)
    if not sess:
        raise HTTPException(404, "支付链接不存在")
    order_no = sess.order_no
    initial = _sse_data(sess)   # 首包在响应开流前取好，generator 不再触碰请求级 db/ORM 对象
    terminal_at_connect = sess.status in _SSE_TERMINAL
    _ensure_sse_runtime()
    queue: asyncio.Queue = asyncio.Queue()
    with _sse_lock:
        _sse_subs.setdefault(order_no, set()).add(queue)
        _sse_last_seen.setdefault(order_no, (initial.get("status"),
                                             initial.get("pay_deadline_ts")))

    async def stream():
        try:
            yield _sse_frame("sync", initial)
            if terminal_at_connect:
                # 连上即终态（支付完成/已取消后才打开页面）：补推终态事件后直接闭流
                yield _sse_frame(initial["status"], initial)
                return
            while True:
                # 空闲 _SSE_HEARTBEAT 秒即推一帧 sync 兼作心跳（wait_for 超时不消费队列，
                # 真事件总是优先送达；心跳兼作代理/浏览器的探活帧防静默断链）
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT)
                except asyncio.TimeoutError:
                    data = await asyncio.to_thread(_sse_snapshot, order_no)
                    if data is None:
                        return   # 会话已被清扫：闭流让前端走 /info 兜底
                    yield _sse_frame("sync", data)
                    with _sse_lock:
                        _sse_last_seen[order_no] = (data.get("status"),
                                                    data.get("pay_deadline_ts"))
                    continue
                yield _sse_frame(item["event"], item["data"])
                if item.get("close"):
                    return
        except asyncio.CancelledError:
            raise   # 客户端断开：向上抛出走 finally 清理注册
        finally:
            with _sse_lock:
                queues = _sse_subs.get(order_no)
                if queues is not None:
                    queues.discard(queue)
                    if not queues:
                        _sse_subs.pop(order_no, None)
                        _sse_last_seen.pop(order_no, None)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class _BroadcastRequest(BaseModel):
    """主 API → 收银台通知体（pay_broadcast.notify_session_change 的对端契约）。"""
    order_no: str
    status: str


@internal_router.post("/broadcast")
def internal_broadcast(body: _BroadcastRequest, request: Request):
    """主 API → 收银台进程的会话状态变更通知（mark_session 收口后自动调用）。

    鉴权：X-Internal-Token 与 data/internal_broadcast.secret 比对（双进程同机同文件）。
    注意以**库里最新状态**为准分派事件（而非请求体声称的 status）：通知是尽力而为的
    加速信号，库态才是唯一事实源——迟到的旧通知不会把终态倒拨回 issued。
    同步 def 路由（线程池执行）：_publish 内部经 loop.call_soon_threadsafe 落回主循环。"""
    presented = (request.headers.get("x-internal-token") or "").strip()
    expected = pay_broadcast.load_internal_secret()
    if not expected or not secrets.compare_digest(presented.encode("utf-8"),
                                                  expected.encode("utf-8")):
        raise HTTPException(401, "internal token 校验失败")
    data = _sse_snapshot(body.order_no)
    if data is None:
        raise HTTPException(404, "支付会话不存在")
    status = data.get("status") or "issued"
    event = status if status in _SSE_TERMINAL else "sync"
    _publish(body.order_no, event, data, close=event != "sync")
    return {"ok": True, "order_no": body.order_no, "status": status, "event": event}
