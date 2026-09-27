"""订单超时校准服务：过期待支付单以茶姬侧真实状态为准，回收本地订单快照与券使用痕迹。

背景（F5 partial 单的本地预记）：成单时即预记券已使用（last_used_at/last_order_no，
本地缓存性质，真实核销以茶姬侧为准）。用户始终未支付时，茶姬在支付窗（10 分钟）后
autoCancel 取消订单，券实际未核销——本模块周期扫描过期待支付单，逐单轻探
getOrderStatus（data 裸 int）做校准：
  - 茶姬已取消(7) → 订单置已取消 + rollback_coupon_usage 回滚券使用痕迹
  - 茶姬已支付(3/6) → 回填订单状态，券保持已使用（真实核销）
  - 仍待支付(1)/未知状态码 → 不动数据，等下一轮
启动：app.py startup 调 start_reconcile_thread()（daemon 线程，间隔环境变量
CHAGEE_RECONCILE_INTERVAL_SECONDS，默认 60 秒）。
"""

import logging
import os
import threading
import time
import types
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from audit import log_audit
from database import SessionLocal
from models import ChageeAccount, CouponRecord, CouponUsageLog, OrderRecord
from oplog import heartbeat, log_op
from services import chagee_bridge as bridge

logger = logging.getLogger(__name__)

RECONCILE_GRACE_SECONDS = 120   # pay_deadline 过期后的宽限，避免与茶姬侧取消竞态
PAY_WINDOW_SECONDS = 600        # 支付窗（与 routers/orders.py 一致；复制常量，避免反向依赖路由层）

# 订单状态文案（与 routers/orders.py ORDER_STATUS_LABELS 同步，仅列校准涉及的态）
_STATUS_LABELS = {1: "待支付", 3: "制作中", 6: "已完成", 7: "已取消"}


def rollback_coupon_usage(db: Session, coupon_code: str, order_no: str,
                          operator: str = "system") -> bool:
    """事务内回滚一张券的使用痕迹（订单超时未支付、茶姬侧实际未核销时调用）。

    CouponRecord 复位为未使用（bucket→effective / last_used_at→None / last_order_no→""），
    并补一条 result="rolled_back" 的 CouponUsageLog（金额/账号字段取该券最近一条 success
    日志快照，无则券名取档案 template_name、金额留空）。券不存在返回 False（不算错误）；
    任何异常 rollback 后向上抛出。返回 True 表示券确实被回滚。
    """
    try:
        rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).first()
        if not rec:
            return False
        last = (db.query(CouponUsageLog)
                  .filter(CouponUsageLog.coupon_code == coupon_code,
                          CouponUsageLog.result == "success")
                  .order_by(CouponUsageLog.id.desc()).first())
        if last:
            name, account_id, account_label = last.coupon_name, last.account_id, last.account_label
            deduction, total_amount, pay_amount = (last.deduction, last.total_amount,
                                                   last.pay_amount)
        else:
            name, account_id, account_label = rec.template_name, 0, ""
            deduction = total_amount = pay_amount = ""
        rec.bucket = "effective"
        rec.last_used_at = None
        rec.last_order_no = ""
        # 幂等防护：该券该单已回滚过（崩溃恢复 / 线程与手动取消竞态）则只复位字段，不重复写日志
        dup = (db.query(CouponUsageLog)
                 .filter(CouponUsageLog.coupon_code == coupon_code,
                         CouponUsageLog.order_no == order_no,
                         CouponUsageLog.result == "rolled_back")
                 .first())
        if not dup:
            db.add(CouponUsageLog(
                coupon_code=coupon_code, coupon_name=name,
                account_id=account_id, account_label=account_label,
                operator=operator, order_no=order_no,
                deduction=deduction, total_amount=total_amount, pay_amount=pay_amount,
                result="rolled_back",
                fail_reason="订单超时未支付，券状态自动回滚为未使用",
                used_at=datetime.now(),
            ))
        db.commit()
        log_op("coupon.rollback", actor=operator, target=order_no,
               params={"coupon_code": coupon_code})
        return True
    except Exception:
        db.rollback()
        raise


def reconcile_order(db: Session, order: OrderRecord, account: ChageeAccount,
                    *, operator: str = "system") -> str:
    """校准单笔待支付订单（要求 order.status==1）：以茶姬侧 getOrderStatus 为准。

    返回 cancelled_rolled_back（茶姬已取消：订单置取消并回滚券，同一事务一次提交）/
    paid_confirmed（已支付：回填状态，券保持已使用不动）/ still_pending（茶姬仍待支付
    或未知状态码，不动数据）/ skipped_error（网络/API 异常，不动数据，不抛出）。
    """
    if int(order.status or 0) != 1:
        return "still_pending"   # 非待支付单不校准（防御直调误用；扫描入口已过滤 status==1）
    try:
        api = bridge.trade_api(account)
        st = int(api.order_status(order.order_no) or 0)   # 轻探针，data 裸 int
    except bridge.SessionExpiredError as e:
        # 凭证失效：顺手置 expired（参照 ops.py coupons_sync_all 容错写法），订单/券数据不动
        try:
            account.status = "expired"
            db.commit()
        except Exception:
            db.rollback()
        logger.info("校准订单 %s 时账号 %s 凭证失效，已置 expired: %s",
                    order.order_no, account.id, e)
        log_op("order.reconcile", level="WARN", actor=operator, target=order.order_no,
               result="skipped_error", error=e)
        return "skipped_error"
    except Exception as e:
        logger.warning("校准订单 %s 查询茶姬状态失败（%s: %s），本轮跳过",
                       order.order_no, type(e).__name__, e)
        log_op("order.reconcile", level="WARN", actor=operator, target=order.order_no,
               result="skipped_error", error=e)
        return "skipped_error"

    if st == 7:
        # 茶姬已取消（autoCancel）：订单置取消 + 券使用痕迹回滚，同一事务内提交
        try:
            order.status = 7
            order.status_label = _STATUS_LABELS[7]
            if order.coupon_code:
                # 券回滚失败（异常）时整体 rollback 并向上抛出，由调用方记 skipped
                rollback_coupon_usage(db, order.coupon_code, order.order_no, operator=operator)
            db.commit()
            log_op("order.reconcile", actor=operator, target=order.order_no,
                   result="cancelled_rolled_back",
                   params={"coupon": order.coupon_code or None})
            return "cancelled_rolled_back"
        except Exception:
            db.rollback()
            raise
    if st in (3, 6):
        # 已支付：券真实核销，本地保持已使用不动；仅回填订单状态（3=制作中, 6=已完成）
        order.status = st
        order.status_label = _STATUS_LABELS[st]
        db.commit()
        log_op("order.reconcile", actor=operator, target=order.order_no,
               result="paid_confirmed", params={"order_status": st})
        return "paid_confirmed"
    if st != 1:
        logger.warning("订单 %s 茶姬侧返回未知状态码 %s，不动数据", order.order_no, st)
        log_op("order.reconcile", level="WARN", actor=operator, target=order.order_no,
               result="unknown_status", params={"remote_status": st})
    return "still_pending"


def _expired_base(order: OrderRecord) -> datetime | None:
    """订单支付截止基线：pay_deadline 优先；缺失用 created_at + 支付窗兜底；两者皆无 → None（跳过）。"""
    if order.pay_deadline:
        return order.pay_deadline
    if order.created_at:
        return order.created_at + timedelta(seconds=PAY_WINDOW_SECONDS)
    return None


def _scan_expired_pending(db: Session, *, operator: str) -> dict:
    now = datetime.now()
    cutoff = now - timedelta(seconds=RECONCILE_GRACE_SECONDS)
    # 先 .all() 物化扫描集：逐单 commit 不会扰动遍历游标
    pendings = (db.query(OrderRecord).filter(OrderRecord.status == 1)
                  .order_by(OrderRecord.id).all())
    scanned = cancelled = confirmed = pending = skipped = 0
    details: list[dict] = []
    for order in pendings:
        base = _expired_base(order)
        if base is None or base >= cutoff:
            continue   # 未过宽限线 / 无时间基线：不在本次扫描范围
        scanned += 1
        account = db.get(ChageeAccount, order.account_id)
        if account is None:
            skipped += 1
            details.append({"order_no": order.order_no, "account_id": order.account_id,
                            "result": "skipped_no_account"})
            continue
        if account.status == "disabled" or not (account.token or ""):
            skipped += 1
            details.append({"order_no": order.order_no, "account_id": order.account_id,
                            "result": "skipped_disabled" if account.status == "disabled"
                            else "skipped_no_token"})
            continue
        try:
            result = reconcile_order(db, order, account, operator=operator)
        except Exception as e:
            # 单笔异常吞掉记 skipped（含券回滚失败的向上抛出），继续下一笔
            db.rollback()
            skipped += 1
            logger.warning("校准订单 %s 异常（%s: %s），记 skipped 继续",
                           order.order_no, type(e).__name__, e)
            log_op("order.reconcile", level="WARN", actor=operator,
                   target=order.order_no, result="skipped_error", error=e)
            details.append({"order_no": order.order_no, "account_id": order.account_id,
                            "result": "skipped_error"})
            continue
        if result == "cancelled_rolled_back":
            cancelled += 1
        elif result == "paid_confirmed":
            confirmed += 1
        elif result == "still_pending":
            pending += 1
        else:
            skipped += 1
        details.append({"order_no": order.order_no, "account_id": order.account_id,
                        "result": result})
    summary = {"scanned": scanned, "cancelled": cancelled, "confirmed": confirmed,
               "pending": pending, "skipped": skipped, "details": details}
    if scanned:
        # 汇总审计（action 命名沿用 feature.* 惯例；后台线程无请求上下文，request/user 置空、
        # username 列承载 operator）。空扫描不写，避免 60 秒轮询刷屏审计日志
        log_audit(db, None, types.SimpleNamespace(id=0, username=operator),
                  "feature.order_reconcile", "全库",
                  {k: summary[k] for k in ("scanned", "cancelled", "confirmed",
                                           "pending", "skipped")})
    return summary


def reconcile_expired_pending_orders(db: Session | None = None, *, operator: str = "system") -> dict:
    """扫描全库过期待支付单（status==1 且过宽限线）并逐单 reconcile_order。

    返回 {"scanned"/"cancelled"/"confirmed"/"pending"/"skipped": int, "details": [...]}；
    db=None 时自开会话（with 自动关闭）。单笔异常不中断整体扫描。
    """
    if db is not None:
        return _scan_expired_pending(db, operator=operator)
    with SessionLocal() as own:   # type: Session
        return _scan_expired_pending(own, operator=operator)


# ---------------- 后台校准线程（app.py startup 挂载） ----------------

_thread_started = False


def _reconcile_loop(interval: int) -> None:
    while True:
        time.sleep(interval)
        heartbeat("order-reconcile")   # 线程存活心跳（限频落库，log_monitor 据此判循环卡死）
        try:
            result = reconcile_expired_pending_orders()   # 自开会话
            if result.get("scanned"):
                logger.info("订单校准完成：scanned=%s cancelled=%s confirmed=%s pending=%s skipped=%s",
                            *(result.get(k, 0) for k in
                              ("scanned", "cancelled", "confirmed", "pending", "skipped")))
        except Exception as e:
            logger.exception("订单校准轮次异常，继续下一轮")
            log_op("system.thread_error", level="ERROR",
                   params={"thread": "order-reconcile"}, error=e)


def start_reconcile_thread() -> None:
    """幂等启动 daemon 校准线程（name="order-reconcile"，_thread_started 标志防重入）。

    间隔读环境变量 CHAGEE_RECONCILE_INTERVAL_SECONDS（默认 60 秒；<=0 则不启动）。
    """
    global _thread_started
    if _thread_started:
        return
    try:
        interval = int(os.environ.get("CHAGEE_RECONCILE_INTERVAL_SECONDS", "60"))
    except ValueError:
        interval = 60
    if interval <= 0:
        return   # 显式关闭校准线程
    _thread_started = True
    threading.Thread(target=_reconcile_loop, args=(interval,),
                     daemon=True, name="order-reconcile").start()
