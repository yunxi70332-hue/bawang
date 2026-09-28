"""H5 收银台支付会话服务：一单一链接、token 即凭证、全链路事件流。

背景：settle→createOrder 得 PayLink（支付宝签名串 order_str），手机端才能拉起收银台；
浏览器无法直接消费 order_str。本模块在 PayLink 之上铸一层「支付会话」——
  - PaySession.order_no 唯一（一单一链接），pay_token（secrets.token_urlsafe(32)）即
    H5 收银台 URL 凭证（/pay/<token>，无 JWT——收银台是公开页，token 本身就是密钥）
  - 续付/重铸（continuePay）原地更新支付字段并追加 PayAttempt，token 永不变——
    二维码/聊天里发过的链接不会失效
  - 每次状态变化记 PayEventLog（page_opened/probe/paid_detected/...），供
    H5 页面、watcher 子代理、管理端 pay-events 查询三方对账

并发模型：主 API（8000）+ 收银台独立进程（8010）+ 后台 watcher 同时读写 SQLite，
靠 WAL（database.py 已启用）+ 本模块的短事务（每次调用独立 commit）保证不长时间持锁。
"""

import json
import logging
import os
import secrets
import socket
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from models import OrderRecord, PayAttempt, PayEventLog, PaySession
from oplog import log_op
from services import chagee_bridge as bridge
from services import pay_broadcast
from services import pay_params
from services.order_reconcile import RECONCILE_GRACE_SECONDS, rollback_coupon_usage

logger = logging.getLogger(__name__)

# 支付窗（与 routers/orders.py 一致；复制常量避免 services 反向依赖路由层，
# 与 order_reconcile.py 的做法相同）。官方语义：paymentExpiryType=autoCancel、
# paymentExpiryTimestamp=下单+10min（order_wire_field_analysis §⑥ wire 实证），
# 到期茶姬侧自动转 7，且倒计时**不随 continuePay 重置**。支付宝 time_expire
# （下单+30min）比它长，直接当 pay_deadline 会造成「茶姬已取消、支付宝交易仍
# 可扣款」的支付无回传空窗——所有 pay_deadline 写入必须经 clamp_pay_deadline
# 钳制到该窗口（2026-09-27 定案）。
PAY_WINDOW_SECONDS = 600
# 收银台独立进程端口（start_payportal.bat 固定 8010，H5 URL 拼接用）
PAY_PORTAL_PORT = 8010

# ---------------- 事件名常量（取值域见 models.PAY_EVENTS；watcher 子代理按此对齐） ----------------
EVENT_LINK_ISSUED = "link_issued"
EVENT_REMINT = "remint"
EVENT_PAGE_OPENED = "page_opened"
EVENT_PROBE = "probe"
EVENT_PAID_DETECTED = "paid_detected"
EVENT_PICKUP_FETCHED = "pickup_fetched"
EVENT_CALLBACK_DISPATCHED = "callback_dispatched"
EVENT_CALLBACK_FAILED = "callback_failed"
EVENT_ROLLED_BACK = "rolled_back"
EVENT_ORDER_CANCELLED = "order_cancelled"
EVENT_SWITCH_FULL_PRICE = "switch_full_price"
EVENT_SESSION_EXPIRED = "session_expired"   # 探针触发账号凭证失效（pay_sessions.status 无关）
EVENT_CASHIER_UPDATED = "cashier_updated"   # 回填实时捕获的官方收银台 URL（mobilegw 短窗）

# 订单状态文案（与 routers/orders.py ORDER_STATUS_LABELS 同步）
ORDER_STATUS_LABELS = {1: "待支付", 3: "制作中", 6: "已完成", 7: "已取消"}


class PaySessionError(Exception):
    """支付会话域本地错误（前置条件不满足等），路由层转 400。"""


def _parse_deadline(expire_at) -> datetime | None:
    """支付宝 time_expire 文本 → DateTime（格式与 routers/orders._parse_deadline 一致）。

    '+' 容错：引擎 PayLink.expire_at 来自 biz_content 解码，形如 "2026-09-27+16:20:42"
    （URL 表单编码的空格），先归一化 '+'→' ' 再解析（2026-09-27 实弹定案，此前静默 None
    导致壳页无倒计时、watcher 过期清扫拿不到锚点）。
    """
    try:
        return datetime.strptime(str(expire_at or "").replace("+", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def clamp_pay_deadline(deadline, order_created_at: datetime | None = None) -> datetime | None:
    """支付截止钳制到官方 10 分钟 autoCancel 窗口：min(支付宝截止, 下单+PAY_WINDOW_SECONDS)。

    官方 paymentExpiryTimestamp=下单+10min、autoCancel、不随续付重置（wire 实证）；
    支付宝 time_expire=下单+30min 且 continuePay 顺延——比官方窗长出的 20 分钟里支付
    会「支付宝扣款成功、茶姬订单已取消」，系统收不到支付回传。deadline 缺失时直接
    返回官方窗口（有总比无锚点好）。order_created_at 缺省取 now（新建订单语境即下单
    时刻；对无 created_at 的旧数据 remint 也宁可早收口不晚收口）。入参兼容
    datetime / time_expire 文本。min 运算幂等，多处钳制（upsert + ensure）结果一致。
    """
    dl = deadline if isinstance(deadline, datetime) else _parse_deadline(deadline)
    official = (order_created_at or datetime.now()) + timedelta(seconds=PAY_WINDOW_SECONDS)
    if dl is None:
        return official
    return min(dl, official)


def server_now_ms() -> int:
    """服务器权威时间（epoch ms）。双进程同机同源，前端用它做时钟偏移校正：
    offset = server_time − Date.now()，倒计时一律按 pay_deadline_ts − (Date.now()+offset) 计算。"""
    return int(time.time() * 1000)


def pay_deadline_ts(sess) -> int | None:
    """支付截止的 epoch ms（钳制后 pay_deadline），下发前端做绝对时间倒计时。

    为什么用绝对时间戳而非 remaining_seconds 单打天下：remaining 是响应落地的瞬间快照，
    网络往返/页面渲染延迟都会吃掉它；绝对锚点 + 前端本地时钟校正才能保证多端一致。
    无截止时间返回 None（前端自行处理无窗场景）。"""
    if not sess or not sess.pay_deadline:
        return None
    return int(sess.pay_deadline.timestamp() * 1000)


def token_prefix(token: str) -> str:
    """事件流里只存 token 前 8 位（溯源够用，降低事件表外泄面）。"""
    return str(token or "")[:8]


def _fmt_money(v) -> str:
    """金额统一两位小数字符串（派生展示值格式化，异常回退原文）。"""
    try:
        return f"{Decimal(str(v)).quantize(Decimal('0.01'))}"
    except (InvalidOperation, ValueError, TypeError):
        return str(v or "")


# ---------------- 会话核心：铸造 / 查询 / 状态迁移 ----------------

# ---------------- 支付宝官方 H5 收银台 URL（套壳跳转目标） ----------------

# 配置缓存：autopay_config.json 的 cashier 三元组（session/utdid/tid）变更频率低，60s 重读
_CASHIER_CFG_CACHE: dict = {"cfg": None, "at": 0.0}
_CASHIER_CFG_TTL = 60.0


def _load_cashier_config() -> dict:
    """读 data/autopay_config.json（缺文件/解析失败返回空 dict——fail-soft）。"""
    now = time.time()
    if _CASHIER_CFG_CACHE["cfg"] is None or now - _CASHIER_CFG_CACHE["at"] > _CASHIER_CFG_TTL:
        cfg: dict = {}
        try:
            # services/x.py 上三层 = account_system/（config 与 database.DATA_DIR 同级定位）
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "data", "autopay_config.json")
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}
        _CASHIER_CFG_CACHE.update(cfg=cfg, at=now)
    return _CASHIER_CFG_CACHE["cfg"]


def alipay_cashier_url(order_str: str) -> str | None:
    """order_str → 支付宝官方 H5 收银台 URL（浏览器直开、登录后付款的真实收银台）。

    凭证时效（2026-09-27 两轮实弹定案）：session/utdid/tid 是云手机 mobilegw 铸造的
    设备级三元组，**同窗内**可跨单复用（上午两单实证）；mobilegw 会话过期后收银台
    返回「你的访问已超时」(mobileclientgw-42-9246，16 时段实证)。因此静态配置仅在
    捕获后短窗有效——配置默认 enabled=false；需浏览器直付时用
    POST /orders/{order_no}/cashier-url 回填实时捕获的新链接（推荐流程）。
    fail-soft：任何失败返回 None，上游照常下发壳页链接，绝不阻塞支付串。
    """
    try:
        from alipay_autopay import build_cashier_url, parse_order_str  # noqa: E402  # scripts/ 已由 chagee_bridge 注入 sys.path
        cfg = _load_cashier_config()
        if not cfg.get("enabled", True):
            return None
        cashier = cfg.get("cashier") or {}
        if not cashier.get("session"):
            return None
        parsed = parse_order_str(order_str or "")
        return build_cashier_url(parsed, session=cashier.get("session"),
                                 utdid=cashier.get("utdid"), tid=cashier.get("tid"))
    except Exception:
        logger.warning("构造支付宝收银台 URL 失败（回退 portal 壳页链接）", exc_info=True)
        return None


def ensure_pay_session(db: Session, account_id: int, order_no, link,
                       mode: str = "partial", coupon_code: str = "") -> PaySession:
    """铸造或续铸支付会话（幂等入口，order_no 已存在则原地更新、token 不变）。

    - 不存在：新建会话（生成 pay_token）+ PayAttempt + link_issued 事件
    - 已存在：更新 pay_no/out_trade_no/order_str/金额/截止时间，status 重置 issued、
      fail_count=0，追加 PayAttempt + remint 事件（重铸新支付串但链接不变）

    total_amount 取 OrderRecord 的订单总额（PayLink.total_amount 是支付宝侧实付差额，
    语义不同）；OrderRecord 缺失时回退 link.total_amount。
    """
    order_no = str(order_no or "").strip()
    rec = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).first()
    total_amount = (rec.total_amount if rec and rec.total_amount else link.total_amount)

    sess = db.query(PaySession).filter(PaySession.order_no == order_no).first()
    created = sess is None
    if created:
        sess = PaySession(account_id=account_id, order_no=order_no,
                          pay_token=secrets.token_urlsafe(32))
        db.add(sess)
    # 原地更新：续付场景金额/流水都会变化；status 回到 issued（仅 issued 态的会话会被续付，
    # 这里重置是为防御历史脏数据）；mode/coupon 按最新调用方语境覆盖
    if mode:
        sess.mode = mode
    if coupon_code:
        sess.coupon_code = coupon_code
    sess.pay_no = str(link.pay_no or "")
    sess.out_trade_no = str(link.out_trade_no or "")
    sess.order_str = link.order_str or ""
    sess.alipay_cashier_url = alipay_cashier_url(link.order_str) or ""
    sess.total_amount = str(total_amount or "")
    sess.pay_amount = str(link.total_amount or "")
    sess.pay_deadline = clamp_pay_deadline(link.expire_at,
                                           rec.created_at if rec else None)
    sess.status = "issued"
    sess.fail_count = 0
    db.flush()   # 拿自增 id 供 PayAttempt 外键
    db.add(PayAttempt(pay_session_id=sess.id, pay_no=str(link.pay_no or ""),
                      out_trade_no=str(link.out_trade_no or ""),
                      expire_at=str(link.expire_at or "")[:32]))
    db.commit()   # 短事务：铸造即提交，不与远程调用混在同一事务
    record_event(db, order_no, token_prefix(sess.pay_token),
                 EVENT_LINK_ISSUED if created else EVENT_REMINT,
                 {"pay_no": sess.pay_no, "out_trade_no": sess.out_trade_no,
                  "mode": sess.mode, "expire_at": str(link.expire_at or ""),
                  "pay_amount": sess.pay_amount})
    # oplog 留痕：本函数无只读路径，每次调用必为「新建铸造」或「续铸」之一
    log_op("pay.session_minted" if created else "pay.session_reminted",
           actor="system", target=order_no,
           params={"mode": sess.mode, "coupon_code": coupon_code or None,
                   "pay_token_prefix": token_prefix(sess.pay_token)})
    # 官方收银台支付参数串：静态构造出 URL 即同步落独立表（覆盖式，最新短窗语义）；
    # fail-soft——参数串是旁路增强，绝不阻塞支付串下发
    if sess.alipay_cashier_url:
        try:
            pay_params.save_pay_params(db, order_no, sess.alipay_cashier_url,
                                       source="static-config", sess=sess)
        except Exception:
            logger.warning("支付参数串生成失败（忽略）order_no=%s", order_no, exc_info=True)
    return sess


# mark_session 允许随状态一并更新的字段白名单（防调用方误写 pay_token/order_no 等锚点字段）
_SESSION_WRITABLE = {"paid_at", "pickup_no", "pay_deadline", "fail_count",
                     "pay_no", "out_trade_no", "order_str", "coupon_code",
                     "total_amount", "pay_amount", "mode"}


def mark_session(db: Session, order_no: str, status: str, **fields) -> bool:
    """状态 CAS 迁移：仅当会话当前不处于目标态时更新并返回 True。

    为什么用单条 UPDATE 的 WHERE 条件做 CAS：watcher 线程与 H5 探针可能并发发现
    「已支付」，UPDATE ... WHERE status != :new 在 SQLite 单语句原子性下保证只有一方
    返回 1（另一方 0 → False），后续的回写/回调动作因此只触发一次。fields 仅接受
    白名单列且非 None 值（空值不覆盖）。
    """
    updated = (db.query(PaySession)
                 .filter(PaySession.order_no == order_no, PaySession.status != status)
                 .update({"status": status, "updated_at": datetime.now()},
                         synchronize_session=False))
    if not updated:
        db.rollback()
        return False
    if fields:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).first()
        if sess:
            for k, v in fields.items():
                if k in _SESSION_WRITABLE and v is not None:
                    setattr(sess, k, v)
    db.commit()
    # 跨进程联动：收口成功（本方 CAS 赢家）即广播给收银台进程的 SSE 订阅者。
    # paid/cancelled/expired 均通知；fire-and-forget——通知失败只 debug（收银台 1s
    # 兜底轮询会补），绝不影响状态收口主流程
    try:
        pay_broadcast.notify_session_change(order_no, status)
    except Exception:
        logger.debug("支付会话状态广播异常（忽略）order_no=%s status=%s",
                     order_no, status, exc_info=True)
    return True


def get_by_token(db: Session, token: str) -> PaySession | None:
    """token → 支付会话（token 即凭证，查不到即无效链接）。"""
    return db.query(PaySession).filter(PaySession.pay_token == (token or "").strip()).first()


def get_by_order_no(db: Session, order_no: str) -> PaySession | None:
    return db.query(PaySession).filter(PaySession.order_no == (order_no or "").strip()).first()


def is_active(sess: PaySession) -> bool:
    """会话是否仍可支付：issued 且未过 pay_deadline + 校准宽限（与 reconcile 宽限同源 120s，
    避免 H5 端比后台校准更早放弃导致两边状态打架；无截止时间视为仍活跃）。"""
    if not sess or sess.status != "issued":
        return False
    if not sess.pay_deadline:
        return True
    return datetime.now() <= sess.pay_deadline + timedelta(seconds=RECONCILE_GRACE_SECONDS)


def remaining_seconds(sess: PaySession) -> int | None:
    """距支付截止的秒数（已过为 0；无截止时间返回 None 由前端自行处理）。"""
    if not sess or not sess.pay_deadline:
        return None
    return max(0, int((sess.pay_deadline - datetime.now()).total_seconds()))


def record_event(db: Session, order_no, token_prefix_: str, event: str,
                 payload: dict | None = None) -> None:
    """事件流落库：payload 预序列化清洗（default=str 保证 JSON 可序列化），
    任何异常只告警不抛出——事件是旁路观测数据，绝不能影响支付主流程。"""
    try:
        safe = json.loads(json.dumps(payload or {}, ensure_ascii=False, default=str))
        db.add(PayEventLog(order_no=str(order_no or "")[:64],
                           pay_token_prefix=str(token_prefix_ or "")[:8],
                           event=str(event or "")[:32], payload=safe))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("支付事件落库失败（忽略）event=%s order_no=%s", event, order_no,
                       exc_info=True)
        log_op("pay.event_log", level="WARN", target=order_no, result="failed",
               error=e, params={"event": event})


# ---------------- H5 URL 构造 ----------------

_H5_BASE_CACHE: dict = {"base": None, "at": 0.0}
_H5_BASE_TTL = 60.0   # 基址缓存 60s：IP 探测有系统调用开销，且本机 IP 很少变化


def _detect_lan_ip() -> str:
    """UDP connect 探测本机局域网出口 IP（仅让内核选默认路由，不实际发包），
    探测失败回退 127.0.0.1（收银台链接退化为仅本机可访问）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def build_h5_url(token: str) -> str:
    """token → 浏览器可直开的收银台 URL。

    基址优先环境变量 CHAGEE_PAY_BASE_URL（生产可指向反代域名）；否则探测局域网 IP
    拼 http://<ip>:8010（手机与后端同网段时可直接扫码/点链访问）。结果缓存 60s。
    """
    now = time.time()
    if _H5_BASE_CACHE["base"] is None or now - _H5_BASE_CACHE["at"] > _H5_BASE_TTL:
        base = os.environ.get("CHAGEE_PAY_BASE_URL", "").strip().rstrip("/")
        if not base:
            base = f"http://{_detect_lan_ip()}:{PAY_PORTAL_PORT}"
        _H5_BASE_CACHE.update(base=base, at=now)
    return f"{_H5_BASE_CACHE['base']}/pay/{token}"


def build_pay_payload(link, token: str | None = None,
                      alipay_cashier: str | None = None) -> dict:
    """PayLink → 响应字段（结构对齐 routers/orders._pay_link_payload）。

    h5_url 恒为自建壳页（稳定可达：拉起支付宝App / 复制支付串 / 状态轮询 / 取餐码）。
    alipay_cashier_url 为官方收银台直链，可空——mobilegw 会话仅捕获后短窗有效，
    过期后打开显示「访问已超时」(mobileclientgw-42-9246，2026-09-27 16 时段实证)，
    需浏览器直付时用 POST /orders/{order_no}/cashier-url 回填实时捕获的新链接。
    不传 token 保持旧契约（h5_url=None，兼容存量调用与离线测试）。
    """
    portal = build_h5_url(token) if token else None
    return {
        "result": "partial",
        "order_no": link.order_no,
        "pay_no": link.pay_no,
        "out_trade_no": link.out_trade_no,
        "total_amount": link.total_amount,
        "expire_at": link.expire_at,
        "pay_window_seconds": PAY_WINDOW_SECONDS,
        "order_str": link.order_str,
        "h5_url": portal,
        "portal_url": portal,
        "alipay_cashier_url": alipay_cashier,
        "pay_token": token,
        "note": ("打开 h5_url 进入收银台壳页：拉起支付宝App完成付款、支付后自动展示取餐码；"
                 "alipay_cashier_url 非空时可浏览器直开官方收银台（云手机实时捕获的短窗链接，"
                 "超时重新捕获并回填）"
                 if token else
                 "支付宝侧扣款不在纯协议范围：人工模式请用手机完成支付；自动模式为实验性"),
    }


def pay_link_payload_with_session(db: Session, link, account_id: int,
                                  mode: str = "partial", coupon_code: str = "") -> dict:
    """ensure_pay_session + 含 h5_url 的响应 payload（orders 路由的会话版出口）。"""
    sess = ensure_pay_session(db, account_id, link.order_no, link,
                              mode=mode, coupon_code=coupon_code)
    # 旁路增强：后台线程经 frida 在云手机上铸该单收银台直链（create partial / pay manual /
    # continue-pay 三处调用点自动生效；续付重铸新 session 也正确——trigger 内部有防抖）。
    # fire-and-forget：任何失败只 WARN，绝不影响支付串下发。
    try:
        from services.cashier_mint import trigger_mint
        trigger_mint(db, link.order_no)
    except Exception:
        logger.warning("触发收银台铸造失败（忽略）order_no=%s", link.order_no, exc_info=True)
    payload = build_pay_payload(link, token=sess.pay_token,
                                alipay_cashier=sess.alipay_cashier_url or None)
    # 时间戳同步契约（docs/pay_timesync_design_20260928.md）：绝对锚点下发，前端做时钟校正
    payload.update({"server_time": server_now_ms(),
                    "pay_deadline_ts": pay_deadline_ts(sess)})
    # 官方收银台支付参数串（已捕获时随单下发；未捕获为空，前端轮询 cashier 端点补齐）
    try:
        payload.update(pay_params.payload_fields(db, link.order_no))
    except Exception:
        logger.warning("支付参数串读取失败（忽略）order_no=%s", link.order_no, exc_info=True)
    return payload


# ---------------- OrderRecord 低耦合 upsert（payportal / 重下流程共用） ----------------
# 不复用 routers/orders._upsert_order（路由模块私有），此处等价实现一份，
# 语义一致：order_no 存在则更新，空值不覆盖已有值。

def upsert_order_record(db: Session, account_id: int, order_no, **fields) -> OrderRecord | None:
    order_no = str(order_no or "").strip()
    if not order_no:
        return None
    rec = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).first()
    if not rec:
        rec = OrderRecord(account_id=account_id, order_no=order_no)
        db.add(rec)
    for k, v in fields.items():
        if k == "status":
            if v is not None:
                rec.status = int(v)
        elif v not in (None, ""):
            # pay_deadline 统一钳制到官方 10 分钟窗（新建锚 now，续付锚下单时刻，
            # 官方倒计时不随 continuePay 重置）——见 clamp_pay_deadline docstring
            if k == "pay_deadline":
                v = clamp_pay_deadline(v, rec.created_at)
            setattr(rec, k, v)
    db.commit()
    return rec


# ---------------- 券差额单 → 原价单（管理端点与 H5 收银台公开端点共用核心） ----------------

def switch_order_to_full_price(db: Session, account, order: OrderRecord, *,
                               operator: str = "pay-portal") -> dict:
    """券差额单切换为原价单：取消旧单并原价重下。

    前置校验（status==1 / scenario==partial / 有券 / 有快照）由调用方完成（HTTP 语义层）。
    流程：api.cancel → 本地置已取消 + 券回滚 → 旧会话置 cancelled → 用 order_target 快照
    复算（calculate_price + settle_direct 不带券）→ create_order → 新 OrderRecord（无券）
    → 新支付会话（新订单新链接，mode=full）→ switch_full_price 事件。
    协议异常原样上抛，由调用方映射 HTTP（orders._fail / payportal 502 兜底）。
    """
    snapshot = order.order_target or {}
    target = snapshot.get("target") or {}
    if not target:
        raise PaySessionError("历史订单缺少商品快照，无法原价重下")
    old_order_no = order.order_no
    coupon_code = order.coupon_code or ""
    api = bridge.trade_api(account)

    # 1) 茶姬侧取消（cancelOrder 静态端点无 wire 样本——与 orders.order_cancel 同口径，实验性）
    try:
        cancel_resp = api.cancel(old_order_no)
    except Exception as e:
        log_op("order.switch_full_price", level="ERROR", actor=operator,
               target=old_order_no, result="failed", error=e, params={"stage": "cancel"})
        raise

    # 2) 本地置已取消 + 券回滚（先提交订单态，再回滚券：rollback_coupon_usage 自管事务）
    order.status = 7
    order.status_label = ORDER_STATUS_LABELS[7]
    db.commit()
    rolled_back = False
    if coupon_code:
        try:
            rolled_back = rollback_coupon_usage(db, coupon_code, old_order_no,
                                                operator=operator)
        except Exception as e:
            # 半途风险：旧单已取消、券使用痕迹未回滚（rollback_coupon_usage 自管事务已回滚）
            log_op("order.switch_full_price", level="ERROR", actor=operator,
                   target=old_order_no, result="failed", error=e, params={"stage": "rollback"})
            raise
    old_sess = get_by_order_no(db, old_order_no)
    record_event(db, old_order_no, token_prefix(old_sess.pay_token) if old_sess else "",
                 EVENT_ORDER_CANCELLED,
                 {"source": EVENT_SWITCH_FULL_PRICE, "coupon_code": coupon_code,
                  "coupon_rolled_back": rolled_back})
    if rolled_back:
        record_event(db, old_order_no, token_prefix(old_sess.pay_token) if old_sess else "",
                     EVENT_ROLLED_BACK, {"coupon_code": coupon_code, "operator": operator})
    # 3) 旧支付会话置 cancelled（CAS 幂等：重复调用/并发安全）
    mark_session(db, old_order_no, "cancelled")

    # 4) 原价重下：快照复算（无券；必选加料按快照原样重放）
    #    no_recommend=True 是关键（2026-09-27 实弹定案）：不带该参数时服务端会自荐最优券
    #    并把抵扣算进 buyerRealPrice，随后 createOrder 因「金额含券抵扣但 discountList 为空」
    #    被服务端校验拒绝（原价重下 502 的根因）
    store_no = str(snapshot.get("store_no") or "")
    store_name = str(snapshot.get("store_name") or "")
    goods_desc = str(snapshot.get("goods_desc") or "")[:255]
    extra_entries = [api.build_extra_entry(o) for o in (snapshot.get("extra_list") or [])]
    try:
        price = api.calculate_price(target)
        settle = api.settle_direct(target, price, coupon_entry=None,
                                   extra_entries=extra_entries, no_recommend=True)
        outcome = api.create_order(settle, store_no, store_name)
    except Exception as e:
        # 半途风险：旧单已取消新单未建（切换流程不可自动重入，必须留痕等人工核对）
        log_op("order.switch_full_price", level="ERROR", actor=operator,
               target=old_order_no, result="failed", error=e, params={"stage": "recreate"})
        raise

    new_snapshot = {"target": target, "extra_list": snapshot.get("extra_list") or [],
                    "store_no": store_no, "store_name": store_name, "goods_desc": goods_desc}

    # 5) 落新 OrderRecord + 新支付会话（新订单新链接）
    if isinstance(outcome, bridge.OrderOutcome):
        # 极端分支：复算后 buyerRealPrice==0（如门店改价）走零元直通，无支付腿
        upsert_order_record(db, account.id, outcome.order_no, store_no=store_no,
                            store_name=store_name, goods_desc=goods_desc,
                            quantity=target.get("quantity", 1), coupon_code="",
                            total_amount=settle.total_trade_price,
                            pay_amount=outcome.pay_amount, scenario="zero",
                            status=outcome.status,
                            status_label=outcome.status_text or ORDER_STATUS_LABELS.get(outcome.status, ""),
                            pickup_no=outcome.pickup_no, order_target=new_snapshot)
        record_event(db, old_order_no, "", EVENT_SWITCH_FULL_PRICE,
                     {"new_order_no": outcome.order_no, "result": "zero",
                      "coupon_rolled_back": rolled_back})
        log_op("order.switch_full_price", actor=operator, target=old_order_no,
               params={"old_order_no": old_order_no, "new_order_no": outcome.order_no,
                       "coupon_rolled_back": rolled_back})
        return {"old_order_no": old_order_no, "new_order_no": outcome.order_no,
                "coupon_rolled_back": rolled_back, "result": "zero",
                "status": outcome.status,
                "status_label": outcome.status_text or ORDER_STATUS_LABELS.get(outcome.status, ""),
                "pay_amount": outcome.pay_amount, "pickup_no": outcome.pickup_no,
                "cancel_response": cancel_resp}

    link = outcome
    upsert_order_record(db, account.id, link.order_no, store_no=store_no,
                        store_name=store_name, goods_desc=goods_desc,
                        quantity=target.get("quantity", 1), coupon_code="",
                        total_amount=settle.total_trade_price,
                        pay_amount=settle.buyer_real_price, scenario="partial",
                        status=1, status_label=ORDER_STATUS_LABELS[1],
                        out_trade_no=link.out_trade_no,
                        pay_deadline=_parse_deadline(link.expire_at),
                        order_target=new_snapshot)
    sess = ensure_pay_session(db, account.id, link.order_no, link, mode="full", coupon_code="")
    record_event(db, old_order_no, "", EVENT_SWITCH_FULL_PRICE,
                 {"new_order_no": link.order_no, "result": "partial",
                  "coupon_rolled_back": rolled_back, "new_pay_token": token_prefix(sess.pay_token)})
    payload = build_pay_payload(link, token=sess.pay_token,
                                alipay_cashier=sess.alipay_cashier_url or None)
    payload.update({"old_order_no": old_order_no, "new_order_no": link.order_no,
                    "new_h5_url": payload.get("h5_url"),
                    "coupon_rolled_back": rolled_back, "cancel_response": cancel_resp,
                    # 时间戳同步契约：新会话的绝对锚点（壳页跳新链接前就能校准时钟）
                    "server_time": server_now_ms(),
                    "pay_deadline_ts": pay_deadline_ts(sess)})
    # 新单的官方收银台支付参数串（原价重下后旧参数串作废，此处恒为新单号记录）
    try:
        payload.update(pay_params.payload_fields(db, link.order_no))
    except Exception:
        logger.warning("支付参数串读取失败（忽略）order_no=%s", link.order_no, exc_info=True)
    log_op("order.switch_full_price", actor=operator, target=old_order_no,
           params={"old_order_no": old_order_no, "new_order_no": link.order_no,
                   "coupon_rolled_back": rolled_back})
    return payload


def deduction_amount(sess: PaySession) -> str:
    """券抵扣额 = 订单总额 − 实付差额（两位小数；金额字段异常时回退空串）。"""
    try:
        return _fmt_money(max(Decimal(str(sess.total_amount or "0"))
                              - Decimal(str(sess.pay_amount or "0")), Decimal("0")))
    except (InvalidOperation, ValueError, TypeError):
        return ""
