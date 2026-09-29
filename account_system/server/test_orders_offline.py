"""F5/F6 订单域离线冒烟测试（wire 回放，零真实网络请求）。

运行：
    cd E:\\霸王茶姬\\account_system\\server && python test_orders_offline.py
    （或 pytest test_orders_offline.py -q）

要点：
  - 先把 database.DB_PATH 指向 data/test_orders.db 并重建 engine，再 import app（不污染生产 app.db）
  - monkeypatch services.chagee_bridge.build_client 为假客户端：按 path 后缀回放
    output/trade_samples_20260926.json 与 output/pay_zero_capture_20260926.json 的真实 wire 响应
    （whoami/getOrderList/getOrderStatus/getWaitingInfo 两份样本未覆盖，用最小合成响应）
  - 覆盖：settle 预览 / create 零元分支 + OrderRecord + 审计 / viewer 403 /
          无 draft 400 / 过期 draft 400 / 待支付单 409 / F6 查询回填
"""

import json
import os
import sys
import time
import urllib.parse
from decimal import Decimal

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根 E:\霸王茶姬
sys.path.insert(0, BASE)

# 禁用校准线程：避免测试进程残留后台副作用
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
# 禁用支付 watcher（app startup 挂载）：避免后台线程探针测试库里的支付会话干扰断言
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
# 禁用 frida 收银台铸造（pay_link_payload_with_session 会触发）：离线环境杜绝真触云手机
os.environ["CHAGEE_MINT_ENABLED"] = "0"
# 禁用跨进程状态广播（mark_session 收口会触发）：离线环境杜绝真发 HTTP 到 8010
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
# 日志隔离：oplog 日志库与文本日志均指向测试路径，避免污染生产 data/logs/
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_orders_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_orders.db")
if os.path.exists(database.DB_PATH):
    os.remove(database.DB_PATH)
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

database.engine = create_engine(
    f"sqlite:///{database.DB_PATH}", connect_args={"check_same_thread": False}, pool_pre_ping=True)
database.SessionLocal = sessionmaker(bind=database.engine, autoflush=False,
                                     autocommit=False, expire_on_commit=False)

import seed  # noqa: E402  （其后 import 的 app/seed 拿到的都是打补丁后的 engine/SessionLocal）
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from models import (AuditLog, ChageeAccount, CouponRecord, CouponUsageLog,  # noqa: E402
                    DecisionLog, OrderPlan, OrderRecord, Role, SystemUser)
from routers import orders as orders_router  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402

# ---------- 2. wire 回放假客户端（替换 build_client，杜绝真实网络） ----------


def _load(name):
    with open(os.path.join(ROOT, "output", name), encoding="utf-8") as f:
        return json.load(f)


TRADE = _load("trade_samples_20260926.json")          # 购物车/试算 wire
ZERO = _load("pay_zero_capture_20260926.json")        # 零元 createOrder + getOrderDetail wire


def _resp(item):
    body = item["resp_body"]
    return json.loads(body) if isinstance(body, str) else body


CALLS: list[str] = []   # 记录假客户端实际收到的调用（供断言确认未越权调用其它端点）


class FakeClient:
    """按 path 后缀回放 2026-09-26 生产 wire 响应。"""

    def __init__(self, account):
        self.account = account

    # -- 无 wire 样本端点的最小合成（结构与引擎读取字段一致） --
    def get(self, path, **kw):
        CALLS.append(path)
        if path.endswith("/customer/userInfo/query"):
            return {"errcode": "0", "data": {"customerId": "1190018250",
                                             "mobileEncrypt": "AESxFAKE", "nickName": "离线测试"}}
        raise AssertionError(f"未预期的 GET 协议调用: {path}")

    def whoami(self):
        return self.get("/user-client/customer/userInfo/query")

    def post(self, path, body=None, **kw):
        CALLS.append(path)
        if path.endswith("/goods/sku/calculatePrice"):
            if FIXTURE["mode"] == "rate":
                return {"errcode": "0", "data": {   # 折扣率券夹具：22 元单价（事故原值）
                    "spuId": "625339451983278080", "spuType": "stand", "skuId": "653632618000097282",
                    "totalSalePrice": "22.00", "totalTradePrice": "22.00", "totalGoodsItemPrice": "22.00",
                    "totalGoodsItemDiscountAmount": "0.00", "totalGoodsPaymentDiscountAmount": "0.00",
                    "totalDiscountAmount": "0.00", "totalWrappingPrice": "0.00"}}
            return {"errcode": "0", "data": {            # 与 settle 夹具自洽：20 元无商品折
                "spuId": "625339451983278080", "spuType": "stand", "skuId": "653632618000097282",
                "totalSalePrice": "20.00", "totalTradePrice": "20.00", "totalGoodsItemPrice": "20.00",
                "totalGoodsItemDiscountAmount": "0.00", "totalGoodsPaymentDiscountAmount": "0.00",
                "totalDiscountAmount": "0.00", "totalWrappingPrice": "0.00"}}
        if path.endswith("/shoppingCart/change"):
            return _resp(TRADE[2])                       # 加购请求回执
        if path.endswith("/shoppingCart/get"):
            return _resp(TRADE[3])                       # 加购后的满购物车快照（totalTradePrice 20）
        if path.endswith("/order/settlePrice"):
            if FIXTURE["mode"] == "rate":
                return RATE_SETTLE_RESP                  # 7折券：服务端回填 6.6（带券/自荐同响应）
            rows = (body or {}).get("discountList") or []
            if not rows:
                return _resp(TRADE[5])                   # 无券(recommendCoupon=true)：服务端自荐 20 元券，实付 0
            amt = Decimal(str(rows[0].get("discountAmount") or 0))
            return _resp(TRADE[5] if amt == 20 else TRADE[6])   # 20 元抵扣回样本5，10 元回样本6
        if path.endswith("/order/createOrder"):
            if FIXTURE["mode"] == "rate":
                return RATE_CREATE_RESP                  # 差额单：payUrl 支付串（15.40）
            return _resp(ZERO[0])                        # 零元单响应：data 仅 orderNo
        if path.endswith("/order/getOrderDetail"):
            return _resp(ZERO[1])                        # 制作中 TA0001，券核销 HYW
        if path.endswith("/order/getOrderList"):
            return {"errcode": "0", "data": {"orderList": [_resp(ZERO[1])["data"]]}}
        if path.endswith("/order/getOrderStatus"):
            return {"errcode": "0", "data": 3}
        if path.endswith("/order/getWaitingInfo"):
            return {"errcode": "0", "data": {"waitingCups": 0, "waitingTime": 300, "queueLimit": 61}}
        if path.endswith("/order/continuePay"):
            # 续付重铸（pay 环节方案阈值用例）：最小合成 wire 响应（payUrl 内嵌 JSON，
            # 实付 15.40 与 rate 夹具同额；orderNo 回显请求单号便于按单断言）
            return {"errcode": "0", "data": {
                "orderNo": str((body or {}).get("orderNo") or RATE_ORDER_NO),
                "payNo": "CHP20260929CONT000000000000",
                "payUrl": json.dumps({"requestJson": {"orderStr":
                    "out_trade_no=331LCONT0001&total_amount=15.40&biz_content=" + urllib.parse.quote(
                        json.dumps({"out_trade_no": "331LCONT0001", "total_amount": "15.40",
                                    "time_expire": "2026-09-29 23:00:00"}))}}),
            }}
        raise AssertionError(f"未预期的 POST 协议调用: {path}")


bridge.build_client = lambda account: FakeClient(account)   # monkeypatch（orders 路由经 bridge.trade_api 间接命中）

# ---------- 3. 测试数据 ----------

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(SystemUser).filter(SystemUser.username == "viewer_smoke").first():
        viewer_role = db.query(Role).filter(Role.name == "viewer").one()
        db.add(SystemUser(username="viewer_smoke", display_name="只读冒烟",
                          password_hash=hash_password("Viewer@123"), role_id=viewer_role.id))
        db.commit()
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800001111").first():
        db.add(ChageeAccount(label="下单冒烟", phone="13800001111", device_uuid="uuid-offline-orders-1",
                             token="fake.token.offline", sk="fakesk", customer_id="1190018250",
                             status="online", group="默认"))
        db.commit()
    ACC_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800001111").one().id

ACC_LABEL = f"下单冒烟#{ACC_ID}"
ORDER_NO = "202609260910110023151918250"          # 零元 wire 样本订单号
COUPON_HYW = "1309482592713252864"                # 霸王茶姬20元代金券-HYW（样本5 服务端自荐券）
STORE_NO, STORE_NAME = "CN03324", "福建龙岩新罗万达广场店"

# ---------- 折扣率券（"7折"）wire 回放夹具（2026-09-28 事故回归） ----------
# 事故：coupon_face 首数字正则把 "7折" 当 ¥7 → ΣdiscountList=7 ≠ totalDiscountAmount=6.6，
# assert_settle_consistency 提交前拦截（账号 茶壶#8，22 元大杯 ×7折 = 抵扣 6.6 / 应付 15.4）
FIXTURE = {"mode": "default"}                     # "rate" = 折扣率券回放
RATE_COUPON_CODE = "CRATE7Z"
RATE_ORDER_NO = "202609280910119999000000099"
RATE_COUPON = {
    "couponCode": RATE_COUPON_CODE, "templateName": "全品类单杯7折券-离线",
    "benefitText": "7折", "benefit2Text": "", "thresholdTips": "优惠1杯",
    "useEndTime": 4102444800000, "canDiscount": True,   # 2100 年：永不过期，断言确定性
}
RATE_SETTLE_RESP = {"errcode": "0", "data": {
    "confirmOrderKey": "RATE-KEY-1",
    "tradeFundInfo": {"totalTradePrice": "22", "buyerRealPrice": "15.4",
                      "totalDiscountAmount": "6.6", "totalCouponDiscountAmount": "6.6"},
    "assetInfo": {"userCouponInfo": {"availableCouponList": [RATE_COUPON]}},
    "orderGroupList": [{"tradeFundInfo": {"buyerRealPrice": "15.4"}}],
    "discountList": [{"discountId": RATE_COUPON_CODE, "discountName": "全品类单杯7折券-离线",
                      "discountSource": 1, "discountType": 1, "scopeType": 2,
                      "discountAmount": "6.6", "currentSelect": None}],
}}
RATE_CREATE_RESP = {"errcode": "0", "data": {
    "orderNo": RATE_ORDER_NO, "payNo": "CHP20260928RATE000000000000",
    "payUrl": json.dumps({"requestJson": {"orderStr":
        "out_trade_no=331LRATE0001&total_amount=15.40&biz_content=" + urllib.parse.quote(
            json.dumps({"out_trade_no": "331LRATE0001", "total_amount": "15.40",
                        "time_expire": "2026-09-28 23:00:00"}))}}),
}}

CLIENT = TestClient(app_module.app)
_tokens: dict[str, str] = {}


def _login(username: str, password: str) -> str:
    if username not in _tokens:
        r = CLIENT.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败 {username}: {r.status_code} {r.text}"
        _tokens[username] = r.json()["token"]
    return _tokens[username]


def _auth(username="admin", password="Admin@123"):
    return {"Authorization": f"Bearer {_login(username, password)}"}


SETTLE_BODY = {
    "store_no": STORE_NO, "store_name": STORE_NAME,
    "spu_id": "625339451983278080", "spu_name": "伯牙绝弦",
    "sku_id": "653632618000097282", "sku_name": "伯牙绝弦",
    "item_sku_id": "653632618000097282", "quantity": 1, "sale_price": 20.0,
    "spec_list": [{"specId": "653599312273510400", "specOptionId": "653599312273510402"}],
    "image_url": "", "spu_type": "stand",
}


def _settle():
    """触发一次 settle 并返回 (draft_id, preview)。"""
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle", json=SETTLE_BODY, headers=_auth())
    assert r.status_code == 200, f"settle 失败: {r.status_code} {r.text}"
    data = r.json()
    return data["draft_id"], data["preview"]


# ---------- 4. 测试用例 ----------

def test_admin_settle_preview():
    """admin settle：返回 draft 预览（total/buyer_real_price/available_coupons 非空）。"""
    draft_id, preview = _settle()
    assert draft_id.startswith("d-")
    assert preview["total_trade_price"] == "20.00"
    assert preview["buyer_real_price"] == "0.00"
    assert isinstance(preview["available_coupons"], list) and preview["available_coupons"]
    assert preview["goods"] and preview["goods"][0]["name"] == "伯牙绝弦"
    assert preview["goods"][0]["price"] == "20.00" and preview["goods"][0]["quantity"] == 1
    # 推荐券（wire 数据 2026-09-26 有效期内）：六张 20 元 + 一张 10 元 → 覆盖且面额最小取列表首个 20 元券
    if preview["recommended_coupon"]:
        assert preview["recommended_coupon"]["couponCode"] == COUPON_HYW
        assert preview["recommended_deduction"] == "20.00"
        assert preview["estimated_pay"] == "0.00"
        assert preview["scenario_preview"] == "zero"
    else:  # 样本券全部过期时（未来运行）退化为无券判定
        assert preview["recommended_deduction"] == "0.00"
        assert preview["estimated_pay"] == "20.00"
        assert preview["scenario_preview"] == "partial"
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle", json=SETTLE_BODY, headers=_auth())
    assert "expires_at" in r.json()


def test_create_zero_with_coupon():
    """create（选 20 元券）→ zero 分支响应 + OrderRecord 落库 + 审计行存在。"""
    draft_id, _ = _settle()
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": draft_id, "coupon_code": COUPON_HYW}, headers=_auth())
    assert r.status_code == 200, f"create 失败: {r.status_code} {r.text}"
    data = r.json()
    assert data["result"] == "zero"
    assert data["order_no"] == ORDER_NO
    assert data["status"] == 3 and data["status_label"] == "制作中"
    assert data["pickup_no"] == "TA0001"
    assert data["pay_amount"] == "0"
    assert data["coupon_code"] == COUPON_HYW

    with database.SessionLocal() as db:
        rec = db.query(OrderRecord).filter(OrderRecord.order_no == ORDER_NO).one()
        assert rec.account_id == ACC_ID
        assert rec.scenario == "zero" and rec.coupon_code == COUPON_HYW
        assert rec.status == 3 and rec.status_label == "制作中"
        assert rec.pickup_no == "TA0001" and rec.pay_amount == "0"
        assert rec.total_amount == "20" and rec.store_no == STORE_NO
        # 零元单成单即核销（§17）：券档案直接迁移历史桶，不等支付确认通道
        crec = db.query(CouponRecord).filter(CouponRecord.coupon_code == COUPON_HYW).one()
        assert crec.bucket == "historical" and crec.last_order_no == ORDER_NO
        audit = (db.query(AuditLog)
                   .filter(AuditLog.action == "feature.order_create", AuditLog.target == ACC_LABEL)
                   .order_by(AuditLog.id.desc()).first())
        assert audit is not None and ORDER_NO in audit.detail
        settle_audit = (db.query(AuditLog)
                          .filter(AuditLog.action == "feature.order_settle", AuditLog.target == ACC_LABEL)
                          .first())
        assert settle_audit is not None


def test_create_with_rate_coupon():
    """折扣率券（7折）回归：预览/下单全链路走服务端回填 6.6 —— 旧缺陷本地按 ¥7 构造行
    会被一致性断言 409 拦截（2026-09-28 事故复现→修复验证）。"""
    FIXTURE["mode"] = "rate"
    try:
        # 预览：服务端自荐 7折券并回填 totalDiscountAmount=6.6（旧口径 recommended_deduction=7.00）
        draft_id, preview = _settle()
        assert preview["total_trade_price"] == "22.00"
        assert preview["recommended_coupon"]["couponCode"] == RATE_COUPON_CODE
        assert preview["recommended_deduction"] == "6.60"
        assert preview["estimated_pay"] == "15.40"
        assert preview["scenario_preview"] == "partial"
        # 下单：取服务端回填行（Σ=6.6 == totalDiscountAmount=6.6）→ 一致性通过、差额成单
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                        json={"draft_id": draft_id, "coupon_code": RATE_COUPON_CODE},
                        headers=_auth())
        assert r.status_code == 200, f"create 失败: {r.status_code} {r.text}"
        data = r.json()
        assert data["result"] == "partial"
        assert data["order_no"] == RATE_ORDER_NO
        assert data["total_amount"] == "15.40"
        with database.SessionLocal() as db:
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == RATE_ORDER_NO).one()
            assert rec.scenario == "partial" and rec.coupon_code == RATE_COUPON_CODE
            assert rec.pay_amount == "15.4" and rec.total_amount == "22"
            usage = (db.query(CouponUsageLog)
                       .filter(CouponUsageLog.coupon_code == RATE_COUPON_CODE,
                               CouponUsageLog.result == "pending")
                       .order_by(CouponUsageLog.id.desc()).first())
            assert usage is not None and usage.deduction == "6.60"   # 差额单预记待支付（§18），服务端事实（旧口径记 7）
            crec = db.query(CouponRecord).filter(CouponRecord.coupon_code == RATE_COUPON_CODE).one()
            assert crec.amount == ""                                 # 折扣率券不落元面额
            # 清场：本单 status=1 会卡后续用例的单次一单守卫，置已完成
            rec.status = 6
            db.commit()
    finally:
        FIXTURE["mode"] = "default"
        with orders_router._draft_lock:
            orders_router._drafts.pop(ACC_ID, None)


def test_viewer_forbidden():
    """viewer（无 feature:order）调 settle/create → 403。"""
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle", json=SETTLE_BODY,
                    headers=_auth("viewer_smoke", "Viewer@123"))
    assert r.status_code == 403, r.text
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": "d-none", "coupon_code": None},
                    headers=_auth("viewer_smoke", "Viewer@123"))
    assert r.status_code == 403, r.text


def test_create_without_draft():
    """无 draft 的 create → 400。"""
    with orders_router._draft_lock:
        orders_router._drafts.pop(ACC_ID, None)
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": "d-ghost", "coupon_code": None}, headers=_auth())
    assert r.status_code == 400
    assert "草稿已过期" in r.json()["detail"]


def test_create_expired_draft():
    """过期 draft（created_at 早于 10 分钟）→ 400。"""
    draft_id, _ = _settle()
    with orders_router._draft_lock:
        orders_router._drafts[ACC_ID]["created_at"] = time.time() - (orders_router.DRAFT_TTL_SECONDS + 100)
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": draft_id, "coupon_code": COUPON_HYW}, headers=_auth())
    assert r.status_code == 400
    assert "草稿已过期" in r.json()["detail"]


def test_create_blocked_by_pending_order():
    """单次一单：账号存在 status==1 待支付订单 → 409。"""
    with database.SessionLocal() as db:
        db.add(OrderRecord(account_id=ACC_ID, order_no="PENDING-FOR-409", status=1,
                           status_label="待支付", scenario="partial"))
        db.commit()
    try:
        draft_id, _ = _settle()
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                        json={"draft_id": draft_id, "coupon_code": None}, headers=_auth())
        assert r.status_code == 409
        assert "待支付订单" in r.json()["detail"]
    finally:
        with database.SessionLocal() as db:
            db.query(OrderRecord).filter(OrderRecord.order_no == "PENDING-FOR-409").delete()
            db.commit()


def test_f6_queries_and_upsert():
    """F6：列表/详情/状态/等待 查询可用，且详情把 uniquePosOrderNo 回填 OrderRecord。"""
    h = _auth()
    r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/orders", params={"tab": "today", "page": 1, "page_size": 10},
                   headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items and items[0]["order_no"] == ORDER_NO
    assert items[0]["order_status"] == 3 and items[0]["can_waiting"] is True
    assert items[0]["pickup_no"] == "TA0001"

    r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/orders/{ORDER_NO}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == 3 and d["status_label"] == "制作中"
    assert d["pay_amount"] == "0" and d["pay_type_text"] == "免支付"
    assert d["unique_pos_order_no"] == "D00296329595152515072"
    assert d["items"] and d["promotions"][0]["promotionId"] == COUPON_HYW
    assert d["payment_expiry_ts"] is None

    r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/orders/{ORDER_NO}/status", headers=h)
    assert r.status_code == 200 and r.json() == {"status": 3, "status_label": "制作中"}

    r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/orders/{ORDER_NO}/waiting", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"waiting_cups": 0, "waiting_time": 300, "queue_limit": 61}

    # 详情回填：OrderRecord.unique_pos_order_no 由空补齐
    with database.SessionLocal() as db:
        rec = db.query(OrderRecord).filter(OrderRecord.order_no == ORDER_NO).one()
        assert rec.unique_pos_order_no == "D00296329595152515072"


def test_no_real_network_endpoints_only():
    """全程仅触达 wire 样本覆盖的端点（防误发未回放的真实协议请求）。"""
    allowed_suffixes = (
        "/goods/sku/calculatePrice",
        "/shoppingCart/change", "/shoppingCart/get", "/order/settlePrice",
        "/order/createOrder", "/order/getOrderDetail", "/order/getOrderList",
        "/order/getOrderStatus", "/order/getWaitingInfo", "/customer/userInfo/query",
        "/order/continuePay",   # pay 环节方案阈值用例的最小合成响应（同 userInfo 合成口径）
    )
    bad = [p for p in CALLS if not p.endswith(allowed_suffixes)]
    assert not bad, f"出现了未经回放覆盖的协议调用: {bad}"


# ---------- 4c. 方案级支付金额上限（2026-09-29：创建/支付两环节 fail-closed） ----------

def _mk_plan(max_pay_amount: str, name: str) -> int:
    """直插一条下单方案并返回 id（绕过方案 API 的必填校验，覆盖历史空值/非法行场景）。"""
    with database.SessionLocal() as db:
        plan = OrderPlan(name=name, strategy="cost_first", enabled=True,
                         max_pay_amount=max_pay_amount)
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return plan.id


def _cleanup_plans(*plan_ids: int) -> None:
    with database.SessionLocal() as db:
        for pid in plan_ids:
            if pid:
                plan = db.get(OrderPlan, pid)
                if plan is not None:
                    db.delete(plan)
        db.commit()


def test_plan_pay_threshold_create_blocked():
    """create 环节超限：7折券复跑实付 15.40 > 阈值 15.00 → 422 超限文案，且未触达
    createOrder（绝不能带超限金额成单）；body.plan_id 与 decision_log_id 两种方案
    来源同受约束；显式引用不存在方案 → 422（与 decide 同语义）。"""
    FIXTURE["mode"] = "rate"
    try:
        plan_id = _mk_plan("15.00", "阈值冒烟-超限")
        with database.SessionLocal() as db:
            dlog = DecisionLog(plan_json={"plan_id": plan_id, "plan_name": "阈值冒烟-超限"})
            db.add(dlog)
            db.commit()
            db.refresh(dlog)
            log_id = dlog.id
        try:
            draft_id, _ = _settle()
            # ① body.plan_id 直带方案 → 422 超限（金额取选券复跑后的服务端确认实付）
            before = len(CALLS)
            r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                            json={"draft_id": draft_id, "coupon_code": RATE_COUPON_CODE,
                                  "plan_id": plan_id},
                            headers=_auth())
            assert r.status_code == 422, r.text
            detail = r.json()["detail"]
            assert "当前支付金额超过方案限制" in detail
            assert "15.40" in detail and "15.00" in detail
            assert not [p for p in CALLS[before:] if p.endswith("/order/createOrder")], "超限单不得触达 createOrder"
            # ② decision_log_id 来源（工作台 settle_prefill 主链路）→ 同样 422
            r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                            json={"draft_id": draft_id, "coupon_code": RATE_COUPON_CODE,
                                  "decision_log_id": log_id},
                            headers=_auth())
            assert r.status_code == 422 and "当前支付金额超过方案限制" in r.json()["detail"], r.text
            # ③ 显式引用不存在的方案 → 422
            r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                            json={"draft_id": draft_id, "coupon_code": RATE_COUPON_CODE,
                                  "plan_id": 999999},
                            headers=_auth())
            assert r.status_code == 422 and "下单方案不存在" in r.json()["detail"], r.text
        finally:
            with database.SessionLocal() as db:
                db.query(DecisionLog).filter(DecisionLog.id == log_id).delete()
                db.commit()
            _cleanup_plans(plan_id)
    finally:
        FIXTURE["mode"] = "default"
        with orders_router._draft_lock:
            orders_router._drafts.pop(ACC_ID, None)


def test_plan_pay_threshold_create_unconfigured():
    """create 环节阈值未配置（历史空值行，方案 API 已强制必填）→ 422 fail-closed 默认拒绝。"""
    FIXTURE["mode"] = "rate"
    try:
        plan_id = _mk_plan("", "阈值冒烟-未配置")
        try:
            draft_id, _ = _settle()
            r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                            json={"draft_id": draft_id, "coupon_code": RATE_COUPON_CODE,
                                  "plan_id": plan_id},
                            headers=_auth())
            assert r.status_code == 422, r.text
            assert "已默认拒绝交易" in r.json()["detail"]
        finally:
            _cleanup_plans(plan_id)
    finally:
        FIXTURE["mode"] = "default"
        with orders_router._draft_lock:
            orders_router._drafts.pop(ACC_ID, None)


def test_plan_pay_threshold_create_auto_mode_pass():
    """plan_id=0（自动模式）：不做方案级校验直接放行成单——即使库内存在未被引用的超限方案。"""
    FIXTURE["mode"] = "rate"
    try:
        plan_id = _mk_plan("0.01", "阈值冒烟-旁路不引用")
        try:
            draft_id, _ = _settle()
            r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                            json={"draft_id": draft_id, "coupon_code": RATE_COUPON_CODE,
                                  "plan_id": 0},
                            headers=_auth())
            assert r.status_code == 200, f"create 失败: {r.status_code} {r.text}"
            assert r.json()["result"] == "partial"
        finally:
            with database.SessionLocal() as db:   # 清场：status=1 会卡后续用例的单次一单守卫
                rec = db.query(OrderRecord).filter(OrderRecord.order_no == RATE_ORDER_NO).first()
                if rec is not None:
                    rec.status = 6
                    db.commit()
            _cleanup_plans(plan_id)
    finally:
        FIXTURE["mode"] = "default"
        with orders_router._draft_lock:
            orders_router._drafts.pop(ACC_ID, None)


def test_plan_pay_threshold_pay_stage():
    """pay 环节：continuePay 重铸支付串（实付 15.40）在支付动作发起前复核——
    超限 422 / 未配置 422 / 未绑定决策流水（无方案）放行 200 正常下发支付串。"""
    plan_block = _mk_plan("15.00", "阈值冒烟-pay超限")
    plan_unconf = _mk_plan("", "阈值冒烟-pay未配置")
    bound_block, bound_unconf, bound_free = "PAYTH-BLOCK-ORDER", "PAYTH-UNCONF-ORDER", "PAYTH-FREE-ORDER"
    with database.SessionLocal() as db:
        db.add(DecisionLog(order_no=bound_block,
                           plan_json={"plan_id": plan_block, "plan_name": "阈值冒烟-pay超限"}))
        db.add(DecisionLog(order_no=bound_unconf,
                           plan_json={"plan_id": plan_unconf, "plan_name": "阈值冒烟-pay未配置"}))
        db.commit()
    try:
        h = _auth()
        # ① 超限：重铸 15.40 支付串 → 下发支付串/自动扣款动作发起前 422
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/{bound_block}/pay",
                        json={"mode": "manual"}, headers=h)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert "当前支付金额超过方案限制" in detail and "15.40" in detail
        # ② 阈值未配置 → fail-closed 422
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/{bound_unconf}/pay",
                        json={"mode": "manual"}, headers=h)
        assert r.status_code == 422 and "已默认拒绝交易" in r.json()["detail"], r.text
        # ③ 无决策流水绑定（无方案）→ 放行，正常下发支付串
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/{bound_free}/pay",
                        json={"mode": "manual"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["total_amount"] == "15.40"
    finally:
        with database.SessionLocal() as db:
            db.query(DecisionLog).filter(DecisionLog.order_no.in_(
                (bound_block, bound_unconf))).delete()
            # 清场：放行分支 _upsert_order 会新建待支付快照，删掉避免遗留
            db.query(OrderRecord).filter(OrderRecord.order_no == bound_free).delete()
            db.commit()
        _cleanup_plans(plan_block, plan_unconf)


# ---------- 4b. 优惠券使用规则（券ID↔token 映射 / 有效性验证 / 使用日志） ----------

def test_settle_upserts_coupon_records():
    """settle 后券档案入库：券ID↔账号(token指纹)映射 + 完整名称全量存储。"""
    _settle()
    with database.SessionLocal() as db:
        rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == COUPON_HYW).one()
        assert rec.account_id == ACC_ID
        assert rec.token_fingerprint.startswith("fake.token")     # 归属账号 token 指纹
        assert rec.template_name                        # 完整名称已存
        assert rec.bucket == "settle_available" and rec.synced_from == "settle"
        assert rec.use_end_time and rec.use_end_time > 1_700_000_000_000  # 毫秒级时间戳（>2023）


def test_create_coupon_ownership_rejected():
    """券ID↔token 映射校验：券档案归属其他账号 → 400 拒绝 + 使用日志 result=rejected。"""
    with database.SessionLocal() as db:
        other = ChageeAccount(label="别人账号", phone="13900002222", device_uuid="uuid-other-acc",
                              token="other.token.value", status="online")
        db.add(other)
        db.commit()
        other_id = other.id
        rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == COUPON_HYW).first()
        if rec:                       # settle 已入库 → 重绑到他人账号（结束后还原）
            original_owner = rec.account_id
            rec.account_id = other_id
            inserted = False
        else:
            db.add(CouponRecord(coupon_code=COUPON_HYW, account_id=other_id,
                                token_fingerprint="other.token...len=17",
                                template_name="霸王茶姬20元代金券-HYW"))
            original_owner, inserted = None, True
        db.commit()
    try:
        draft_id, _ = _settle()   # 注意：settle 会把券重绑回本账号——故重绑须在 settle 之后
        with database.SessionLocal() as db:
            rec2 = db.query(CouponRecord).filter(CouponRecord.coupon_code == COUPON_HYW).one()
            rec2.account_id = other_id
            db.commit()
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                        json={"draft_id": draft_id, "coupon_code": COUPON_HYW}, headers=_auth())
        assert r.status_code == 400
        assert "归属于其他账号" in r.json()["detail"]
        with database.SessionLocal() as db:
            log = (db.query(CouponUsageLog)
                     .filter(CouponUsageLog.coupon_code == COUPON_HYW,
                             CouponUsageLog.result == "rejected")
                     .order_by(CouponUsageLog.id.desc()).first())
            assert log is not None and "归属" in log.fail_reason
            assert log.account_label == ACC_LABEL and log.operator == "admin"
    finally:
        with database.SessionLocal() as db:
            rec3 = db.query(CouponRecord).filter(CouponRecord.coupon_code == COUPON_HYW).one()
            if inserted:
                db.delete(rec3)
            else:
                rec3.account_id = original_owner
            db.query(ChageeAccount).filter(ChageeAccount.id == other_id).delete()
            db.commit()


def test_validate_coupon_unit():
    """_validate_coupon 单元验证：有效期（过期/未生效）/ 门槛 / 可用性三类拒绝路径。"""
    from types import SimpleNamespace
    from routers.orders import _validate_coupon
    draft = {"settle_base": SimpleNamespace(total_trade_price="20")}
    now_ms = int(time.time() * 1000)
    base = {"couponCode": "X", "templateName": "测试券", "canDiscount": True,
            "thresholdTips": "", "useStartTime": now_ms - 86400000, "useEndTime": now_ms + 86400000}
    # 通过路径（无归属记录时跳过①）
    assert _validate_coupon(_db(), _acc(), draft, "X", dict(base)) is None
    # ② canDiscount=False
    e2 = dict(base, canDiscount=False, unavailableReason="门店不可用")
    assert "券不可用" in _validate_coupon(_db(), _acc(), draft, "X", e2)
    # ③ 已过期 / 未生效
    e3a = dict(base, useEndTime=now_ms - 1000)
    assert "已过期" in _validate_coupon(_db(), _acc(), draft, "X", e3a)
    e3b = dict(base, useStartTime=now_ms + 86400000, useEndTime=now_ms + 2 * 86400000)
    assert "未到生效时间" in _validate_coupon(_db(), _acc(), draft, "X", e3b)
    # ④ 门槛未满足（满 100 元可用，订单 20 元）
    e4 = dict(base, thresholdTips="满100元可用")
    assert "门槛未满足" in _validate_coupon(_db(), _acc(), draft, "X", e4)


def _db():
    return database.SessionLocal()


def _acc():
    with database.SessionLocal() as db:
        return db.get(ChageeAccount, ACC_ID)


def test_usage_logs_and_stats():
    """成单后：使用日志 success 落库（含订单号/抵扣/操作人）+ 查询端点与统计摘要 + 券档案使用痕迹。
    （命名保证字典序晚于 create 系用例执行）"""
    with database.SessionLocal() as db:
        log = (db.query(CouponUsageLog)
                 .filter(CouponUsageLog.coupon_code == COUPON_HYW,
                         CouponUsageLog.result == "success")
                 .order_by(CouponUsageLog.id.desc()).first())
        assert log is not None, "成功使用日志未落库"
        assert log.order_no == ORDER_NO and log.scenario == "zero"
        assert log.deduction == "20.00" and log.pay_amount == "0.00"
        assert log.operator == "admin" and log.account_label == ACC_LABEL
        rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == COUPON_HYW).one()
        assert rec.last_order_no == ORDER_NO and rec.last_used_at is not None
    h = _auth()
    r = CLIENT.get("/api/ops/coupon-usage-logs", params={"keyword": COUPON_HYW}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] >= 2                                   # success + rejected（归属测试遗留）
    assert d["stats"]["success"] >= 1
    assert Decimal(d["stats"]["total_deduction"]) >= Decimal("20")
    r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/coupons/records", headers=h)
    assert r.status_code == 200 and r.json()["total"] >= 1
    item = r.json()["items"][0]
    assert item["template_name"] and item["token_fingerprint"]


def test_usage_logs_order_link_fields():
    """「查看订单」数据面（2026-09-29 对账联动）：券日志行回填 account_id/order_status(_label)
    （join OrderRecord）+ 订单详情端点 live 优先 / 失败本地快照兜底 / 无记录走失败路径。"""
    h = _auth()
    # OrderRecord 置为已完成（6）→ 券日志行 order_status=6（前端按钮展示条件）
    with database.SessionLocal() as db:
        rec = db.query(OrderRecord).filter(OrderRecord.order_no == ORDER_NO).one()
        rec.status = 6
        rec.status_label = "已完成"
        rec.goods_desc = "伯牙绝弦（大杯） x1"
        rec.coupon_code = COUPON_HYW
        db.commit()

    d = CLIENT.get("/api/ops/coupon-usage-logs",
                   params={"keyword": COUPON_HYW}, headers=h).json()
    row = next(i for i in d["items"] if i["order_no"] == ORDER_NO)
    assert row["account_id"] == ACC_ID
    assert row["order_status"] == 6 and row["order_status_label"] == "已完成", row
    # 未成单行（rejected 记录 order_no 空）→ order_status None（无按钮）
    rj = next((i for i in d["items"] if i["result"] == "rejected"), None)
    if rj is not None:
        assert rj["order_no"] == "" and rj["order_status"] is None

    # 详情端点：live 优先（FakeClient getOrderDetail 正常回放）
    r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/orders/{ORDER_NO}", headers=h)
    assert r.status_code == 200, r.text
    live = r.json()
    assert live["detail_source"] == "live" and live["order_no"]

    # live 详情按 wire 夹具回填状态 3（制作中）——重置回 6 再验证兜底快照口径
    with database.SessionLocal() as db:
        rec = db.query(OrderRecord).filter(OrderRecord.order_no == ORDER_NO).one()
        rec.status = 6
        rec.status_label = "已完成"
        db.commit()

    # live 失败（模拟账号离线/凭证失效）→ 本地 OrderRecord 快照兜底（字段与 wire 同构）
    orig_build = bridge.build_client

    class DeadClient:
        def order_detail(self, order_no):
            raise RuntimeError("token expired (offline)")

    def dead_factory(account):
        return DeadClient()

    bridge.build_client = dead_factory
    try:
        r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/orders/{ORDER_NO}", headers=h)
        assert r.status_code == 200, r.text
        snap = r.json()
        assert snap["detail_source"] == "local_snapshot", snap
        assert snap["status"] == 6 and snap["order_no"] == ORDER_NO
        assert snap["items"] and snap["items"][0]["name"].startswith("伯牙绝弦")
        assert snap["promotions"] and snap["promotions"][0]["promotionId"] == COUPON_HYW
        # 本地无记录 + live 失败 → 原失败路径（统一异常链 HTTPException）
        r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/orders/NO-SUCH-ORDER-404", headers=h)
        assert r.status_code >= 400, r.text
    finally:
        bridge.build_client = orig_build


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
