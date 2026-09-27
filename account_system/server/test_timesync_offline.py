"""支付倒计时时间戳同步机制离线回归测试（FakeClient 回放，零真实网络）。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server && python test_timesync_offline.py
    （或 pytest test_timesync_offline.py -q；须单独跑，不与其它套件合跑——engine 互踩是既定约定）

覆盖（编号即用例名前缀，main() 按字典序执行；契约 docs/pay_timesync_design_20260928.md §1）：
  01 info/status 时间戳字段：server_time ≈ now、pay_deadline_ts 与库内 pay_deadline 同锚；
     expire_at 为钳制后本地文本（expire_in=1800 的 PayLink 证明被钳到 ≤ now+10min）
  02 order_cancel 联动：管理端取消 → PaySession cancelled + order_cancelled{source:
     manual-cancel} + OrderRecord=7；重复取消幂等（不产生第二条事件）
  03 reconcile st==7 联动：直调 reconcile_order → PaySession cancelled +
     order_cancelled{source: reconcile}
  04 mark_session 触发跨进程通知：CAS 成功即 notify(order_no, status)；CAS 失败不重复通知
  05 SSE sync 首帧：/pay/{token}/events 为 text/event-stream；issued 会话首帧 event: sync
     且 data 含 server_time/pay_deadline_ts（ASGI 进程内驱动首帧，10s 看门狗）；cancelled
     会话经标准 client.stream 验证「sync+终态帧后闭流」（本环境 TestClient 传输层缓冲
     全量响应体，无限流只能以 ASGI 直驱读首帧——有限流可全量读）
  06 /internal/broadcast 鉴权：无 header 401 / 错 token 401 / 对 token+不存在单 404 /
     对 token+存在会话 200 不抛
  07 remint 409 不回归：会话 cancelled 后 POST /pay/{token}/remint → 409
  08 零网络保障：全程仅触达回放覆盖的协议端点（未知 path 由 FakeClient AssertionError 拦截）

要点（与 test_payportal_offline.py 同模式）：
  - 先把 database.DB_PATH 指向 data/test_timesync.db 并重建 engine，再 import app
  - import 前置 CHAGEE_RECONCILE_INTERVAL_SECONDS=0 / CHAGEE_PAYWATCH_INTERVAL_SECONDS=0
    / CHAGEE_MINT_ENABLED=0 / **CHAGEE_PAY_BROADCAST_ENABLED=0**（mark_session 收口
    不发真 HTTP 到 8010）
  - monkeypatch services.chagee_bridge.build_client 为 FakeClient：未知 path 抛 AssertionError
  - FakeClient 默认回放 getOrderStatus→1（待支付）：/status 探针触发也无状态跃迁，
    用例里按需 _reset_fake(7) 切换
  - /internal/broadcast 仅收银台进程（pay_portal.py）挂载 internal_router，故另建
    TestClient(pay_portal.app)；secret 经 monkeypatch pay_broadcast._SECRET_PATH 指向
    临时文件（绝不读写真实 data/internal_broadcast.secret）
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根
sys.path.insert(0, BASE)

# 后台线程全关 + 铸造/跨进程广播关（必须在 import app 之前）：
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MINT_ENABLED"] = "0"
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"     # 离线环境杜绝真发 HTTP 到 8010
# 日志隔离：oplog 库与文本日志均指 test_timesync 前缀测试路径
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_timesync_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_timesync_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_timesync.db")
for _suffix in ("", "-journal", "-wal", "-shm"):
    _p = database.DB_PATH + _suffix
    if os.path.exists(_p):
        os.remove(_p)
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

database.engine = create_engine(
    f"sqlite:///{database.DB_PATH}", connect_args={"check_same_thread": False}, pool_pre_ping=True)
database.SessionLocal = sessionmaker(bind=database.engine, autoflush=False,
                                     autocommit=False, expire_on_commit=False)

import seed  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import pay_portal as pay_portal_module  # noqa: E402  （/internal/broadcast 仅此进程挂载）
from models import ChageeAccount, OrderRecord, PayAttempt, PayEventLog, PaySession, SystemUser  # noqa: E402
from routers import payportal as payportal_router  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import order_reconcile as reconcile_module  # noqa: E402
from services import pay_broadcast  # noqa: E402
from services import pay_session as ps  # noqa: E402

# ---------- 2. FakeClient（替换 build_client，杜绝真实网络） ----------

# getOrderStatus 默认回放 1（待支付）：/status 探针触发也无跃迁，状态类用例按需改
FAKE: dict = {"status": 1, "error": None}
CALLS: list[str] = []          # 假客户端实际收到的调用（零网络断言用）


def _reset_fake(status: int = 1) -> None:
    FAKE.update(status=status, error=None)


def _order_str(out_trade_no: str, amount: str = "10.00", expire_at: str = None) -> str:
    """构造 alipay.trade.app.pay 形态签名串（parse_order_str 可解析）。"""
    expire_at = expire_at or time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(time.time() + 590))
    biz = {"out_trade_no": out_trade_no, "total_amount": amount, "subject": "霸王茶姬",
           "product_code": "QUICK_MSECURITY_PAY", "time_expire": expire_at}
    return ("alipay_sdk=alipay-sdk-java-4.9.28.ALL&app_id=202100117&charset=utf-8"
            "&biz_content=" + urllib.parse.quote(json.dumps(biz, ensure_ascii=False))
            + "&sign=OFFLINETESTSIGN&sign_type=RSA2")


def _link(order_no: str, *, pay_no: str = None, out_trade_no: str = None,
          amount: str = "10.00", expire_in: float = 590) -> bridge.PayLink:
    """构造测试 PayLink（不经任何网络）；expire_in>600 用于证明钳制逻辑。"""
    expire_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + expire_in))
    out = out_trade_no or f"OT-{order_no[-6:]}"
    return bridge.PayLink(order_no=order_no, pay_no=pay_no or f"PN-{order_no[-6:]}",
                          order_str=_order_str(out, amount, expire_at),
                          out_trade_no=out, total_amount=amount, expire_at=expire_at)


class FakeClient:
    """按 path 后缀回放最小合成响应；未知 path 抛 AssertionError（零网络兜底）。"""

    def __init__(self, account):
        self.account = account

    def get(self, path, **kw):
        CALLS.append(path)
        if path.endswith("/customer/userInfo/query"):   # cancel/order_status 前置 _user_id
            return {"errcode": "0", "data": {"customerId": "1190099999",
                                             "mobileEncrypt": "AESxFAKETS", "nickName": "时间同步"}}
        raise AssertionError(f"未预期的 GET 协议调用: {path}")

    def whoami(self):
        return self.get("/user-client/customer/userInfo/query")

    def post(self, path, body=None, **kw):
        CALLS.append(path)
        if path.endswith("/order/getOrderStatus"):     # 轻探针：data 裸 int
            if FAKE["error"] is not None:
                raise FAKE["error"]
            return {"errcode": "0", "data": int(FAKE["status"])}
        if path.endswith("/order/cancelOrder"):        # 管理端取消：回放成功
            return {"errcode": "0", "data": True}
        raise AssertionError(f"未预期的 POST 协议调用: {path}")


bridge.build_client = lambda account: FakeClient(account)   # monkeypatch（order_cancel/reconcile 经 bridge.trade_api 命中）

# ---------- 3. 测试数据 ----------

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800009999").first():
        db.add(ChageeAccount(label="时间同步冒烟", phone="13800009999", device_uuid="uuid-offline-ts-1",
                             token="fake.token.ts", sk="fakesk", customer_id="1190099999",
                             status="online", group="默认"))
        db.commit()
    ACC_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800009999").one().id

# 主 app（/pay/* 公开路由）+ 收银台进程 app（多挂 internal_router）；
# 均不用 with 上下文——不触发生命周期（startup 的 init_oplog/线程挂载与本套件无关）
CLIENT = TestClient(app_module.app)
PP_CLIENT = TestClient(pay_portal_module.app)
_tokens: dict[str, str] = {}


def _login(username: str, password: str) -> str:
    if username not in _tokens:
        r = CLIENT.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败 {username}: {r.status_code} {r.text}"
        _tokens[username] = r.json()["token"]
    return _tokens[username]


def _auth(username="admin", password="Admin@123"):
    return {"Authorization": f"Bearer {_login(username, password)}"}


def _events(order_no: str) -> list[str]:
    with database.SessionLocal() as db:
        return [e.event for e in db.query(PayEventLog).filter(PayEventLog.order_no == order_no)
                .order_by(PayEventLog.id).all()]


def _cancelled_events(order_no: str) -> list[PayEventLog]:
    with database.SessionLocal() as db:
        return (db.query(PayEventLog)
                  .filter(PayEventLog.order_no == order_no,
                          PayEventLog.event == "order_cancelled")
                  .order_by(PayEventLog.id).all())


def _wipe_pay(order_no: str) -> None:
    """删除本测试造的支付会话/尝试记录/事件流/订单快照，并清进程内探针节流。"""
    with database.SessionLocal() as db:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).first()
        if sess:
            db.query(PayAttempt).filter(PayAttempt.pay_session_id == sess.id).delete()
            db.delete(sess)
        db.query(PayEventLog).filter(PayEventLog.order_no == order_no).delete()
        db.query(OrderRecord).filter(OrderRecord.order_no == order_no).delete()
        db.commit()
    payportal_router._last_probe.pop(order_no, None)


@contextlib.contextmanager
def _internal_secret(path_value: str):
    """把内部广播密钥源重定向到临时文件（绝不触真实 data/internal_broadcast.secret）。

    load_internal_secret 有模块级缓存 _secret_cache：进入时清缓存 + 换路径，
    结束时恢复原值并再清缓存（下个读者按原路径重读）。"""
    orig_path = pay_broadcast._SECRET_PATH
    orig_cache = pay_broadcast._secret_cache
    pay_broadcast._SECRET_PATH = path_value
    pay_broadcast._secret_cache = None
    try:
        yield
    finally:
        pay_broadcast._SECRET_PATH = orig_path
        pay_broadcast._secret_cache = orig_cache


# ---------- 4. 测试用例 ----------

def test_01_info_status_timestamp_fields():
    """info/status：server_time ≈ now±5s、pay_deadline_ts 与库内 pay_deadline 同锚（±1s）；
    expire_at 为钳制后文本——expire_in=1800 的支付宝原文被钳到 ≤ now+10min。"""
    o1, o2 = "TS-INFO-1", "TS-CLAMP-1"
    try:
        with database.SessionLocal() as db:
            s1 = ps.ensure_pay_session(db, ACC_ID, o1, _link(o1, expire_in=590))
            tok1 = s1.pay_token
            deadline_ms_1 = int(s1.pay_deadline.timestamp() * 1000)
            # 钳制证明：OrderRecord 缺席 → 锚 now；支付宝原文 now+1800s 必被钳到 now+600s
            s2 = ps.ensure_pay_session(db, ACC_ID, o2, _link(o2, expire_in=1800))
            tok2 = s2.pay_token
            assert s2.pay_deadline is not None
            dl2 = s2.pay_deadline.timestamp()
            assert time.time() + 590 <= dl2 <= time.time() + 615, \
                f"expire_in=1800 应钳制到 10min 窗: {dl2 - time.time():.0f}s"
        for tok, deadline_ms, path in ((tok1, deadline_ms_1, "info"), (tok1, deadline_ms_1, "status")):
            r = CLIENT.get(f"/pay/{tok}/{path}")
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"
            d = r.json()
            assert "server_time" in d and "pay_deadline_ts" in d, f"{path} 缺时间戳字段: {sorted(d)}"
            now_ms = time.time() * 1000
            assert abs(d["server_time"] - now_ms) <= 5000, \
                f"{path} server_time 偏差过大: {d['server_time']} vs {now_ms:.0f}"
            assert abs(d["pay_deadline_ts"] - deadline_ms) <= 1000, \
                f"{path} pay_deadline_ts 与库内锚点不一致: {d['pay_deadline_ts']} vs {deadline_ms}"
        # info 的 expire_at 是钳制后 pay_deadline 的本地文本（非支付宝原文）
        r = CLIENT.get(f"/pay/{tok2}/info")
        assert r.status_code == 200, r.text
        d = r.json()
        from datetime import datetime
        expire_dt = datetime.strptime(d["expire_at"], "%Y-%m-%d %H:%M:%S")
        assert expire_dt.timestamp() <= time.time() + 615, \
            f"expire_at 未被钳制: {d['expire_at']}"
        assert d["remaining_seconds"] <= 600, d["remaining_seconds"]
        assert d["pay_deadline_ts"] <= (time.time() + 615) * 1000
    finally:
        _reset_fake()
        _wipe_pay(o1)
        _wipe_pay(o2)


def test_02_order_cancel_cascade():
    """管理端取消：PaySession cancelled + order_cancelled{source: manual-cancel} +
    OrderRecord=7；重复取消幂等（CAS 失败不产生第二条事件）。"""
    order = "TS-CANCEL-1"
    url = f"/api/ops/accounts/{ACC_ID}/orders/{order}/cancel"
    try:
        with database.SessionLocal() as db:
            db.add(OrderRecord(account_id=ACC_ID, order_no=order, scenario="partial",
                               status=1, status_label="待支付",
                               total_amount="20.00", pay_amount="10.00"))
            db.commit()
            tok = ps.ensure_pay_session(db, ACC_ID, order, _link(order)).pay_token
        r = CLIENT.post(url, headers=_auth())
        assert r.status_code == 200, f"取消失败: {r.status_code} {r.text}"
        with database.SessionLocal() as db:
            sess = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert sess.status == "cancelled", sess.status
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == order).one()
            assert rec.status == 7 and rec.status_label == "已取消"
            evs = _cancelled_events(order)
            assert len(evs) == 1, _events(order)
            assert evs[0].payload.get("source") == "manual-cancel", evs[0].payload
            assert evs[0].payload.get("operator") == "admin", evs[0].payload
            assert evs[0].pay_token_prefix == tok[:8]
        # 幂等：FakeClient 再次回放取消成功 → CAS 失败 → 不追加第二条 order_cancelled
        r = CLIENT.post(url, headers=_auth())
        assert r.status_code == 200, r.text
        assert len(_cancelled_events(order)) == 1, "重复取消不应追加 order_cancelled 事件"
    finally:
        _wipe_pay(order)


def test_03_reconcile_st7_cascade():
    """超时校准 st==7：直调 reconcile_order → PaySession cancelled +
    order_cancelled{source: reconcile}（事件数与状态迁移一对一）。"""
    order = "TS-RECON-1"
    try:
        with database.SessionLocal() as db:
            db.add(OrderRecord(account_id=ACC_ID, order_no=order, scenario="partial",
                               status=1, status_label="待支付",
                               total_amount="20.00", pay_amount="10.00"))
            db.commit()
            tok = ps.ensure_pay_session(db, ACC_ID, order, _link(order)).pay_token
        _reset_fake(status=7)                     # 茶姬侧返回已取消
        with database.SessionLocal() as db:
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == order).one()
            account = db.get(ChageeAccount, ACC_ID)
            result = reconcile_module.reconcile_order(db, rec, account, operator="system")
            assert result == "cancelled_rolled_back", result
        with database.SessionLocal() as db:
            sess = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert sess.status == "cancelled", sess.status
            evs = _cancelled_events(order)
            assert len(evs) == 1, _events(order)
            assert evs[0].payload.get("source") == "reconcile", evs[0].payload
            assert evs[0].pay_token_prefix == tok[:8]
    finally:
        _reset_fake()
        _wipe_pay(order)


def test_04_mark_session_notifies_broadcast():
    """mark_session CAS 成功 → notify_session_change(order_no, status)；CAS 失败不重复通知。"""
    order = "TS-BCAST-1"
    calls: list[tuple[str, str]] = []
    orig = pay_broadcast.notify_session_change
    pay_broadcast.notify_session_change = lambda o, s: calls.append((o, s))
    try:
        with database.SessionLocal() as db:
            ps.ensure_pay_session(db, ACC_ID, order, _link(order))
            assert ps.mark_session(db, order, "paid") is True
            assert (order, "paid") in calls, calls
            # CAS 失败（已 paid 再 paid）：不重复通知
            assert ps.mark_session(db, order, "paid") is False
            assert calls.count((order, "paid")) == 1, calls
            # 状态真实迁移（paid→cancelled）再次通知——每次 CAS 赢家通知一次
            assert ps.mark_session(db, order, "cancelled") is True
            assert calls == [(order, "paid"), (order, "cancelled")], calls
    finally:
        pay_broadcast.notify_session_change = orig
        _wipe_pay(order)


def test_05_sse_sync_first_frame():
    """SSE：/pay/{token}/events → text/event-stream；首帧 event: sync 且 data 含
    server_time/pay_deadline_ts（与库内锚点一致）。

    双路验证：
      (a) issued 会话（无限流）：直接以 ASGI 协议驱动主 app，收到首个 body 分块即模拟
          客户端断开（本环境 TestClient 传输层会把响应体完整缓冲后再返回——无限 SSE
          流用 client.stream 必然挂死，进程内 ASGI 驱动是等价的首帧读取方式）；
      (b) cancelled 会话（有限流：sync+终态帧后路由主动闭流）：标准 client.stream 全量
          读取，同时覆盖「连上即终态 → 补推终态事件后关闭流」契约。
    两路均有 10s 看门狗防挂死。"""
    import asyncio

    order = "TS-SSE-1"
    result: dict = {}

    def _asgi_first_frame(tok: str) -> dict:
        """驱动主 app 读 SSE 首帧：收集 http.response.start 头 + 首个 body 分块（含
        完整 sync 帧）后以 CancelledError 模拟客户端断开；整体 8s 兜底超时。"""
        out: dict = {}
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": f"/pay/{tok}/events", "raw_path": f"/pay/{tok}/events".encode(),
            "query_string": b"", "root_path": "",
            "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")],
            "client": ("testclient", 50000), "server": ("testserver", 80),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                out["status"] = message["status"]
                out["headers"] = {k.decode().lower(): v.decode()
                                  for k, v in message.get("headers", [])}
            elif message["type"] == "http.response.body":
                out["body"] = out.get("body", b"") + message.get("body", b"")
                if b"data:" in out["body"] and b"\n\n" in out["body"]:
                    raise asyncio.CancelledError        # 首帧收完：模拟客户端断开

        async def run():
            await app_module.app(scope, receive, send)

        try:
            asyncio.run(asyncio.wait_for(run(), 8.0))
        except (asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
            pass                                        # 预期路径（主动断开/兜底超时）
        return out

    def _terminal_stream_full(tok: str) -> dict:
        """cancelled 会话（有限流）经标准 TestClient.stream 全量读取。"""
        out: dict = {}
        try:
            with CLIENT.stream("GET", f"/pay/{tok}/events") as r:
                out["status"] = r.status_code
                out["content_type"] = r.headers.get("content-type", "")
                out["lines"] = [ln for ln in r.iter_lines()]
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    def _guarded(fn, *args) -> dict:
        box: dict = {}

        def _run():
            try:
                box.update(fn(*args))
            except Exception as e:  # noqa: BLE001
                box["error"] = f"{type(e).__name__}: {e}"

        worker = threading.Thread(target=_run, daemon=True, name="ts-sse-stream")
        worker.start()
        worker.join(10.0)
        if worker.is_alive():
            box["error"] = "SSE 流读取超时（10s 看门狗触发）"
        return box

    try:
        with database.SessionLocal() as db:
            sess = ps.ensure_pay_session(db, ACC_ID, order, _link(order, expire_in=590))
            tok = sess.pay_token
            assert ps.mark_session(db, order, "cancelled") is True   # (b) 用：终态连入
        # (a) issued 会话换成第二单（无限流首帧）
        order2 = "TS-SSE-2"
        try:
            with database.SessionLocal() as db:
                sess2 = ps.ensure_pay_session(db, ACC_ID, order2, _link(order2, expire_in=590))
                tok2 = sess2.pay_token
                deadline_ms2 = int(sess2.pay_deadline.timestamp() * 1000)
            first = _guarded(_asgi_first_frame, tok2)
            assert "error" not in first, first.get("error")
            assert first.get("status") == 200, first
            assert first["headers"].get("content-type", "").startswith("text/event-stream"), \
                first["headers"]
            assert first["headers"].get("cache-control") == "no-cache", first["headers"]
            frame = first["body"].decode()
            assert "event: sync" in frame, frame
            data = json.loads(frame.split("data:", 1)[1].strip())
            now_ms = time.time() * 1000
            assert abs(data["server_time"] - now_ms) <= 5000, data
            assert abs(data["pay_deadline_ts"] - deadline_ms2) <= 1000, data
            assert data["status"] == "issued" and data["remaining_seconds"] > 0, data
        finally:
            _wipe_pay(order2)
        # (b) cancelled 会话：sync 帧 + cancelled 终态帧 + 路由闭流（流自然结束可全量读）
        full = _guarded(_terminal_stream_full, tok)
        assert "error" not in full, full.get("error")
        assert full.get("status") == 200, full
        assert full["content_type"].startswith("text/event-stream"), full["content_type"]
        events = [ln.strip() for ln in full["lines"] if ln.startswith("event:")]
        assert events == ["event: sync", "event: cancelled"], events   # 终态推完即闭流
        data_lines = [ln for ln in full["lines"] if ln.startswith("data:")]
        d0 = json.loads(data_lines[0].strip()[len("data:"):])
        assert abs(d0["server_time"] - time.time() * 1000) <= 5000, d0
        assert d0["status"] == "cancelled" and d0["pay_deadline_ts"] is not None, d0
    finally:
        _wipe_pay(order)


def test_06_internal_broadcast_auth():
    """/internal/broadcast：无 header 401 / 错 token 401 / 对 token+不存在单 404 /
    对 token+存在会话 200 不抛（secret 走临时文件，不触真实 data/）。"""
    order = "TS-IB-1"
    url = "/internal/broadcast"
    tmpdir = tempfile.mkdtemp(prefix="ts_ibsecret_")
    secret_path = os.path.join(tmpdir, "internal_broadcast.secret")
    with open(secret_path, "w", encoding="utf-8") as f:
        f.write("ts-offline-secret-01")
    try:
        with database.SessionLocal() as db:
            tok = ps.ensure_pay_session(db, ACC_ID, order, _link(order)).pay_token
        with _internal_secret(secret_path):
            body = {"order_no": order, "status": "cancelled"}
            r = PP_CLIENT.post(url, json=body)                              # 无 header
            assert r.status_code == 401, f"无 token 应 401: {r.status_code} {r.text}"
            r = PP_CLIENT.post(url, json=body, headers={"X-Internal-Token": "wrong"})
            assert r.status_code == 401, f"错 token 应 401: {r.status_code} {r.text}"
            r = PP_CLIENT.post(url, json={"order_no": "NO-SUCH-ORDER", "status": "paid"},
                               headers={"X-Internal-Token": "ts-offline-secret-01"})
            assert r.status_code == 404, f"不存在会话应 404: {r.status_code} {r.text}"
            # 正确 token + 存在的 issued 会话 → 200（以库内最新态分派 sync 事件）
            r = PP_CLIENT.post(url, json=body,
                               headers={"X-Internal-Token": "ts-offline-secret-01"})
            assert r.status_code == 200, f"合法通知应 200: {r.status_code} {r.text}"
            d = r.json()
            assert d["ok"] is True and d["order_no"] == order and d["event"] == "sync", d
        with database.SessionLocal() as db:                                  # 通知不改库态
            sess = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert sess.status == "issued", "broadcast 是加速信号，不得改库态"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _wipe_pay(order)


def test_07_remint_conflict_after_cancel():
    """remint 409 不回归：会话 cancelled 后 POST /pay/{token}/remint → 409。"""
    order = "TS-REMT-1"
    try:
        with database.SessionLocal() as db:
            tok = ps.ensure_pay_session(db, ACC_ID, order, _link(order)).pay_token
            assert ps.mark_session(db, order, "cancelled") is True
        calls0 = len(CALLS)
        r = CLIENT.post(f"/pay/{tok}/remint")
        assert r.status_code == 409, f"cancelled 会话 remint 应 409: {r.status_code} {r.text}"
        assert "不可续付" in r.json()["detail"]
        assert len(CALLS) == calls0                # 拒绝在协议调用之前，不触达远端
    finally:
        _wipe_pay(order)


def test_08_no_real_network_calls():
    """零网络保障：全程仅触达回放覆盖的协议端点（未知 path 已由 FakeClient AssertionError 拦截）。"""
    allowed_suffixes = ("/order/getOrderStatus", "/order/cancelOrder",
                        "/customer/userInfo/query")
    bad = [p for p in CALLS if not p.endswith(allowed_suffixes)]
    assert not bad, f"出现了未经回放覆盖的协议调用: {bad}"
    assert CALLS, "本套件应至少发生过一次协议层回放调用（/status 探针或取消）"


# ---------- 5. 运行器 ----------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"[PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n结果: {passed} passed, {failed} failed (共 {len(tests)} 项)")
    # 清理测试库（Windows 下先释放连接池）
    database.engine.dispose()
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = database.DB_PATH + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
