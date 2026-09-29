"""支付事件链后台 watcher：自动探针支付会话 + 状态跃迁联动 + 外部回调分派。

为什么需要 watcher：H5 收银台的 /status 探针只在「有人开着页面」时工作——用户付完款
关掉浏览器、或压根没打开过链接，支付会话就停在 issued。本模块在主 API 进程内起
daemon 线程周期扫 issued 会话，以茶姬侧 getOrderStatus 为准收口：
  3/6 已支付 → 置 paid + 取餐号回写 + 审计 + 外部回调（若配置）
  7   已取消 → 置 cancelled + OrderRecord 同步 + 有券回滚
  1   待支付 → 不动；过宽限线且连续 3 轮仍 1 → 会话置 expired（订单深度校准仍归
              order_reconcile 线程，这里只做会话态收口）

与 payportal._probe_remote 的并发契约：两者可能同时发现已支付，mark_session 的单条
UPDATE CAS（WHERE status != 目标态）保证只有一方返回 True——本模块所有后续动作
（取餐号 detail / 审计 / 外部回调）都必须在 CAS 成功之后才执行，因此只触发一次。
启动：app.py startup 调 start_pay_watcher()（间隔环境变量
CHAGEE_PAYWATCH_INTERVAL_SECONDS，默认 2.0 秒）。
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import types
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from audit import log_audit
from database import SessionLocal
from models import ChageeAccount, PaySession
from oplog import heartbeat, log_op
from services import chagee_bridge as bridge
from services.order_reconcile import (RECONCILE_GRACE_SECONDS, confirm_coupon_usage,
                                      rollback_coupon_usage)
from services.pay_session import (
    EVENT_CALLBACK_DISPATCHED, EVENT_CALLBACK_FAILED, EVENT_ORDER_CANCELLED,
    EVENT_PAID_DETECTED, EVENT_PICKUP_FETCHED, EVENT_PROBE, EVENT_ROLLED_BACK,
    EVENT_SESSION_EXPIRED, ORDER_STATUS_LABELS, is_active, mark_session,
    record_event, token_prefix, upsert_order_record,
)

logger = logging.getLogger(__name__)

WATCH_BATCH_LIMIT = 20       # 每轮最多探针的单量（防止大会话库把轮次拖成分钟级）
EXPIRE_STREAK_ROUNDS = 3     # 过期会话置 expired 前需连续 N 轮探针仍返回 1
WATCHER_OPERATOR = "pay-watcher"   # 审计/券回滚的伪用户名（后台线程无请求上下文）

# 回调重试退避（秒）：初始尝试失败后按序沉睡再试，共 1+3 次
_CALLBACK_RETRY_BACKOFF = (1.0, 2.0, 4.0)

# 直连 opener：绕过系统/注册表代理——回调目标多为内网地址（127.0.0.1/局域网），
# Windows 代理设置会把这类请求劫持到代理服务器导致必然失败
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 过期清扫的连 1 计数（order_no → 连续返回 1 的轮次）。为什么放内存不落库：
# 只是「过宽限线后别急着判死」的降级依据，进程重启归零最多多探 3 轮，
# 不值得为它加表列；非 1 结果 / 回到活跃窗即清零
_pending_streak: dict[str, int] = {}

_round_seq = 0   # 单调轮次号（降频跳轮的模运算基准；watcher 单线程 + 测试串行调用）

# services/x.py 上三层 = account_system/（config 与 database.DATA_DIR 同级定位）
_ACCOUNT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CALLBACK_CONFIG_PATH = os.path.join(_ACCOUNT_ROOT, "data", "pay_callback_config.json")
SECRET_KEY_PATH = os.path.join(_ACCOUNT_ROOT, "data", "secret.key")


# ---------------- 单会话动作 ----------------

def _bump_fail(db: Session, sess: PaySession) -> None:
    """探针失败计数 +1（降频依据；落库失败仅回滚不抛出）。"""
    try:
        sess.fail_count = (sess.fail_count or 0) + 1
        db.commit()
    except Exception as e:
        db.rollback()
        log_op("pay.probe_fail_bump", level="WARN", actor=WATCHER_OPERATOR,
               target=sess.order_no, result="failed", error=e)


def _handle_paid(db: Session, sess: PaySession, account, api, st: int) -> str:
    """3/6 已支付收口。顺序契约：必须先 CAS mark_session("paid") 成功再做
    detail/审计/回调——与 payportal._probe_remote 并发发现时 CAS 只有一方成功，
    后续动作（取餐号回写、外部回调）因此只执行一次；CAS 失败即对方已收口，直接退出。"""
    prefix = token_prefix(sess.pay_token)
    if not mark_session(db, sess.order_no, "paid", paid_at=datetime.now()):
        return "cas_lost"   # H5 探针/reconcile 已并发收口，本侧无需动作（探针本身已发生）
    db.refresh(sess)
    record_event(db, sess.order_no, prefix, EVENT_PAID_DETECTED,
                 {"source": WATCHER_OPERATOR, "order_status": st})
    detail: dict = {}
    if not sess.pickup_no:
        try:
            detail = api.order_detail(sess.order_no) or {}
            if detail.get("pickupNo"):
                sess.pickup_no = str(detail["pickupNo"])
                db.commit()
        except Exception as e:
            # 取餐号失败不影响已支付收口（H5 端 /status 仍可补取），只留痕
            record_event(db, sess.order_no, prefix, EVENT_PROBE,
                         {"ok": False, "stage": "order_detail",
                          "error": f"{type(e).__name__}: {e}"[:200],
                          "source": WATCHER_OPERATOR})
    # 详情字段回填 OrderRecord（无 detail 时至少把状态推进到 3/6，空值不覆盖已有值）
    upsert_order_record(db, sess.account_id, sess.order_no, status=st,
                        status_label=ORDER_STATUS_LABELS.get(st, str(st)),
                        pickup_no=sess.pickup_no or None,
                        pay_amount=str(detail.get("payAmount") or "") or None,
                        total_amount=str(detail.get("totalAmount") or "") or None)
    # 券核销收口：支付确认 → 券档案迁移历史桶（幂等，与取消回滚链对称；
    # 失败仅 WARN——order_reconcile 线程的 paid 分支稍后会补迁，不影响收口主流程）
    if sess.coupon_code:
        try:
            confirm_coupon_usage(db, sess.coupon_code, sess.order_no,
                                 operator=WATCHER_OPERATOR)
        except Exception:
            db.rollback()
            logger.warning("订单 %s 券核销迁移失败（校准线程会补迁）",
                           sess.order_no, exc_info=True)
    record_event(db, sess.order_no, prefix, EVENT_PICKUP_FETCHED,
                 {"source": WATCHER_OPERATOR, "order_status": st,
                  "pickup_no": sess.pickup_no or ""})
    # 伪用户审计（后台线程无请求上下文，username 列承载 operator——order_reconcile 同写法）
    log_audit(db, None, types.SimpleNamespace(id=0, username=WATCHER_OPERATOR),
              "feature.pay_watcher", sess.order_no,
              {"order_status": st, "pickup_no": sess.pickup_no or "",
               "pay_amount": sess.pay_amount or ""})
    # 外部回调：paid 必发一次；此刻取餐号已回填则同发 pickup
    dispatch_payment_callback(db, sess, "paid")
    if sess.pickup_no:
        dispatch_payment_callback(db, sess, "pickup")
    # 异步订单中枢联动（2026-09-29）：茶姬单收口 → 登记单推进 completed + 取餐码缓存
    # + 按单回调（惰性导入防环；联动失败绝不影响支付收口主流程）
    try:
        from services import intake_notify
        intake_notify.on_chagee_order_paid(db, sess.order_no, sess.pickup_no or "")
    except Exception:
        logger.warning("intake 登记单支付联动失败（不影响收口）order_no=%s",
                       sess.order_no, exc_info=True)
    log_op("pay.watcher_paid", actor=WATCHER_OPERATOR, target=sess.order_no,
           params={"order_status": st, "pickup_no": sess.pickup_no or "",
                   "pay_amount": sess.pay_amount or ""})
    return "paid"


def _handle_cancelled(db: Session, sess: PaySession) -> str:
    """7 已取消收口：CAS 成功才做事件/同步/回滚（与 H5 探针、reconcile 线程三方并发安全）。"""
    prefix = token_prefix(sess.pay_token)
    if not mark_session(db, sess.order_no, "cancelled"):
        return "cas_lost"
    db.refresh(sess)
    record_event(db, sess.order_no, prefix, EVENT_ORDER_CANCELLED,
                 {"source": WATCHER_OPERATOR, "order_status": 7})
    upsert_order_record(db, sess.account_id, sess.order_no, status=7,
                        status_label=ORDER_STATUS_LABELS[7])
    if sess.coupon_code:
        try:
            rollback_coupon_usage(db, sess.coupon_code, sess.order_no,
                                  operator=WATCHER_OPERATOR)
            record_event(db, sess.order_no, prefix, EVENT_ROLLED_BACK,
                         {"coupon_code": sess.coupon_code, "operator": WATCHER_OPERATOR})
        except Exception as e:
            # 会话已终态、watcher 不会再碰这单：回滚失败必须留痕等人工核对
            db.rollback()
            logger.warning("订单 %s 取消后券回滚失败（需人工核对券 %s）",
                           sess.order_no, sess.coupon_code, exc_info=True)
            log_op("coupon.rollback", level="WARN", actor=WATCHER_OPERATOR,
                   target=sess.order_no, result="failed", error=e,
                   params={"coupon_code": sess.coupon_code})
    log_op("pay.watcher_cancelled", actor=WATCHER_OPERATOR, target=sess.order_no,
           params={"coupon_rolled_back": bool(sess.coupon_code)})
    # 异步订单中枢联动：茶姬单取消/超时 → 登记单 failed + 按单回调（惰性导入防环）
    try:
        from services import intake_notify
        intake_notify.on_chagee_order_cancelled(db, sess.order_no)
    except Exception:
        logger.warning("intake 登记单取消联动失败（不影响收口）order_no=%s",
                       sess.order_no, exc_info=True)
    return "cancelled"


def _probe_one(db: Session, sess: PaySession) -> str:
    """单会话探针 + 跃迁分派。返回动作名（统计/日志用）：paid/cancelled/expired/
    pending/fail/skip（账号不可用未探针）/cas_lost（并发方已收口）。跃迁口径与
    payportal._probe_remote 完全一致。"""
    account = db.get(ChageeAccount, sess.account_id)
    if account is None or account.status == "disabled" or not (account.token or ""):
        return "skip"   # 本地不可用账号不消耗远程探针（order_reconcile 同口径）
    prefix = token_prefix(sess.pay_token)
    overdue = not is_active(sess)   # 过 pay_deadline+宽限（is_active 已含 issued 判定）
    try:
        api = bridge.trade_api(account)
        st = int(api.order_status(sess.order_no) or 0)
    except bridge.SessionExpiredError as e:
        # 凭证失效：账号置 expired（下轮 scan 仍会命中但失败计数会降频），会话数据不动
        try:
            account.status = "expired"
            db.commit()
        except Exception:
            db.rollback()
        _bump_fail(db, sess)
        record_event(db, sess.order_no, prefix, EVENT_SESSION_EXPIRED,
                     {"source": WATCHER_OPERATOR, "error": str(e)[:200]})
        _pending_streak.pop(sess.order_no, None)
        return "fail"
    except Exception as e:
        _bump_fail(db, sess)
        record_event(db, sess.order_no, prefix, EVENT_PROBE,
                     {"ok": False, "source": WATCHER_OPERATOR,
                      "error": f"{type(e).__name__}: {e}"[:200]})
        return "fail"
    if sess.fail_count:
        try:
            sess.fail_count = 0   # 探针恢复即解除降频
            db.commit()
        except Exception:
            db.rollback()

    if st == 1:
        if not overdue:
            _pending_streak.pop(sess.order_no, None)   # 回到活跃窗（重铸过），清计数
            return "pending"
        streak = _pending_streak.get(sess.order_no, 0) + 1
        _pending_streak[sess.order_no] = streak
        # 连 N 轮仍 1 才收口 expired：宽限线后茶姬侧 autoCancel 有延迟，且 H5 端
        # 可能在临界点完成支付——多探几轮再判死。订单深度校准归 reconcile 线程
        if streak >= EXPIRE_STREAK_ROUNDS and mark_session(db, sess.order_no, "expired"):
            record_event(db, sess.order_no, prefix, EVENT_PROBE,
                         {"ok": True, "last_status": 1, "expired": True,
                          "deadline": str(sess.pay_deadline or "")})
            log_op("pay.session_expired", actor=WATCHER_OPERATOR, target=sess.order_no)
            _pending_streak.pop(sess.order_no, None)
            return "expired"
        return "pending"
    _pending_streak.pop(sess.order_no, None)
    if st in (3, 6):
        return _handle_paid(db, sess, account, api, st)
    if st == 7:
        return _handle_cancelled(db, sess)
    logger.warning("订单 %s 茶姬侧返回未知状态码 %s，不动数据", sess.order_no, st)
    return "pending"


# ---------------- 轮次扫描 ----------------

def _on_backoff(sess: PaySession) -> bool:
    """连续失败会话降频：fail_count>=6 时按 fail_count//3 步进跳轮（6→每 2 轮探 1 次、
    9→每 3 轮…），避免坏单（账号过期/网络分区）占满每轮探针名额挤掉正常单。"""
    fc = sess.fail_count or 0
    if fc < 6:
        return False
    step = fc // 3
    return step > 0 and (_round_seq % step) != 0


def _scan_once(db: Session) -> dict:
    """一轮扫描：活跃会话探针（≤20）+ 过期清扫（≤20），order_no 排序保证确定性轮转。"""
    global _round_seq
    _round_seq += 1
    # 先 .all() 物化扫描集：逐单 commit 不会扰动遍历游标（order_reconcile 同做法）
    sessions = (db.query(PaySession).filter(PaySession.status == "issued")
                .order_by(PaySession.order_no).all())
    active = [s for s in sessions if is_active(s)]
    overdue = [s for s in sessions if not is_active(s)]
    stats = {"scanned": len(sessions), "probed": 0, "paid": 0, "cancelled": 0,
             "expired": 0, "pending": 0, "fail": 0, "skip": 0}
    for lane in (active, overdue):
        probed = 0
        for sess in lane:
            if probed >= WATCH_BATCH_LIMIT:
                break
            if _on_backoff(sess):
                continue   # 降频跳轮：不探针、不占名额
            probed += 1
            try:
                action = _probe_one(db, sess)
            except Exception:
                # 单会话异常不拖垮整轮（如 SQLite 偶发 locked）
                db.rollback()
                action = "fail"
                logger.warning("支付会话 %s 探针处理异常，跳过", sess.order_no, exc_info=True)
            stats[action] = stats.get(action, 0) + 1
            if action != "skip":
                stats["probed"] += 1
    if stats["probed"]:
        logger.info("支付 watcher 轮次：scanned=%s probed=%s paid=%s cancelled=%s "
                    "expired=%s pending=%s fail=%s skip=%s",
                    *(stats[k] for k in ("scanned", "probed", "paid", "cancelled",
                                         "expired", "pending", "fail", "skip")))
    return stats


def watch_once(db: Session | None = None) -> dict:
    """单轮扫描入口（db=None 自开会话；异常由后台循环兜底捕获，单轮失败不致命）。"""
    if db is not None:
        return _scan_once(db)
    with SessionLocal() as own:   # type: Session
        return _scan_once(own)


# ---------------- 外部回调分派 ----------------

def _load_callback_config() -> dict | None:
    """读 data/pay_callback_config.json（缺文件/解析失败返回 None → 分派整体 no-op）。
    为什么不做缓存：分派频率低（每单支付成功至多两次），每次现读便于热改配置。"""
    try:
        with open(CALLBACK_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def _callback_secret(cfg: dict) -> bytes:
    """回调签名密钥：config.secret 优先，缺省派生自 data/secret.key 文本。
    读不到（文件缺失）返回空串仍继续签名——分派是尽力而为的旁路，不因密钥缺失阻断。"""
    secret = str(cfg.get("secret") or "").strip()
    if secret:
        return secret.encode("utf-8")
    try:
        with open(SECRET_KEY_PATH, encoding="utf-8") as f:
            return f.read().strip().encode("utf-8")
    except Exception:
        return b""


def _post_callback(db: Session, order_no: str, prefix: str, event_type: str,
                   url: str, raw: bytes, headers: dict, timeout: float) -> bool:
    """单 URL 投递：初始尝试 + 按退避重试，每次失败记 callback_failed（含剩余重试，
    重试耗尽与否可从事件流直接读出），成功记 callback_dispatched。"""
    attempts = len(_CALLBACK_RETRY_BACKOFF) + 1
    last_err = ""
    for attempt in range(attempts):
        if attempt:
            time.sleep(_CALLBACK_RETRY_BACKOFF[min(attempt - 1,
                                                   len(_CALLBACK_RETRY_BACKOFF) - 1)])
        try:
            req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
            with _OPENER.open(req, timeout=timeout) as resp:   # 非 2xx 抛 HTTPError
                http_status = int(resp.getcode() or 0)
            record_event(db, order_no, prefix, EVENT_CALLBACK_DISPATCHED,
                         {"url": url, "http_status": http_status,
                          "event": event_type, "attempt": attempt + 1})
            return True
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]
            retries_left = attempts - attempt - 1
            logger.warning("支付回调投递失败 url=%s event=%s attempt=%s/%s 剩余重试=%s（%s）",
                           url, event_type, attempt + 1, attempts, retries_left, last_err)
            log_op("pay.callback", level="WARN", actor=WATCHER_OPERATOR,
                   target=order_no, result="failed", error=e,
                   params={"event": event_type, "attempt": attempt + 1})
            record_event(db, order_no, prefix, EVENT_CALLBACK_FAILED,
                         {"url": url, "event": event_type, "error": last_err,
                          "retries_left": retries_left})
    return False


def dispatch_payment_callback(db: Session, sess: PaySession, event_type: str) -> None:
    """支付事件外发：event_type ∈ "paid"|"pickup"。配置缺失（未配 urls）整体 no-op。
    签名：X-CHAGEE-Signature = hex(hmac_sha256(secret, raw_body))，接收端用同一
    secret 对原始 body 验签（JSON 字节级比对，故 body 一次性序列化后原样投递）。"""
    if event_type not in ("paid", "pickup"):
        raise ValueError(f"未知回调事件类型: {event_type}")
    cfg = _load_callback_config()
    if not cfg:
        return
    urls = [str(u) for u in (cfg.get("urls") or []) if str(u).startswith("http")]
    if not urls:
        return
    try:
        timeout = float(cfg.get("timeout") or 5)
    except (TypeError, ValueError):
        timeout = 5.0
    secret = _callback_secret(cfg)
    body = {
        "event": event_type,
        "order_no": sess.order_no,
        "account_id": sess.account_id,
        "pay_no": sess.pay_no or "",
        "out_trade_no": sess.out_trade_no or "",
        "pay_amount": sess.pay_amount or "",
        "pickup_no": sess.pickup_no or "",
        "paid_at": str(sess.paid_at or ""),
        "dispatched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "X-CHAGEE-Signature": hmac.new(secret, raw, hashlib.sha256).hexdigest()}
    prefix = token_prefix(sess.pay_token)
    for url in urls:
        try:
            _post_callback(db, sess.order_no, prefix, event_type, url, raw,
                           headers, timeout)
        except Exception as e:
            # 单 URL 投递意外（如 record_event 之外的路由异常）不阻断其余 URL
            db.rollback()
            logger.warning("支付回调分派异常 url=%s order_no=%s", url, sess.order_no,
                           exc_info=True)
            log_op("pay.callback", level="WARN", actor=WATCHER_OPERATOR,
                   target=sess.order_no, result="failed", error=e,
                   params={"event": event_type})


# ---------------- 后台 watcher 线程（app.py startup 挂载） ----------------

_started = False


def _watch_loop(interval: float) -> None:
    while True:
        time.sleep(interval)
        heartbeat("pay-watcher")   # 线程存活心跳（限频落库，log_monitor 据此判循环卡死）
        try:
            watch_once()   # 自开会话
        except Exception as e:
            logger.exception("支付 watcher 轮次异常，继续下一轮")
            log_op("system.thread_error", level="ERROR",
                   params={"thread": "pay-watcher"}, error=e)


def start_pay_watcher() -> None:
    """幂等启动 daemon 支付 watcher 线程（name="pay-watcher"，_started 标志防重入）。
    间隔读环境变量 CHAGEE_PAYWATCH_INTERVAL_SECONDS（默认 2.0 秒；<=0 则不创建线程，
    供离线测试彻底关闭）。仿 order_reconcile.start_reconcile_thread。"""
    global _started
    if _started:
        return
    try:
        interval = float(os.environ.get("CHAGEE_PAYWATCH_INTERVAL_SECONDS", "2.0"))
    except ValueError:
        interval = 2.0
    if interval <= 0:
        return   # 显式关闭支付 watcher
    _started = True
    threading.Thread(target=_watch_loop, args=(interval,),
                     daemon=True, name="pay-watcher").start()
