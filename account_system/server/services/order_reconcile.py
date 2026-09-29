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

import json
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


def _append_history(log: CouponUsageLog, from_state: str, to_state: str,
                    by: str, reason: str = "") -> None:
    """使用日志状态流转轨迹（§18）：JSON 追加一条 {at, from, to, by, reason}；坏数据容错为空表。"""
    try:
        hist = json.loads(log.state_history or "[]")
        if not isinstance(hist, list):
            hist = []
    except Exception:
        hist = []
    hist.append({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "from": from_state, "to": to_state,
                 "by": by or "system", "reason": (reason or "")[:200]})
    log.state_history = json.dumps(hist, ensure_ascii=False)


def _latest_usage_log(db: Session, coupon_code: str, order_no: str) -> CouponUsageLog | None:
    """取该券该单最新一条使用日志（单行生命周期锚点）。"""
    return (db.query(CouponUsageLog)
              .filter(CouponUsageLog.coupon_code == coupon_code,
                      CouponUsageLog.order_no == order_no)
              .order_by(CouponUsageLog.id.desc()).first())


def confirm_coupon_usage(db: Session, coupon_code: str, order_no: str,
                         operator: str = "system") -> bool:
    """支付确认后把券档案标记为已核销（bucket→historical，与 rollback_coupon_usage 对称成对）。

    成单时已预记 last_used_at/last_order_no，此处只迁移桶位；使用日志单行生命周期：
    pending 行原地迁移 success（金额快照保留，不新增行——避免使用统计/抵扣双算），
    流转 actor 与原因记入 state_history。幂等：桶位未变化、日志非 pending 则各自跳过。
    券不存在返回 False（不算错误）；任何异常 rollback 后向上抛出。返回 True 表示券存在。
    """
    try:
        rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).first()
        if not rec:
            return False
        changed = rec.bucket != "historical"
        rec.bucket = "historical"
        log = _latest_usage_log(db, coupon_code, order_no)
        if log and log.result == "pending":
            log.result = "success"
            _append_history(log, "pending", "success", operator, "支付确认，券真实核销")
        db.commit()
        if changed:
            log_op("coupon.confirm", actor=operator, target=order_no,
                   params={"coupon_code": coupon_code})
        return True
    except Exception:
        db.rollback()
        raise


def rollback_coupon_usage(db: Session, coupon_code: str, order_no: str,
                          operator: str = "system") -> bool:
    """订单取消时回滚券使用痕迹，使用日志原地迁移 rolled_back（§18 单行生命周期）。

    CouponRecord 复位为未使用（bucket→effective / last_used_at→None / last_order_no→""）；
    CouponUsageLog：pending/success 行原地置 rolled_back（金额/账号快照保留、operator 保持
    原下单人，流转 actor 记入 state_history），无行时补一条 rolled_back 兜底行（早期数据）。
    幂等：已 rolled_back 的行再调只复位档案字段，不重复迁移/不加行。
    券不存在返回 False（不算错误）；任何异常 rollback 后向上抛出。返回 True 表示券确实被回滚。
    """
    try:
        rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).first()
        if not rec:
            return False
        rec.bucket = "effective"
        rec.last_used_at = None
        rec.last_order_no = ""
        log = _latest_usage_log(db, coupon_code, order_no)
        if log is None:
            # 兜底：成单日志缺失（极早期数据）→ 从券档案补一条回滚行
            db.add(CouponUsageLog(
                coupon_code=coupon_code, coupon_name=rec.template_name,
                account_id=rec.account_id, account_label="", operator=operator,
                order_no=order_no, result="rolled_back",
                fail_reason="订单超时未支付，券状态自动回滚为未使用",
                used_at=datetime.now()))
        elif log.result in ("pending", "success"):
            from_state = log.result
            log.result = "rolled_back"
            log.fail_reason = "订单超时未支付，券状态自动回滚为未使用"
            _append_history(log, from_state, "rolled_back", operator,
                            "订单取消（茶姬侧未核销），券回退为未使用")
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
            # 支付会话联动（2026-09-28 取消链路漏洞修复）：此前只置订单 7+券回滚，
            # PaySession 停在 issued——H5 收银台倒计时照走、pay watcher 继续探已取消单。
            # mark_session CAS 置 cancelled（无会话/已终态返回 False 静默），成功才记
            # order_cancelled 事件（事件数与状态迁移一对一）；跨进程 SSE 通知由
            # mark_session 内部自动发出。延迟 import：pay_session 模块级反向依赖本模块
            from services.pay_session import (
                EVENT_ORDER_CANCELLED, get_by_order_no, mark_session, record_event, token_prefix,
            )
            if mark_session(db, order.order_no, "cancelled"):
                sess = get_by_order_no(db, order.order_no)
                record_event(db, order.order_no,
                             token_prefix(sess.pay_token) if sess else "",
                             EVENT_ORDER_CANCELLED, {"source": "reconcile", "order_status": 7})
            log_op("order.reconcile", actor=operator, target=order.order_no,
                   result="cancelled_rolled_back",
                   params={"coupon": order.coupon_code or None})
            return "cancelled_rolled_back"
        except Exception:
            db.rollback()
            raise
    if st in (3, 6):
        # 已支付：券真实核销，迁移历史桶（痕迹保留，不写使用日志避免双算）；
        # 仅回填订单状态（3=制作中, 6=已完成）。与 watcher/H5 探针并发发现时各自幂等
        order.status = st
        order.status_label = _STATUS_LABELS[st]
        if order.coupon_code:
            confirm_coupon_usage(db, order.coupon_code, order.order_no, operator=operator)
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
