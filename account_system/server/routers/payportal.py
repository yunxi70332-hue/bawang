"""H5 收银台公开路由（/pay/*）——token 即凭证，无 JWT。

为什么公开：收银台链接要能在任意手机浏览器直接打开（聊天转发/扫码），无法携带系统
JWT；pay_token（secrets.token_urlsafe(32)，43 字符随机）本身就是足够长的 bearer 凭证，
且一单一链接、随支付窗过期。页面 pay_cashier.html 由前端子代理提供，本路由只负责：
  GET  /pay/{token}                  收银台页面（templates/pay_cashier.html）
  GET  /pay/{token}/info             订单/金额/券抵扣/支付串 展示数据（记 page_opened 事件）
  GET  /pay/{token}/status           支付状态轮询（2s 节流远程探针，跃迁联动回写/回滚）
  POST /pay/{token}/remint           续付重铸支付串（token 不变）
  POST /pay/{token}/switch-full-price 券差额单 → 原价单（取消旧单原价重下）

异常兜底：协议层错误统一 502 带中文说明；SessionExpiredError 把账号置 expired 并记
session_expired 事件（与主 API 行为一致，无请求上下文所以走事件流而非审计日志）。
"""

import logging
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db
from models import (PAY_SESSION_STATUS_LABELS, ChageeAccount, CouponRecord, OrderRecord, PayAttempt,
                    PaySession)
from oplog import log_op
from security import client_ip
from services import chagee_bridge as bridge
from services.pay_session import (
    EVENT_ORDER_CANCELLED, EVENT_PAGE_OPENED, EVENT_PAID_DETECTED, EVENT_PICKUP_FETCHED,
    EVENT_PROBE, EVENT_ROLLED_BACK, EVENT_SESSION_EXPIRED, ORDER_STATUS_LABELS, PaySessionError,
    build_h5_url, deduction_amount, ensure_pay_session, get_by_token, is_active, mark_session,
    record_event, remaining_seconds, switch_order_to_full_price, token_prefix,
    upsert_order_record,
)
from services.order_reconcile import rollback_coupon_usage

logger = logging.getLogger(__name__)

pay_router = APIRouter(prefix="/pay", tags=["pay-portal"])

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
    attempt = (db.query(PayAttempt).filter(PayAttempt.pay_session_id == sess.id)
                 .order_by(PayAttempt.id.desc()).first())
    # expire_at 优先取最近一次铸造的支付宝原文（String），缺失回退本地截止时间
    expire_at = (attempt.expire_at if attempt and attempt.expire_at else
                 (sess.pay_deadline.strftime("%Y-%m-%d %H:%M:%S") if sess.pay_deadline else ""))
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
