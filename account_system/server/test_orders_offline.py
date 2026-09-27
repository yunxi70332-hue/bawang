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
                    OrderRecord, Role, SystemUser)
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
            rows = (body or {}).get("discountList") or []
            if not rows:
                return _resp(TRADE[5])                   # 无券(recommendCoupon=true)：服务端自荐 20 元券，实付 0
            amt = Decimal(str(rows[0].get("discountAmount") or 0))
            return _resp(TRADE[5] if amt == 20 else TRADE[6])   # 20 元抵扣回样本5，10 元回样本6
        if path.endswith("/order/createOrder"):
            return _resp(ZERO[0])                        # 零元单响应：data 仅 orderNo
        if path.endswith("/order/getOrderDetail"):
            return _resp(ZERO[1])                        # 制作中 TA0001，券核销 HYW
        if path.endswith("/order/getOrderList"):
            return {"errcode": "0", "data": {"orderList": [_resp(ZERO[1])["data"]]}}
        if path.endswith("/order/getOrderStatus"):
            return {"errcode": "0", "data": 3}
        if path.endswith("/order/getWaitingInfo"):
            return {"errcode": "0", "data": {"waitingCups": 0, "waitingTime": 300, "queueLimit": 61}}
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
        audit = (db.query(AuditLog)
                   .filter(AuditLog.action == "feature.order_create", AuditLog.target == ACC_LABEL)
                   .order_by(AuditLog.id.desc()).first())
        assert audit is not None and ORDER_NO in audit.detail
        settle_audit = (db.query(AuditLog)
                          .filter(AuditLog.action == "feature.order_settle", AuditLog.target == ACC_LABEL)
                          .first())
        assert settle_audit is not None


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
    )
    bad = [p for p in CALLS if not p.endswith(allowed_suffixes)]
    assert not bad, f"出现了未经回放覆盖的协议调用: {bad}"


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
