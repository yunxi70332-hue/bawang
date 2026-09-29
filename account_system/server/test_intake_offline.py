"""异步订单中枢离线测试（接收接口 + worker 全自动执行链，wire 回放零真实网络）。

覆盖：
  接收层：内部 202 / 幂等 duplicate / 422 参数与规格歧义 / 外部 v1 X-Api-Key 401+202+适配 /
          限流 429 / 队列统计 / 取消 / 死信重放
  worker：全自动零元单链（decide→settle→create→completed+取餐码）/ 决策 blocked→failed+死信 /
          差额单→awaiting_payment+pay_url / pay-watcher 收口挂钩→completed
  回调：HMAC 签名投递到本地 dummy HTTP server（127.0.0.1，无外网）

运行：pytest test_intake_offline.py -q（mock 手法与 test_orders_offline.py 同源）
"""

import hashlib
import hmac
import json
import os
import sys
import threading
import urllib.parse
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
sys.path.insert(0, BASE)

# ---- 离线开关（同 test_orders_offline；worker/backup 线程由 conftest 兜底置 0） ----
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MENU_REFRESH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MINT_ENABLED"] = "0"
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
os.environ["CHAGEE_INTAKE_RATE_LIMIT"] = "8"
os.environ["CHAGEE_TUNNEL_ENABLED"] = "0"
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_intake_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_intake.db")
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
from models import (ChageeAccount, CouponRecord, CustomerOrder, MenuGoodsCache,  # noqa: E402
                    OrderMessage, Role, SystemUser)
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import decision as decision_svc  # noqa: E402
from services import intake_notify, order_worker  # noqa: E402

# 决策配置指到测试文件（min_profit 1.00：customer_price 22 - 券成本 20 = 利润 2 过阈）
_DEC_CFG = os.path.join(database.DATA_DIR, "test_intake_decision_config.json")
with open(_DEC_CFG, "w", encoding="utf-8") as f:
    json.dump({"min_profit": "1.00", "min_margin": "", "max_order_cost": "",
               "overhead": "0", "cost_fallback_ratio": "1.0"}, f)
decision_svc._CONFIG_PATH = _DEC_CFG

# 回调签名密钥固定（不依赖生产 secret.key / pay_callback_config.json）
_CB_SECRET = "intake-test-secret"
_cb_cfg = os.path.join(database.DATA_DIR, "test_intake_callback_config.json")
with open(_cb_cfg, "w", encoding="utf-8") as f:
    json.dump({"secret": _CB_SECRET}, f)
intake_notify._CALLBACK_CONFIG_PATH = _cb_cfg

# ---- wire 回放假客户端（与 test_orders_offline 同源结构） ----


def _load(name):
    with open(os.path.join(ROOT, "output", name), encoding="utf-8") as f:
        return json.load(f)


TRADE = _load("trade_samples_20260926.json")
ZERO = _load("pay_zero_capture_20260926.json")
FIXTURE = {"mode": "default", "order_seq": 99}


def _resp(item):
    body = item["resp_body"]
    return json.loads(body) if isinstance(body, str) else body


RATE_COUPON_CODE = "CRATE7Z"
RATE_ORDER_NO = "202609280910119999000000099"
RATE_COUPON = {"couponCode": RATE_COUPON_CODE, "templateName": "全品类单杯7折券-离线",
               "benefitText": "7折", "benefit2Text": "", "thresholdTips": "优惠1杯",
               "useEndTime": 4102444800000, "canDiscount": True}
RATE_SETTLE_RESP = {"errcode": "0", "data": {
    "confirmOrderKey": "RATE-KEY-1",
    "tradeFundInfo": {"totalTradePrice": "22", "buyerRealPrice": "15.4",
                      "totalDiscountAmount": "6.6", "totalCouponDiscountAmount": "6.6"},
    "assetInfo": {"userCouponInfo": {"availableCouponList": [RATE_COUPON]}},
    "orderGroupList": [{"tradeFundInfo": {"buyerRealPrice": "15.4"}}],
    "discountList": [{"discountId": RATE_COUPON_CODE, "discountName": "全品类单杯7折券-离线",
                      "discountSource": 1, "discountType": 1, "scopeType": 2,
                      "discountAmount": "6.6", "currentSelect": None}]}}
RATE_CREATE_RESP = {"errcode": "0", "data": {
    "orderNo": RATE_ORDER_NO, "payNo": "CHP20260928RATE000000000000",
    "payUrl": json.dumps({"requestJson": {"orderStr":
        "out_trade_no=331LRATE0001&total_amount=15.40&biz_content=" + urllib.parse.quote(
            json.dumps({"out_trade_no": "331LRATE0001", "total_amount": "15.40",
                        "time_expire": "2026-09-28 23:00:00"}))}})}}


class FakeClient:
    """按 path 后缀回放 2026-09-26 生产 wire；rate 模式回放 7折券差额单夹具。"""

    def __init__(self, account):
        self.account = account

    def get(self, path, **kw):
        if path.endswith("/customer/userInfo/query"):
            return {"errcode": "0", "data": {"customerId": "1190018250",
                                             "mobileEncrypt": "AESxFAKE", "nickName": "离线测试"}}
        raise AssertionError(f"未预期的 GET 协议调用: {path}")

    def whoami(self):
        return self.get("/user-client/customer/userInfo/query")

    def post(self, path, body=None, **kw):
        if path.endswith("/goods/sku/calculatePrice"):
            price = "22.00" if FIXTURE["mode"] == "rate" else "20.00"
            return {"errcode": "0", "data": {
                "spuId": "625339451983278080", "spuType": "stand", "skuId": TEST_SKU,
                "totalSalePrice": price, "totalTradePrice": price,
                "totalGoodsItemPrice": price, "totalGoodsItemDiscountAmount": "0.00",
                "totalGoodsPaymentDiscountAmount": "0.00", "totalDiscountAmount": "0.00",
                "totalWrappingPrice": "0.00"}}
        if path.endswith("/shoppingCart/change"):
            return _resp(TRADE[2])
        if path.endswith("/shoppingCart/get"):
            return _resp(TRADE[3])
        if path.endswith("/order/settlePrice"):
            if FIXTURE["mode"] == "rate":
                return RATE_SETTLE_RESP
            rows = (body or {}).get("discountList") or []
            if not rows:
                return _resp(TRADE[5])     # 无券自荐：20 元券 HYW，实付 0
            amt = Decimal(str(rows[0].get("discountAmount") or 0))
            return _resp(TRADE[5] if amt == 20 else TRADE[6])
        if path.endswith("/order/createOrder"):
            if FIXTURE["mode"] == "rate":
                FIXTURE["order_seq"] += 1   # 动态订单号：多单互不串号（挂钩按单号定位）
                no = f"202609280910119999{FIXTURE['order_seq']:010d}"
                return {"errcode": "0", "data": {
                    "orderNo": no, "payNo": f"CHP20260928RATE{FIXTURE['order_seq']:012d}",
                    "payUrl": RATE_CREATE_RESP["data"]["payUrl"].replace(
                        "20260928RATE000000000000", f"20260928RATE{FIXTURE['order_seq']:012d}")}}
            return _resp(ZERO[0])
        if path.endswith("/order/getOrderDetail"):
            return _resp(ZERO[1])
        if path.endswith("/order/getOrderList"):
            return {"errcode": "0", "data": {"orderList": [_resp(ZERO[1])["data"]]}}
        if path.endswith("/order/getOrderStatus"):
            return {"errcode": "0", "data": 3}
        raise AssertionError(f"未预期的 POST 协议调用: {path}")


bridge.build_client = lambda account: FakeClient(account)

# ---- 测试数据 ----

ORDER_NO = "202609260910110023151918250"     # 零元 wire 样本订单号
COUPON_HYW = "1309482592713252864"           # 20 元代金券（样本5 服务端自荐）
TEST_SKU = "653632618000097282"
TEST_SPU = "625339451983278080"
STORE_NO = "CN03324"

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800002222").first():
        db.add(ChageeAccount(label="中枢冒烟", phone="13800002222", device_uuid="uuid-offline-intake-1",
                             token="fake.token.intake", sk="fakesk", customer_id="1190018250",
                             status="online", group="默认"))
        db.commit()
    ACC_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800002222").one().id
    # 菜单规格库：resolve_by_sku 命中链（decide 与接收预检共用）
    if not db.query(MenuGoodsCache).filter_by(store_no=STORE_NO, spu_id=TEST_SPU).first():
        db.add(MenuGoodsCache(
            store_no=STORE_NO, spu_id=TEST_SPU, spu_name="伯牙绝弦-离线", spu_type="stand",
            status=1, sale_out=False, default_price="20",
            sku_index={TEST_SKU: {"price": "20.00", "stock": 999,
                                  "itemSkuId": "653632618000097282",
                                  "specs": [{"specId": "653599312273510400",
                                             "specOptionId": "653599312273510401",
                                             "specOptionName": "大杯"}]}},
            spec_groups=[{"groupId": "653599312273510400", "groupName": "杯型",
                          "options": [{"optionId": "653599312273510401", "optionName": "大杯",
                                       "defaulted": True, "sequence": 1},
                                      {"optionId": "653599312273510402", "optionName": "中杯",
                                       "defaulted": False, "sequence": 2}]}],
            attribute_groups=[{"groupId": "745317722624679942", "groupName": "温度",
                               "options": [{"attributeOptionId": "7453177226246799431",
                                            "optionName": "冰", "defaulted": True, "sequence": 1},
                                           {"attributeOptionId": "7453177226246799432",
                                            "optionName": "热", "defaulted": False, "sequence": 2}]}],
            extra_groups=[], raw={}, fetched_at=__import__("datetime").datetime.now()))
        db.commit()
    # 券候选：HYW 20元（零元链）+ CRATE7Z 7折（差额链）
    for code, benefit, bucket in ((COUPON_HYW, "20元", "effective"),
                                  (RATE_COUPON_CODE, "7折", "effective")):
        if not db.query(CouponRecord).filter(CouponRecord.coupon_code == code).first():
            db.add(CouponRecord(coupon_code=code, account_id=ACC_ID, benefit_text=benefit,
                                template_name=f"离线测试券-{benefit}", bucket=bucket,
                                use_end_time=4102444800000, can_discount=True))
    db.commit()
    # 清理上一轮残留（幂等重跑）
    db.query(CustomerOrder).delete()
    db.query(OrderMessage).delete()
    db.commit()

CLIENT = TestClient(app_module.app)
_tokens: dict[str, str] = {}


def _login(username: str, password: str) -> str:
    if username not in _tokens:
        r = CLIENT.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败: {r.status_code} {r.text}"
        _tokens[username] = r.json()["token"]
    return _tokens[username]


def _auth():
    return {"Authorization": f"Bearer {_login('admin', 'Admin@123')}"}


def _submit(no: str, price: str = "22", specs: list | None = None, **over):
    body = {"customer_order_no": no, "store_no": STORE_NO, "sku_id": TEST_SKU,
            "quantity": 1, "spec_texts": specs or [], "customer_price": price}
    body.update(over)
    return CLIENT.post("/api/intake/orders", json=body, headers=_auth())


def _co(no: str) -> CustomerOrder:
    with database.SessionLocal() as db:
        row = db.query(CustomerOrder).filter_by(customer_order_no=no).first()
        assert row is not None, f"登记单 {no} 不存在"
        db.expunge(row)
        return row


def _msg_of(co_id: int) -> OrderMessage:
    with database.SessionLocal() as db:
        row = db.query(OrderMessage).filter_by(customer_order_id=co_id) \
            .order_by(OrderMessage.id.desc()).first()
        db.expunge(row)
        return row


# ---------------- 接收层 ----------------

def _drain(rounds: int = 12) -> list[str]:
    """排空队列到 idle（claim 全局 FIFO：先消费前序用例残留，再轮到本用例目标单）。"""
    outcomes = []
    for _ in range(rounds):
        out = order_worker.process_once("w-test")
        outcomes.append(out)
        if out == "idle":
            break
    return outcomes

def test_01_submit_ok_and_idempotent():
    r = _submit("INTAKE-001")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "enqueued" and body["duplicate"] is False
    assert body["query"].endswith("INTAKE-001")
    # 幂等：重复单号不重复入队
    r2 = _submit("INTAKE-001")
    assert r2.status_code == 202
    assert r2.json()["duplicate"] is True
    co = _co("INTAKE-001")
    assert _msg_of(co.id) is not None   # 仅一条消息（requeue 前）


def test_02_submit_validation_422():
    # 未知 sku：菜单库未命中（预检回源被 FakeClient 的 AssertionError 拦截成 422 链）
    r = _submit("INTAKE-BAD-SKU", sku_id="NOT-EXIST-SKU")
    assert r.status_code == 422
    # 规格文案无法识别：明确报错，不进队列
    r2 = _submit("INTAKE-BAD-SPEC", specs=["半糖"])
    assert r2.status_code == 422
    assert "无法识别" in json.dumps(r2.json(), ensure_ascii=False)
    # 金额非法：Pydantic 422
    r3 = _submit("INTAKE-BAD-PRICE", price="-3")
    assert r3.status_code == 422
    with database.SessionLocal() as db:
        assert db.query(CustomerOrder).filter(
            CustomerOrder.customer_order_no.like("INTAKE-BAD%")).count() == 0


def test_03_worker_zero_order_full_auto():
    """全自动零元链：decide 选号选券 → settle → create → completed + 取餐码。"""
    assert _submit("INTAKE-ZERO").status_code == 202
    outcomes = _drain()
    assert "done" in outcomes, f"期望 done，实得 {outcomes}"
    co = _co("INTAKE-ZERO")
    assert co.status == "completed"
    assert co.chagee_order_no == ORDER_NO
    assert co.pickup_no == "TA0001"          # wire getOrderDetail 的取餐码
    assert co.account_id == ACC_ID
    assert co.coupon_code == COUPON_HYW
    msg = _msg_of(co.id)
    assert msg.status == "done"
    # 消费幂等：消息已 done，重复 process_once 空转不重复下单
    assert order_worker.process_once("w-test") == "idle"


def test_04_worker_decision_blocked_to_dead():
    """决策拦截（customer_price 1 → 利润深度负）：failed + 死信（kill 不耗退避）。"""
    assert _submit("INTAKE-BLOCK", price="1").status_code == 202
    outcomes = _drain()
    assert "dead" in outcomes, outcomes
    co = _co("INTAKE-BLOCK")
    assert co.status == "failed" and "利润" in co.error or co.error  # blocked_reason 落 error
    assert _msg_of(co.id).status == "dead"
    # 死信管理接口：列表可见 + 重放回 pending（admin 有 intake:manage）
    r = CLIENT.get("/api/intake/queue/messages?status=dead", headers=_auth())
    assert r.status_code == 200
    dead_id = r.json()["items"][0]["id"]
    r2 = CLIENT.post(f"/api/intake/queue/messages/{dead_id}/requeue", headers=_auth())
    assert r2.status_code == 200
    assert _msg_of(co.id).status == "pending"
    assert _co("INTAKE-BLOCK").status == "enqueued"   # 终态同步回 enqueued


def test_05_worker_partial_and_pay_hook():
    """差额链：7折券 → awaiting_payment + pay_url；watcher 收口挂钩 → completed。"""
    FIXTURE["mode"] = "rate"
    try:
        assert _submit("INTAKE-PART", price="22").status_code == 202
        assert "done" in _drain()
        co = _co("INTAKE-PART")
        assert co.status == "awaiting_payment"
        assert co.chagee_order_no and co.chagee_order_no != ORDER_NO   # rate 动态单号
        assert co.pay_url.startswith("http") and "/pay/" in co.pay_url
        assert co.pay_amount == "15.40"        # link.total_amount = 实付差额（应付口径）
        # pay-watcher 收口挂钩（_handle_paid 内同款调用；用登记单上的动态单号）
        with database.SessionLocal() as db:
            intake_notify.on_chagee_order_paid(db, co.chagee_order_no, "P777")
        co2 = _co("INTAKE-PART")
        assert co2.status == "completed" and co2.pickup_no == "P777"
        # 取消挂钩（另一单走取消路径）
        assert _submit("INTAKE-PART2", price="22").status_code == 202
        assert "done" in _drain()
        assert _co("INTAKE-PART2").status == "awaiting_payment"
        with database.SessionLocal() as db:
            intake_notify.on_chagee_order_cancelled(db, _co("INTAKE-PART2").chagee_order_no)
        co3 = _co("INTAKE-PART2")
        assert co3.status == "failed" and "取消" in co3.error
    finally:
        FIXTURE["mode"] = "default"


def test_06_cancel_and_requeue():
    assert _submit("INTAKE-CXL").status_code == 202
    r = CLIENT.post("/api/intake/orders/INTAKE-CXL/cancel", headers=_auth())
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _msg_of(_co("INTAKE-CXL").id).status == "dead"   # 未消费消息一并作废
    # 取消时未消费消息已被作废（dead）：worker 无事可做
    assert _drain() == ["idle"]
    # 重放：failed/cancelled → 新消息 + enqueued
    r2 = CLIENT.post("/api/intake/orders/INTAKE-CXL/requeue", headers=_auth())
    assert r2.status_code == 200 and r2.json()["status"] == "enqueued"
    assert _msg_of(_co("INTAKE-CXL").id).status == "pending"


def test_07_external_v1_key_and_adaptation():
    """外部 KFC 系：401 无/错 key → 建密钥 → 202 适配（linkId/specs/storeNo 缺省）。"""
    body = {"orderNo": "KFC-001", "linkId": TEST_SKU, "count": 1,
            "specs": ["大杯", "热"], "payAmount": "22"}
    assert CLIENT.post("/api/intake/v1/orders", json=body).status_code == 401
    assert CLIENT.post("/api/intake/v1/orders", json=body,
                       headers={"X-Api-Key": "wrong-key"}).status_code == 401
    r = CLIENT.post("/api/intake/keys", json={"label": "离线平台", "source": "kfc"},
                    headers=_auth())
    assert r.status_code == 200
    api_key = r.json()["api_key"]
    hdr = {"X-Api-Key": api_key}
    # 缺 storeNo 且未配默认门店 → 422
    r2 = CLIENT.post("/api/intake/v1/orders", json=body, headers=hdr)
    assert r2.status_code == 422 and "storeNo" in r2.text
    # 配默认门店后 → 202；规格文案「大杯/热」经解析链命中
    from services import intake_registry
    cfg_path = intake_registry.INTAKE_CONFIG_PATH
    test_cfg = os.path.join(database.DATA_DIR, "test_intake_config.json")
    with open(test_cfg, "w", encoding="utf-8") as f:
        json.dump({"default_store_no": STORE_NO}, f)
    intake_registry.INTAKE_CONFIG_PATH = test_cfg
    try:
        r3 = CLIENT.post("/api/intake/v1/orders", json=body, headers=hdr)
        assert r3.status_code == 202, r3.text
        co = _co("KFC-001")
        assert co.source == "external:kfc" and co.api_key_id == r.json()["id"]
        assert co.payload["store_no"] == STORE_NO
        # 查询接口（同 key 归属隔离）
        r4 = CLIENT.get("/api/intake/v1/orders/KFC-001", headers=hdr)
        assert r4.status_code == 200 and r4.json()["orderNo"] == "KFC-001"
        assert CLIENT.get("/api/intake/v1/orders/INTAKE-001", headers=hdr).status_code == 404
        # 规格歧义：未知文案 → 422
        r5 = CLIENT.post("/api/intake/v1/orders",
                         json={**body, "orderNo": "KFC-BAD", "specs": ["半糖"]}, headers=hdr)
        assert r5.status_code == 422
    finally:
        intake_registry.INTAKE_CONFIG_PATH = cfg_path


def test_08_queue_stats_endpoint():
    r = CLIENT.get("/api/intake/queue/stats", headers=_auth())
    assert r.status_code == 200
    stats = r.json()
    assert stats["workers_configured"] == 0   # 测试进程 worker 禁用
    assert "pending" in stats and "dead" in stats


def test_09_callback_signed_delivery():
    """按单回调：HMAC 签名投递到本地 dummy server（127.0.0.1）。"""
    received: list[tuple[bytes, str]] = []

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received.append((self.rfile.read(length), self.headers.get("X-CHAGEE-Signature", "")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cb_url = f"http://127.0.0.1:{server.server_port}/cb"
    try:
        FIXTURE["mode"] = "rate"
        assert _submit("INTAKE-CB", price="22", callback_url=cb_url).status_code == 202
        assert "done" in _drain()
        with database.SessionLocal() as db:
            co = db.query(CustomerOrder).filter_by(customer_order_no="INTAKE-CB").one()
            intake_notify.on_chagee_order_paid(db, co.chagee_order_no, "P888")
        # 等两个回调（order_created + completed）到达
        for _ in range(100):
            if len(received) >= 2:
                break
            threading.Event().wait(0.1)
        assert len(received) >= 2, f"回调未送达，实收 {len(received)}"
        events = []
        for raw, sig in received:
            assert sig == hmac.new(_CB_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            events.append(json.loads(raw)["event"])
        assert "order_created" in events and "completed" in events
    finally:
        FIXTURE["mode"] = "default"
        server.shutdown()


def test_10_rate_limit_429():
    """每密钥滑窗限流：CHAGEE_INTAKE_RATE_LIMIT=8，同 key 第 9 单 429。"""
    r = CLIENT.post("/api/intake/keys", json={"label": "限流测试", "source": "ratelimit"},
                    headers=_auth())
    hdr = {"X-Api-Key": r.json()["api_key"]}
    statuses = []
    for i in range(9):
        rr = CLIENT.post("/api/intake/v1/orders",
                         json={"orderNo": f"RL-{i}", "linkId": TEST_SKU,
                               "storeNo": STORE_NO, "payAmount": "22"}, headers=hdr)
        statuses.append(rr.status_code)
    assert statuses[-1] == 429, statuses
    assert all(s in (202, 429) for s in statuses)


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        fn()
        print(f"[ok] {name}")
    print("ALL INTAKE TESTS PASSED")
