"""订单超时校准离线测试（FakeClient 回放，零真实网络请求）。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server && python test_reconcile_offline.py
    （或 pytest test_reconcile_offline.py -q）

覆盖（services/order_reconcile.py + routers/orders.py 校准相关段）：
  - POST /api/ops/orders/reconcile 手动触发：超时取消回滚券 / 超时已支付保券 /
    网络失败跳过且不破坏下一轮 / 未过宽限线不扫描 / 权限门控（401 / viewer 403）
  - order_create 超时单内联校准放行（不再被待支付单 409 卡死）
  - POST .../orders/{order_no}/cancel 手动取消端点回滚券（coupon_rolled_back）
  - rollback_coupon_usage 幂等防护（同券同单仅写一条 rolled_back 日志）
  - 直调 reconcile_order（still_pending / SessionExpiredError→账号置 expired）与
    _scan 层 skipped_no_account / skipped_disabled / skipped_no_token 三分支
  - GET /api/ops/coupon-usage-logs 统计（rolled_back 计数、累计抵扣差值下限 0、result 过滤）
  - CHAGEE_RECONCILE_INTERVAL_SECONDS<=0 时 start_reconcile_thread 不创建线程

要点（与 test_orders_offline.py 同模式）：
  - 先把 database.DB_PATH 指向 data/test_reconcile.db 并重建 engine，再 import app
  - import app 之前置 CHAGEE_RECONCILE_INTERVAL_SECONDS=0 禁用后台校准线程
  - monkeypatch services.chagee_bridge.build_client 为假客户端：未知 path 抛 AssertionError
    （防越权端点）；getOrderStatus 行为由模块级 FAKE 字典控制（回放 int / 抛异常）
"""

import os
import sys
import threading
from datetime import datetime, timedelta
from decimal import Decimal

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根
sys.path.insert(0, BASE)

# 禁用校准线程（必须在 import app 之前）：TestClient startup 会调 start_reconcile_thread，
# 置 0 后不创建后台线程，避免测试进程残留 60 秒后醒来的 daemon 线程产生副作用
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
# 禁用支付 watcher（app startup 挂载）：避免后台线程探针测试库里的支付会话干扰断言
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
# 禁用 frida 收银台铸造（pay_link_payload_with_session 会触发）：离线环境杜绝真触云手机
os.environ["CHAGEE_MINT_ENABLED"] = "0"
# 禁用跨进程状态广播（mark_session 取消联动会触发）：离线环境杜绝真发 HTTP 到 8010
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
# 日志隔离：oplog 日志库与文本日志均指向测试路径，避免污染生产 data/logs/
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_reconcile_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_reconcile.db")
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
from models import (AuditLog, ChageeAccount, CouponRecord, CouponUsageLog,  # noqa: E402
                    OrderRecord, Role, SystemUser)
from routers import orders as orders_router  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import order_reconcile as reconcile_module  # noqa: E402
from services.order_reconcile import (  # noqa: E402
    reconcile_expired_pending_orders, reconcile_order, rollback_coupon_usage, start_reconcile_thread,
)

# ---------- 2. FakeClient（替换 build_client，杜绝真实网络） ----------

# getOrderStatus 的可控行为：status=回放的裸 int；error=抛出的 ChageeError；
# session_expired=True=抛 SessionExpiredError（bridge 可识别并置账号 expired）
FAKE: dict = {"status": 7, "error": None, "session_expired": False}
CALLS: list[str] = []      # 假客户端实际收到的调用（调试用）

NEW_ORDER_NO = "RCNEW20260927001"      # createOrder 合成响应的零元单号


def _reset_fake(status: int = 7) -> None:
    FAKE.update(status=status, error=None, session_expired=False)


class FakeClient:
    """按 path 后缀回放最小合成响应（结构与引擎读取字段一致）；未知 path 抛 AssertionError。"""

    def __init__(self, account):
        self.account = account

    # -- 无 wire 样本端点的最小合成 --
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
        if path.endswith("/order/getOrderStatus"):     # 轻探针：data 裸 int
            if FAKE["session_expired"]:
                raise bridge.SessionExpiredError("401", "您的账号已退出登录", "trace-rc")
            if FAKE["error"] is not None:
                raise FAKE["error"]
            return {"errcode": "0", "data": int(FAKE["status"])}
        if path.endswith("/order/cancelOrder"):        # 静态端点无 wire 样本：最小合成成功响应
            return {"errcode": "0", "data": True}
        if path.endswith("/goods/sku/calculatePrice"):
            return {"errcode": "0", "data": {
                "totalSalePrice": "20.00", "totalTradePrice": "20.00",
                "totalGoodsItemPrice": "20.00", "totalDiscountAmount": "0.00"}}
        if path.endswith("/order/settlePrice"):        # 无券试算：buyerRealPrice=0（零元场景）
            return {"errcode": "0", "data": {
                "confirmOrderKey": "ck-rc-offline",
                "tradeFundInfo": {"totalTradePrice": "20", "buyerRealPrice": "0"},
                "assetInfo": {"userCouponInfo": {"availableCouponList": []}},
                "orderGroupList": [{"goodsList": [],
                                    "tradeFundInfo": {"buyerRealPrice": "0"}}]}}
        if path.endswith("/order/createOrder"):        # 零元单：data 仅 orderNo
            return {"errcode": "0", "data": {"orderNo": NEW_ORDER_NO}}
        if path.endswith("/order/getOrderDetail"):
            return {"errcode": "0", "data": {
                "orderNo": NEW_ORDER_NO, "orderStatus": 3, "orderStatusText": "制作中",
                "payAmount": "0", "payTypeText": "免支付", "pickupNo": "RC01",
                "orderPromotions": []}}
        raise AssertionError(f"未预期的 POST 协议调用: {path}")


bridge.build_client = lambda account: FakeClient(account)   # monkeypatch（校准经 bridge.trade_api 命中）

# ---------- 3. 测试数据 ----------

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(SystemUser).filter(SystemUser.username == "viewer_rc").first():
        viewer_role = db.query(Role).filter(Role.name == "viewer").one()
        db.add(SystemUser(username="viewer_rc", display_name="校准只读",
                          password_hash=hash_password("Viewer@123"), role_id=viewer_role.id))
        db.commit()
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800001234").first():
        db.add(ChageeAccount(label="校准冒烟", phone="13800001234", device_uuid="uuid-offline-rc-1",
                             token="fake.token.rc", sk="fakesk", customer_id="1190018250",
                             status="online", group="默认"))
        db.commit()
    ACC_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800001234").one().id

ACC_LABEL = f"校准冒烟#{ACC_ID}"
TOKEN_FP = "fake.token.rc...len=14"

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
    "store_no": "CN03324", "store_name": "福建龙岩新罗万达广场店",
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


def _seed_expired_pending(order_no: str, coupon_code: str, *, account_id: int = None,
                          deadline_min: int = -30, deduction: str = "10",
                          label: str = "校准冒烟") -> None:
    """造一组「超时待支付单」现场：status=1 OrderRecord（pay_deadline=now+deadline_min 分钟）
    + 已使用券档案（bucket=settle_available / last_used_at / last_order_no）+ 一条 success
    的 CouponUsageLog（回滚快照来源）。"""
    account_id = account_id or ACC_ID
    now = datetime.now()
    with database.SessionLocal() as db:
        db.add(OrderRecord(account_id=account_id, order_no=order_no, coupon_code=coupon_code,
                           status=1, status_label="待支付", scenario="partial",
                           total_amount="20", pay_amount="10",
                           pay_deadline=now + timedelta(minutes=deadline_min)))
        db.add(CouponRecord(coupon_code=coupon_code, account_id=account_id,
                            token_fingerprint=TOKEN_FP, template_name="霸王茶姬10元代金券-RC",
                            amount="10", bucket="settle_available", synced_from="settle",
                            last_used_at=now, last_order_no=order_no))
        db.add(CouponUsageLog(coupon_code=coupon_code, coupon_name="霸王茶姬10元代金券-RC",
                              account_id=account_id, account_label=f"{label}#{account_id}",
                              operator="admin", order_no=order_no, deduction=deduction,
                              total_amount="20", pay_amount="10", scenario="partial",
                              result="success"))
        db.commit()


def _cleanup(order_no: str, coupon_code: str, account_ids=()) -> None:
    """删除本测试造的订单/券档案/使用日志（及专属账号），保证各用例互不干扰。"""
    with database.SessionLocal() as db:
        db.query(CouponUsageLog).filter(CouponUsageLog.coupon_code == coupon_code).delete()
        db.query(OrderRecord).filter(OrderRecord.order_no == order_no).delete()
        db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).delete()
        for aid in account_ids:
            acc = db.get(ChageeAccount, aid)
            if acc:
                db.delete(acc)
        db.commit()


# ---------- 4. 测试用例 ----------

def test_timeout_cancelled_rolls_back_coupon():
    """超时单茶姬侧已取消(7)：reconcile 端点 → 订单置取消 + 券回滚 + rolled_back 日志 + 审计。"""
    order_no, coupon = "RC-CANCEL-1", "COUP-CANCEL-1"
    _seed_expired_pending(order_no, coupon)
    _reset_fake(status=7)
    try:
        r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth())
        assert r.status_code == 200, r.text
        rec = r.json()["reconciled"]
        assert rec["scanned"] == 1 and rec["cancelled"] == 1 and rec["skipped"] == 0
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert order.status == 7 and order.status_label == "已取消"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.bucket == "effective" and c.last_used_at is None and c.last_order_no == ""
            log = (db.query(CouponUsageLog)
                     .filter(CouponUsageLog.coupon_code == coupon,
                             CouponUsageLog.result == "rolled_back")
                     .order_by(CouponUsageLog.id.desc()).first())
            assert log is not None
            assert log.order_no == order_no and log.deduction == "10"   # 承接原 success 快照
            assert log.operator == "admin"
            audit = (db.query(AuditLog)
                       .filter(AuditLog.action == "feature.order_reconcile")
                       .order_by(AuditLog.id.desc()).first())
            assert audit is not None
    finally:
        _cleanup(order_no, coupon)


def test_create_not_blocked_after_reconcile():
    """超时单不卡死下单：① 无有效 draft → 400「草稿已过期」（而非 409「存在待支付订单」）；
    ② 有效 draft 的 create → pending 分支内联校准（茶姬 7）放行，下单成功；
    ③ 旧超时单被校准为已取消、券已回滚。"""
    order_no, coupon = "RC-CREATE-1", "COUP-CREATE-1"
    _seed_expired_pending(order_no, coupon)
    _reset_fake(status=7)                      # 茶姬侧已 autoCancel
    try:
        # ① 无有效 draft：命中草稿校验 400（而非待支付单 409）
        with orders_router._draft_lock:
            orders_router._drafts.pop(ACC_ID, None)
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                        json={"draft_id": "d-ghost", "coupon_code": None}, headers=_auth())
        assert r.status_code == 400, r.text
        assert "草稿已过期" in r.json()["detail"]
        # ② 有效 draft 的 create：超时单在 pending 分支先内联校准 → 放行继续下单
        draft_id, _ = _settle()
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                        json={"draft_id": draft_id, "coupon_code": None}, headers=_auth())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["result"] == "zero" and data["order_no"] == NEW_ORDER_NO
        assert data["status"] == 3 and data["status_label"] == "制作中"
        # ③ 旧超时单已被校准为已取消、券使用痕迹已回滚
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert order.status == 7 and order.status_label == "已取消"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.bucket == "effective" and c.last_used_at is None and c.last_order_no == ""
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == coupon,
                              CouponUsageLog.result == "rolled_back").count()) == 1
    finally:
        _cleanup(order_no, coupon)
        with database.SessionLocal() as db:      # create 合成出的新零元单一并清理
            db.query(OrderRecord).filter(OrderRecord.order_no == NEW_ORDER_NO).delete()
            db.commit()


def test_timeout_paid_keeps_coupon_used():
    """超时单茶姬侧已支付(3)：订单回填制作中、券保持已使用（真实核销）、无 rolled_back 日志。"""
    order_no, coupon = "RC-PAID-1", "COUP-PAID-1"
    _seed_expired_pending(order_no, coupon)
    _reset_fake(status=3)
    try:
        r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth())
        assert r.status_code == 200, r.text
        rec = r.json()["reconciled"]
        assert rec["scanned"] == 1 and rec["confirmed"] == 1 and rec["cancelled"] == 0
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert order.status == 3 and order.status_label == "制作中"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.last_order_no == order_no            # 非空：券保持已使用
            assert c.last_used_at is not None
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == coupon,
                              CouponUsageLog.result == "rolled_back").count()) == 0
    finally:
        _cleanup(order_no, coupon)


def test_network_error_skips_and_retries_next_round():
    """getOrderStatus 网络异常：本轮 skipped==1 且数据不动；下一轮恢复后成功回滚。"""
    order_no, coupon = "RC-NETERR-1", "COUP-NETERR-1"
    _seed_expired_pending(order_no, coupon)
    _reset_fake()
    FAKE["error"] = bridge.ChageeError("82041201", "网络异常，请稍后再试", "trace-rc")
    try:
        r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth())
        assert r.status_code == 200, r.text
        rec = r.json()["reconciled"]
        assert rec["scanned"] == 1 and rec["skipped"] == 1 and rec["cancelled"] == 0
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert order.status == 1 and order.status_label == "待支付"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.last_order_no == order_no and c.last_used_at is not None   # 券仍已使用
        # 切换为回放 7：下一轮成功回滚（本轮失败不破坏下轮）
        _reset_fake(status=7)
        r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth())
        assert r.status_code == 200, r.text
        rec = r.json()["reconciled"]
        assert rec["scanned"] == 1 and rec["cancelled"] == 1 and rec["skipped"] == 0
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert order.status == 7 and order.status_label == "已取消"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.bucket == "effective" and c.last_used_at is None and c.last_order_no == ""
    finally:
        _cleanup(order_no, coupon)


def test_not_expired_not_scanned():
    """支付窗内（pay_deadline 未过宽限线）的待支付单不进扫描范围：数据完全不动。"""
    order_no, coupon = "RC-FRESH-1", "COUP-FRESH-1"
    _seed_expired_pending(order_no, coupon, deadline_min=10)    # now+10min
    _reset_fake(status=7)                       # 即使茶姬侧已取消也不应被触碰
    try:
        r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth())
        assert r.status_code == 200, r.text
        rec = r.json()["reconciled"]
        assert rec["scanned"] == 0 and rec["cancelled"] == 0 and rec["details"] == []
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert order.status == 1 and order.status_label == "待支付"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.last_order_no == order_no and c.last_used_at is not None
    finally:
        _cleanup(order_no, coupon)


def test_cancel_endpoint_rolls_back():
    """手动取消端点：cancelOrder 成功 → 订单置取消 + 券回滚 + 响应 coupon_rolled_back==true。"""
    order_no, coupon = "RC-MANUAL-1", "COUP-MANUAL-1"
    _seed_expired_pending(order_no, coupon)
    _reset_fake(status=1)                       # getOrderStatus 无所谓（cancel 不查状态）
    try:
        r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/{order_no}/cancel", headers=_auth())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["coupon_rolled_back"] is True
        assert data["order_no"] == order_no and data["experimental"] is True
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert order.status == 7 and order.status_label == "已取消"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.bucket == "effective" and c.last_used_at is None and c.last_order_no == ""
            log = (db.query(CouponUsageLog)
                     .filter(CouponUsageLog.coupon_code == coupon,
                             CouponUsageLog.result == "rolled_back").first())
            assert log is not None and log.order_no == order_no and log.operator == "admin"
    finally:
        _cleanup(order_no, coupon)


def test_rollback_idempotent():
    """幂等防护：同券同单直调 rollback_coupon_usage 两次 → 仅 1 条 rolled_back 日志，字段仍复位。"""
    order_no, coupon = "RC-IDEM-1", "COUP-IDEM-1"
    _seed_expired_pending(order_no, coupon)
    try:
        with database.SessionLocal() as db:
            assert rollback_coupon_usage(db, coupon, order_no, operator="admin") is True
            assert rollback_coupon_usage(db, coupon, order_no, operator="admin") is True
            logs = (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == coupon,
                              CouponUsageLog.order_no == order_no,
                              CouponUsageLog.result == "rolled_back").all())
            assert len(logs) == 1
            assert logs[0].deduction == "10" and logs[0].order_no == order_no
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.bucket == "effective" and c.last_used_at is None and c.last_order_no == ""
    finally:
        _cleanup(order_no, coupon)


def test_stats_endpoint_counts_rolled_back():
    """券使用日志统计：rolled_back 计数；累计抵扣 = success − rolled_back（下限 0）；result 过滤。"""
    coupon = "COUP-STATS-1"
    with database.SessionLocal() as db:
        db.add(CouponUsageLog(coupon_code=coupon, coupon_name="统计10元代金券",
                              account_id=ACC_ID, account_label=ACC_LABEL, operator="admin",
                              order_no="RC-STATS-S", deduction="10", total_amount="20",
                              pay_amount="10", scenario="partial", result="success"))
        db.add(CouponUsageLog(coupon_code=coupon, coupon_name="统计10元代金券",
                              account_id=ACC_ID, account_label=ACC_LABEL, operator="admin",
                              order_no="RC-STATS-R", deduction="4", total_amount="20",
                              pay_amount="10", scenario="partial", result="rolled_back",
                              fail_reason="订单超时未支付，券状态自动回滚为未使用"))
        db.commit()
    try:
        h = _auth()
        r = CLIENT.get("/api/ops/coupon-usage-logs", params={"keyword": coupon}, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] == 2
        assert d["stats"]["success"] == 1 and d["stats"]["rolled_back"] == 1
        assert Decimal(d["stats"]["total_deduction"]) == Decimal("6.00")    # 10 − 4
        # result=rolled_back 过滤：只返回该条且 result_label 含「已回滚」；
        # 筛选集内只有回滚 → 0 − 4 触发累计抵扣下限 0
        r = CLIENT.get("/api/ops/coupon-usage-logs",
                       params={"keyword": coupon, "result": "rolled_back"}, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] == 1 and len(d["items"]) == 1
        assert d["items"][0]["result"] == "rolled_back"
        assert "已回滚" in d["items"][0]["result_label"]
        assert d["items"][0]["order_no"] == "RC-STATS-R"
        assert d["stats"]["rolled_back"] == 1
        assert Decimal(d["stats"]["total_deduction"]) == Decimal("0")
    finally:
        with database.SessionLocal() as db:
            db.query(CouponUsageLog).filter(CouponUsageLog.coupon_code == coupon).delete()
            db.commit()


def test_reconcile_endpoint_requires_perm():
    """权限门控：无 token → 401；viewer（无 feature:order）→ 403。"""
    assert CLIENT.post("/api/ops/orders/reconcile").status_code == 401
    r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth("viewer_rc", "Viewer@123"))
    assert r.status_code == 403, r.text
    assert CLIENT.get("/api/ops/coupon-usage-logs").status_code == 401


def test_direct_reconcile_order_variants():
    """直调 reconcile_order：still_pending / SessionExpiredError→skipped_error+账号置 expired；
    _scan 层（reconcile_expired_pending_orders 传 db）：skipped_no_account/disabled/no_token。"""
    _reset_fake(status=1)
    # a) 茶姬仍待支付(1) → still_pending，数据不动
    order_no, coupon = "RC-DIRECT-P1", "COUP-DIRECT-P1"
    _seed_expired_pending(order_no, coupon)
    try:
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            account = db.get(ChageeAccount, ACC_ID)
            assert reconcile_order(db, order, account) == "still_pending"
            assert order.status == 1 and order.status_label == "待支付"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.last_order_no == order_no
    finally:
        _cleanup(order_no, coupon)

    # b) SessionExpiredError → skipped_error，账号顺手置 expired，订单/券数据不动
    with database.SessionLocal() as db:
        acc2 = ChageeAccount(label="凭证失效", phone="13800009999", device_uuid="uuid-rc-exp-1",
                             token="expired.token.value", status="online")
        db.add(acc2)
        db.commit()
        acc2_id = acc2.id
    order_no, coupon = "RC-DIRECT-E1", "COUP-DIRECT-E1"
    _seed_expired_pending(order_no, coupon, account_id=acc2_id, label="凭证失效")
    _reset_fake(status=7)
    FAKE["session_expired"] = True
    try:
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            account = db.get(ChageeAccount, acc2_id)
            assert reconcile_order(db, order, account) == "skipped_error"
            assert account.status == "expired"          # 凭证失效已回写
            assert order.status == 1                    # 订单数据不动
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon).one()
            assert c.last_order_no == order_no          # 券数据不动
    finally:
        _cleanup(order_no, coupon, account_ids=(acc2_id,))
        _reset_fake()

    # c) _scan 层三分支：无账号 / 停用 / 无 token（均不动数据，details 逐单标注）
    with database.SessionLocal() as db:
        acc_dis = ChageeAccount(label="停用账号", phone="13800008888", device_uuid="uuid-rc-dis-1",
                                token="t-disabled", status="disabled")
        acc_notok = ChageeAccount(label="无token账号", phone="13800007777",
                                  device_uuid="uuid-rc-notok-1", token="", status="online")
        db.add_all([acc_dis, acc_notok])
        db.commit()
        dis_id, notok_id = acc_dis.id, acc_notok.id
        past = datetime.now() - timedelta(minutes=30)
        scan_orders = [("RC-SCAN-NOACC", 999999), ("RC-SCAN-DIS", dis_id),
                       ("RC-SCAN-NOTOK", notok_id)]
        for no, aid in scan_orders:
            db.add(OrderRecord(account_id=aid, order_no=no, status=1,
                               status_label="待支付", pay_deadline=past))
        db.commit()
    try:
        with database.SessionLocal() as db:
            summary = reconcile_expired_pending_orders(db, operator="tester")
            assert summary["scanned"] == 3 and summary["skipped"] == 3
            assert summary["cancelled"] == 0 and summary["confirmed"] == 0
            by_no = {d["order_no"]: d["result"] for d in summary["details"]}
            assert by_no == {"RC-SCAN-NOACC": "skipped_no_account",
                             "RC-SCAN-DIS": "skipped_disabled",
                             "RC-SCAN-NOTOK": "skipped_no_token"}
            for no, _ in scan_orders:
                assert db.query(OrderRecord).filter(OrderRecord.order_no == no).one().status == 1
    finally:
        with database.SessionLocal() as db:
            db.query(OrderRecord).filter(OrderRecord.order_no.in_(
                [no for no, _ in scan_orders])).delete(synchronize_session=False)
            db.query(ChageeAccount).filter(ChageeAccount.id.in_([dis_id, notok_id]))\
                .delete(synchronize_session=False)
            db.commit()


def test_start_reconcile_thread_disabled():
    """CHAGEE_RECONCILE_INTERVAL_SECONDS<=0：start_reconcile_thread 不置标志、不创建线程。"""
    original = reconcile_module._thread_started
    reconcile_module._thread_started = False
    os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
    try:
        start_reconcile_thread()
        assert reconcile_module._thread_started is False     # 未置启动标志
        assert not any(t.name == "order-reconcile" for t in threading.enumerate())
    finally:
        reconcile_module._thread_started = original
        os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"


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
