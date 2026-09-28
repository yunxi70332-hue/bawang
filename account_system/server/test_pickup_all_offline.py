"""全量取餐码扫描 / 多维模糊搜索 / SSE 事件流 / 仪表盘指纹推送 —— 离线测试（零真实网络请求）。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server && python -m pytest test_pickup_all_offline.py -q

覆盖：
  - POST /api/ops/pickup/scan-all：后台线程遍历所有账号 token、批量翻页拉单落库
    （含 order_time/biz_type/goods_desc 回填）、取餐码收集、单账号凭证失效不中断并回写 expired
  - GET  /api/ops/pickup/search：取餐码/订单号/饮品/门店/履约方式/账号（备注昵称手机号）
    多维 LIKE 模糊匹配 + 状态/账号/仅看有码/场景过滤 + 分页与命中统计
  - GET  /api/events：query-token 鉴权（401/400/403）+ SSE 帧格式 + dashboard 连上即推缓存 stats
  - services/dashboard_push：指纹比对（剔除 generated_at，数据变化才推）
"""

import json
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# 禁用校准线程：避免测试进程残留后台副作用
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
# 禁用支付 watcher（app startup 挂载）：避免后台线程探针测试库里的支付会话干扰断言
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
# 禁用 frida 收银台铸造（pay_link_payload_with_session 会触发）：离线环境杜绝真触云手机
os.environ["CHAGEE_MINT_ENABLED"] = "0"
# 禁用跨进程状态广播（mark_session 收口会触发）：离线环境杜绝真发 HTTP 到 8010
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
# 禁用云手机隧道子进程（app startup 挂载）：离线环境杜绝真起 adb 子进程
os.environ["CHAGEE_TUNNEL_ENABLED"] = "0"
# 禁用仪表盘统计推送线程（本测试直接调用 push_once 断言指纹逻辑）
os.environ["CHAGEE_DASHBOARD_PUSH_INTERVAL_SECONDS"] = "0"
# 日志隔离：oplog 日志库与文本日志均指向测试路径，避免污染生产 data/logs/
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_pickup_all_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_pickup_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_pickup_all.db")
if os.path.exists(database.DB_PATH):
    os.remove(database.DB_PATH)
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

database.engine = create_engine(
    f"sqlite:///{database.DB_PATH}", connect_args={"check_same_thread": False}, pool_pre_ping=True)
database.SessionLocal = sessionmaker(bind=database.engine, autoflush=False,
                                     autocommit=False, expire_on_commit=False)

import seed  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from models import ChageeAccount, OrderRecord, Role, SystemUser  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402

with TestClient(app_module.app) as client:   # startup → init_db（含 order_time/biz_type 列迁移路径）
    pass

# ---------- 2. 假协议客户端（回放 getOrderList 分页数据） ----------

# 账号1：今日 2 单（制作中带码 / 待支付无码）+ 历史 1 单（已完成带码）；账号3：无单；账号4：凭证失效
ORDERS = {
    1: {
        "today": [
            {"orderNo": "O2026092801", "orderStatus": 3, "orderStatusText": "制作中",
             "pickupNo": "TA0001", "payAmount": "8", "totalAmount": "18",
             "storeNo": "CN001", "storeName": "测试门店一",
             "orderTime": "2026-09-28 10:00:00", "uniquePosOrderNo": "POS001",
             "businessTypeText": "自取",
             "orderItems": [{"skuName": "伯牙绝弦", "buyNum": 1}, {"skuName": "青青糯山", "buyNum": 2}]},
            {"orderNo": "O2026092802", "orderStatus": 1, "orderStatusText": "待支付",
             "pickupNo": "", "payAmount": "10", "totalAmount": "10",
             "storeNo": "CN001", "storeName": "测试门店一",
             "orderTime": "2026-09-28 11:00:00", "uniquePosOrderNo": "POS002",
             "businessTypeText": "外卖", "orderItems": [{"skuName": "桂花乌龙", "buyNum": 1}]},
        ],
        "history": [
            {"orderNo": "O2026092001", "orderStatus": 6, "orderStatusText": "已完成",
             "pickupNo": "T0101", "payAmount": "0", "totalAmount": "16",
             "storeNo": "CN002", "storeName": "历史门店二",
             "orderTime": "2026-09-20 09:00:00", "uniquePosOrderNo": "POS101",
             "businessTypeText": "自取", "orderItems": [{"skuName": "轻芝士葡萄", "buyNum": 1}]},
        ],
    },
    3: {"today": [], "history": []},
}


class FakeTradeApi:
    def __init__(self, account):
        self.account = account

    def order_list(self, tab, page, size):
        if self.account.id == 4:
            raise bridge.SessionExpiredError("12320120400401", "token 已失效")
        rows = ORDERS.get(self.account.id, {}).get(tab, [])
        start = (page - 1) * size
        return rows[start:start + size]


bridge.trade_api = lambda account: FakeTradeApi(account)   # monkeypatch（orders 模块经 bridge.trade_api 调用）

# ---------- 3. 测试数据与夹具 ----------

db = database.SessionLocal()
seed.init_db()

admin_role = db.query(Role).filter(Role.name == "admin").one()
if not db.query(SystemUser).filter(SystemUser.username == "op").first():
    db.add(SystemUser(username="op", password_hash=hash_password("Op@123"),
                      display_name="运营", role_id=admin_role.id))
# 非内置的空权限角色用户（SSE topic 权限校验 403 用例；注意内置 viewer 角色自带 account:read，
# 名字须避开 BUILTIN_ROLES：admin/operator/viewer）
if not db.query(Role).filter(Role.name == "blind").first():
    db.add(Role(name="blind", description="无任何权限（SSE 403 用例）", permissions=[]))
db.commit()
blind_role = db.query(Role).filter(Role.name == "blind").one()
if not db.query(SystemUser).filter(SystemUser.username == "blind").first():
    db.add(SystemUser(username="blind", password_hash=hash_password("Blind@123"),
                      display_name="无权限", role_id=blind_role.id))

# 账号1/3/4 在线有 token；账号2 无 token（应被跳过）；账号5 停用（应被跳过）
for aid, status, token in ((1, "online", "t1"), (2, "pending", ""), (3, "online", "t3"),
                           (4, "online", "t4"), (5, "disabled", "t5")):
    if not db.get(ChageeAccount, aid):
        db.add(ChageeAccount(id=aid, label=f"测试账号{aid}", phone=f"1380000000{aid}",
                             device_uuid=f"uuid-{aid}", token=token, status=status,
                             nickname=f"昵称{aid}"))
db.commit()
db.close()

client = TestClient(app_module.app)
TOK = client.post("/api/auth/login", json={"username": "op", "password": "Op@123"}).json()["token"]
BLIND_TOK = client.post("/api/auth/login",
                        json={"username": "blind", "password": "Blind@123"}).json()["token"]
H = {"Authorization": f"Bearer {TOK}"}


def fresh_db():
    return database.SessionLocal()


def wait_scan_done(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/ops/pickup/scan-status", headers=H).json()
        if not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("扫描线程超时未完成")


# ---------- 4. 用例 ----------


def test_scan_all_collects_pickup_codes():
    r = client.post("/api/ops/pickup/scan-all", headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["started"] is True
    assert d["accounts"] == 3   # 只遍历 有token且未停用：1/3/4（2 无 token、5 disabled 跳过）

    s = wait_scan_done()
    summary = s["last"]["summary"]
    assert summary["scanned"] == 3
    assert summary["ok"] == 2 and summary["expired"] == 1 and summary["failed"] == 0
    assert summary["orders"] == 3                 # 账号1 今日2 + 历史1（账号3 无单、账号4 失效）
    assert summary["distinct_pickup_codes"] == 2  # TA0001 + T0101

    # 落库断言：新增维度字段回填（order_time / biz_type / goods_desc / quantity）
    db = fresh_db()
    rec = db.query(OrderRecord).filter(OrderRecord.order_no == "O2026092801").one()
    assert rec.pickup_no == "TA0001"
    assert rec.order_time == "2026-09-28 10:00:00"
    assert rec.biz_type == "自取"
    assert rec.goods_desc == "伯牙绝弦、青青糯山 x2"
    assert rec.quantity == 3 and rec.status == 3
    rec2 = db.query(OrderRecord).filter(OrderRecord.order_no == "O2026092802").one()
    assert rec2.pickup_no == "" and rec2.biz_type == "外卖"   # 待支付无码
    # 失效账号状态已回写
    assert db.get(ChageeAccount, 4).status == "expired"
    db.close()

    # scan-status：空闲态 + 逐账号明细
    assert any(a["result"] == "expired" for a in s["last"]["accounts"])


def test_scan_all_no_available_accounts():
    db = fresh_db()
    for a in db.query(ChageeAccount).all():
        a.status = "disabled"
    db.commit()
    db.close()
    r = client.post("/api/ops/pickup/scan-all", headers=H)
    assert r.status_code == 400
    assert "没有可查询的账号" in r.json()["detail"]
    # 恢复账号状态供后续用例
    db = fresh_db()
    db.get(ChageeAccount, 1).status = "online"
    db.get(ChageeAccount, 3).status = "online"
    db.get(ChageeAccount, 4).status = "expired"   # 已被扫描置失效，保持
    db.commit()
    db.close()


def test_pickup_search_multi_dimension():
    # 码值维度
    d = client.get("/api/ops/pickup/search", headers=H, params={"keyword": "TA0001"}).json()
    assert [i["order_no"] for i in d["items"]] == ["O2026092801"]
    assert d["items"][0]["account"]["label"] == "测试账号1"
    assert d["items"][0]["account"]["phone_masked"] == "13800000001"
    # 饮品（名称）维度
    d = client.get("/api/ops/pickup/search", headers=H, params={"keyword": "伯牙绝弦"}).json()
    assert {i["order_no"] for i in d["items"]} == {"O2026092801"}
    # 门店维度
    d = client.get("/api/ops/pickup/search", headers=H, params={"keyword": "测试门店"}).json()
    assert {i["order_no"] for i in d["items"]} == {"O2026092801", "O2026092802"}
    # 履约方式（使用范围）维度
    d = client.get("/api/ops/pickup/search", headers=H, params={"keyword": "自取"}).json()
    assert {i["order_no"] for i in d["items"]} == {"O2026092801", "O2026092001"}
    # 归属账号名维度
    d = client.get("/api/ops/pickup/search", headers=H, params={"keyword": "测试账号1"}).json()
    assert d["total"] == 3
    # 手机号维度（13800000003 无单 → 0；13800000001 → 3 单）
    assert client.get("/api/ops/pickup/search", headers=H,
                      params={"keyword": "13800000001"}).json()["total"] == 3
    assert client.get("/api/ops/pickup/search", headers=H,
                      params={"keyword": "13800000003"}).json()["total"] == 0


def test_pickup_search_filters_and_stats():
    # 基础统计（无 status/has_pickup 过滤时与全集一致）
    d = client.get("/api/ops/pickup/search", headers=H).json()
    assert d["total"] == 3
    assert d["stats"]["by_status"] == {"3": 1, "1": 1, "6": 1}
    assert d["stats"]["with_pickup"] == 2
    # 状态过滤
    d = client.get("/api/ops/pickup/search", headers=H, params={"status": 3}).json()
    assert d["total"] == 1 and d["items"][0]["pickup_no"] == "TA0001"
    # 仅看有码
    d = client.get("/api/ops/pickup/search", headers=H, params={"has_pickup": "true"}).json()
    assert d["total"] == 2 and all(i["pickup_no"] for i in d["items"])
    # 账号过滤（账号3 无单）
    d = client.get("/api/ops/pickup/search", headers=H, params={"account_id": 3}).json()
    assert d["total"] == 0
    # 分页
    d = client.get("/api/ops/pickup/search", headers=H,
                   params={"page": 1, "page_size": 2}).json()
    assert d["total"] == 3 and len(d["items"]) == 2


def test_pickup_search_scenario_filter():
    db = fresh_db()
    db.add(OrderRecord(account_id=1, order_no="OZERO01", status=6, scenario="zero",
                       pickup_no="TZ01", store_name="零元门店", goods_desc="零元测试饮品"))
    db.commit()
    db.close()
    d = client.get("/api/ops/pickup/search", headers=H, params={"scenario": "zero"}).json()
    assert d["total"] == 1 and d["items"][0]["order_no"] == "OZERO01"


def test_events_auth():
    # 无 token → 401；无效 topic → 400；无权限 → 403（均为 JSON 短响应，流式 200 才会挂 TestClient）
    assert client.get("/api/events", params={"topics": "dashboard"}).status_code == 401
    r = client.get("/api/events", params={"topics": "nope", "token": TOK})
    assert r.status_code == 400
    r = client.get("/api/events", params={"topics": "dashboard", "token": BLIND_TOK})
    assert r.status_code == 403
    assert "account:read" in r.json()["detail"]


def test_events_stream_frames():
    """帧格式 + dashboard 连上即推缓存 stats + pickup_scan 事件实时投递。
    注：starlette 1.7 TestClient 不支持流式响应（等 app 跑完才返回，无限流会挂起），
    故直接驱动端点返回的 StreamingResponse；body_iterator 为 async 生成器（sync 源被
    iterate_in_threadpool 包装），用单一事件循环拉帧。真实 HTTP 链路由在线验证覆盖。"""
    import anyio
    from services import dashboard_push, events_bus
    from routers.events import events_stream as sse_endpoint

    dashboard_push.push_once()   # 先造一份缓存 stats（首推）

    db = fresh_db()
    resp = sse_endpoint(topics="dashboard,pickup_scan", token=TOK, db=db)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"
    gen = resp.body_iterator

    async def pull_two_frames():
        # 第一帧：dashboard 订阅连上即推缓存 stats
        first = await gen.__anext__()
        # 第二帧：0.2s 后的事件实时投递（此刻生成器正阻塞在队列 get 上）
        threading.Timer(0.2, lambda: events_bus.publish(
            "pickup_scan", "account_done", {"index": 1, "total": 2})).start()
        second = await gen.__anext__()
        return first, second

    first, frame = anyio.run(pull_two_frames)
    assert first.startswith("event: stats\ndata: {")
    payload = json.loads(first.split("\ndata: ", 1)[1])
    assert "generated_at" in payload and "cards" in payload
    assert frame.startswith("event: account_done\ndata: ")
    assert json.loads(frame.split("\ndata: ", 1)[1]) == {"index": 1, "total": 2}
    # 断开注销：生成器 close（模拟连接断开）后再 publish 幂等无错、无泄漏投递
    anyio.run(gen.aclose)
    events_bus.publish("pickup_scan", "account_done", {"index": 2, "total": 2})
    db.close()


def test_events_bus_frame_and_backpressure():
    from services import events_bus

    # 独立 topic：不与端点注册的 topic 混用，隔离其他用例可能残留的订阅者
    q = events_bus.subscribe(["unit_test_bus"])
    assert events_bus.publish("unit_test_bus", "account_done", {"index": 1}) == 1
    assert q.get_nowait() == 'event: account_done\ndata: {"index": 1}\n\n'
    events_bus.unsubscribe(q, ["unit_test_bus"])
    assert events_bus.publish("unit_test_bus", "account_done", {"index": 2}) == 0   # 无人订阅
    # 慢消费者：队列满丢最旧保最新，不阻塞发布方
    q2 = events_bus.subscribe(["unit_test_bus"])
    for i in range(events_bus._QUEUE_MAX + 10):
        events_bus.publish("unit_test_bus", "tick", {"i": i})
    assert q2.qsize() == events_bus._QUEUE_MAX
    assert json.loads(q2.get_nowait().split("\ndata: ", 1)[1])["i"] == 10   # 最旧 10 帧被丢弃
    events_bus.unsubscribe(q2, ["unit_test_bus"])


def test_dashboard_push_fingerprint():
    from services import dashboard_push

    # 无变化 → 不推（generated_at 被剔除出指纹）
    assert dashboard_push.push_once() is False
    # 数据变化（新增一单）→ 再推
    db = fresh_db()
    db.add(OrderRecord(account_id=1, order_no="O2026092899", status=6, pickup_no="T999"))
    db.commit()
    db.close()
    assert dashboard_push.push_once() is True
    assert dashboard_push.push_once() is False
    latest = dashboard_push.latest_stats()
    assert latest is not None and latest["cards"]["orders_total"] >= 1
