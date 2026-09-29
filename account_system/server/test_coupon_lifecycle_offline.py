"""券生命周期自动收口离线测试（FakeClient 回放，零真实网络请求）。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server && python test_coupon_lifecycle_offline.py
    （或 pytest test_coupon_lifecycle_offline.py -q）

覆盖（契约 §17：核销迁移历史桶 + 登录自动同步券入库；§18：使用日志状态机）：
  - confirm_coupon_usage 单元：settle_available→historical、使用痕迹保留、券不存在 False、
    不新增 CouponUsageLog（无双算）、幂等双调安全
  - 三条支付发现通道核销迁移：手动 reconcile 端点 paid 分支、pay-watcher watch_once、
    H5 收银台 /status 探针
  - 登录自动同步券入库：POST /api/accounts/{id}/login → F4 两列表全量落库
    + coupons_synced 字段；同步失败容错（登录仍成功、coupons_synced=null、账号仍 online）
  - dashboard_push 指纹联动：核销迁移桶位后 push_once 触发推送
  - §18 差额单全链路：settle→create 预记 pending → 支付确认原地迁 success / 超时取消
    原地迁 rolled_back（单行、金额保留、轨迹入 state_history、统计口径只计已核销）
  - §18 历史回填：旧双行合并 + 误记 success 按订单实况校正 + 幂等

要点（与 test_reconcile_offline.py / test_payportal_offline.py 同模式）：
  - 先把 database.DB_PATH 指向 data/test_coupon_lifecycle.db 并重建 engine，再 import app
  - 置 CHAGEE_RECONCILE_INTERVAL_SECONDS / CHAGEE_PAYWATCH_INTERVAL_SECONDS /
    CHAGEE_DASHBOARD_PUSH_INTERVAL_SECONDS = 0 禁后台线程（push_once 直调断言）
  - monkeypatch services.chagee_bridge 的 build_client / proto_login_sms / read_session_file：
    未知 path 抛 AssertionError（防越权端点）
"""

import json
import os
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根
sys.path.insert(0, BASE)

# 禁用订单校准线程 + 支付 watcher（TestClient startup 挂载，置 0 不创建后台线程）
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
# 禁用仪表盘推送线程（本测试直调 push_once 断言指纹逻辑）
os.environ["CHAGEE_DASHBOARD_PUSH_INTERVAL_SECONDS"] = "0"
# 禁用 frida 收银台铸造 + 跨进程状态广播（杜绝真触云手机/真发 HTTP 到 8010）
os.environ["CHAGEE_MINT_ENABLED"] = "0"
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
# 日志隔离：oplog 日志库与文本日志均指向测试路径，避免污染生产 data/logs/
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_coupon_lifecycle_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_coupon_lifecycle.db")
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
from models import (AuditLog, ChageeAccount, CouponRecord, CouponUsageLog,  # noqa: E402
                    LoginTicket, OrderRecord, PayAttempt, PayEventLog, PaySession, SystemUser)
from routers import payportal as payportal_router  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import dashboard_push, payment_events as pe  # noqa: E402
from services import pay_session as ps  # noqa: E402
from services.order_reconcile import confirm_coupon_usage  # noqa: E402

# ---------- 2. FakeClient（替换 build_client，杜绝真实网络） ----------

# getOrderStatus 回放裸 int；getOrderDetail 回放 FAKE["detail"]；coupon_fail=True 时
# F4 券两列表抛 RuntimeError（登录同步容错用例的失败注入）
FAKE: dict = {
    "status": 3,
    "error": None,
    "detail": {"orderNo": "", "orderStatus": 3, "orderStatusText": "制作中",
               "payAmount": "10.00", "totalAmount": "20.00", "payTypeText": "支付宝",
               "pickupNo": "PW01", "orderPromotions": []},
    "coupon_fail": False,
}
CALLS: list[str] = []      # 假客户端实际收到的调用（零网络断言用）

# 登录同步用 F4 两列表（同码不重叠：可用 2 张 + 历史 1 张）
LOGIN_EFFECTIVE = {
    "C-LOGIN-E1": {"couponCode": "C-LOGIN-E1", "templateName": "霸王茶姬20元代金券-LG",
                   "bizType": 1, "benefitText": "20元", "benefit2Text": "代金", "status": 10,
                   "useStartTime": 1790092800000, "useEndTime": 4102444800000,
                   "usableScenes": [2, 6]},
    "C-LOGIN-E2": {"couponCode": "C-LOGIN-E2", "templateName": "免5杯多次卡-LG",
                   "bizType": 1, "benefitText": "免5杯", "benefit2Text": "", "status": 10,
                   "useStartTime": 1790092800000, "useEndTime": 4102444800000,
                   "usableScenes": [2]},
}
LOGIN_HISTORICAL = {
    "C-LOGIN-H1": {"couponCode": "C-LOGIN-H1", "templateName": "已使用10元代金券-LG",
                   "bizType": 1, "benefitText": "10元", "benefit2Text": "代金", "status": 10,
                   "useStartTime": 1790092800000, "useEndTime": 1792684799000,
                   "usableScenes": [62]},
}

# partial 全链路用：settle 回放的可用券 + createOrder 回放的差额单号
PART_ORDER_NO = "CLPART20260930001"
CL_PART_COUPON = {"couponCode": "CL-PART-C1", "templateName": "券生命周期10元代金券",
                  "benefitText": "10元", "benefit2Text": "", "thresholdTips": "",
                  "useEndTime": 4102444800000, "canDiscount": True}   # 2100 年：永不过期

SETTLE_BODY = {
    "store_no": "CN03324", "store_name": "福建龙岩新罗万达广场店",
    "spu_id": "625339451983278080", "spu_name": "伯牙绝弦",
    "sku_id": "653632618000097282", "sku_name": "伯牙绝弦",
    "item_sku_id": "653632618000097282", "quantity": 1, "sale_price": 20.0,
    "spec_list": [{"specId": "653599312273510400", "specOptionId": "653599312273510402"}],
    "image_url": "", "spu_type": "stand",
}


def _reset_fake(status: int = 3, pickup_no: str = "PW01") -> None:
    detail = dict(FAKE["detail"], orderStatus=status, pickupNo=pickup_no)
    FAKE.update(status=status, error=None, detail=detail, coupon_fail=False)


def _order_str(out_trade_no: str, amount: str = "10.00", expire_at: str = None) -> str:
    """构造 alipay.trade.app.pay 形态签名串（parse_order_str/parse_pay_payload 可解析）。"""
    expire_at = expire_at or time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(time.time() + 590))
    biz = {"out_trade_no": out_trade_no, "total_amount": amount, "subject": "霸王茶姬",
           "product_code": "QUICK_MSECURITY_PAY", "time_expire": expire_at}
    return ("alipay_sdk=alipay-sdk-java-4.9.28.ALL&app_id=202100117&charset=utf-8"
            "&biz_content=" + urllib.parse.quote(json.dumps(biz, ensure_ascii=False))
            + "&sign=OFFLINETESTSIGN&sign_type=RSA2")


def _link(order_no: str, *, amount: str = "10.00", expire_in: float = 590) -> bridge.PayLink:
    """构造测试 PayLink（不经任何网络）。"""
    expire_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + expire_in))
    out = f"OT-{order_no[-6:]}"
    return bridge.PayLink(order_no=order_no, pay_no=f"PN-{order_no[-6:]}",
                          order_str=_order_str(out, amount, expire_at),
                          out_trade_no=out, total_amount=amount, expire_at=expire_at)


class FakeClient:
    """按 path 后缀回放最小合成响应（结构与引擎读取字段一致）；未知 path 抛 AssertionError。"""

    def __init__(self, account):
        self.account = account
        self.token = account.token or ""
        self.proto = type("P", (), {})()          # SimpleNamespace 等价物
        self.proto.sk = account.sk or "fakesk"
        self.proto.token = self.token

    def ensure_sk(self):                          # 登录流程前置：离线 no-op
        pass

    def get(self, path, **kw):
        CALLS.append(path)
        if path.endswith("/customer/userInfo/query"):
            return {"errcode": "0", "data": {"customerId": "1190018250",
                                             "mobileEncrypt": "AESxFAKECL", "nickName": "离线券生命周期"}}
        raise AssertionError(f"未预期的 GET 协议调用: {path}")

    def whoami(self):
        return self.get("/user-client/customer/userInfo/query")

    def post(self, path, body=None, **kw):
        CALLS.append(path)
        if path.endswith("/order/getOrderStatus"):     # 轻探针：data 裸 int
            if FAKE["error"] is not None:
                raise FAKE["error"]
            return {"errcode": "0", "data": int(FAKE["status"])}
        if path.endswith("/order/getOrderDetail"):
            d = dict(FAKE["detail"])
            d["orderNo"] = str((body or {}).get("orderNo") or d.get("orderNo") or "")
            return {"errcode": "0", "data": d}
        if path.endswith("/goods/sku/calculatePrice"):  # settle 前置复算：20 元无折
            return {"errcode": "0", "data": {
                "totalSalePrice": "20.00", "totalTradePrice": "20.00",
                "totalGoodsItemPrice": "20.00", "totalDiscountAmount": "0.00"}}
        if path.endswith("/order/settlePrice"):        # 有券试算：差额 10 元（partial 场景）
            return {"errcode": "0", "data": {
                "confirmOrderKey": "ck-cl-part",
                "tradeFundInfo": {"totalTradePrice": "20.00", "buyerRealPrice": "10.00",
                                  "totalDiscountAmount": "10.00"},
                "assetInfo": {"userCouponInfo": {"availableCouponList": [CL_PART_COUPON]}},
                "orderGroupList": [{"tradeFundInfo": {"buyerRealPrice": "10.00"}}],
                "discountList": [{"discountId": CL_PART_COUPON["couponCode"],
                                  "discountName": CL_PART_COUPON["templateName"],
                                  "discountSource": 1, "discountType": 1, "scopeType": 2,
                                  "discountAmount": "10.00", "currentSelect": None}]}}
        if path.endswith("/order/createOrder"):        # 差额单：payUrl 内嵌 orderStr
            return {"errcode": "0", "data": {
                "orderNo": PART_ORDER_NO, "payNo": "PN-CLPART",
                "payUrl": json.dumps({"requestJson": {"orderStr":
                    f"out_trade_no=OT-CLPART&total_amount=10.00&biz_content=" +
                    urllib.parse.quote(json.dumps({"out_trade_no": "OT-CLPART",
                                                   "total_amount": "10.00",
                                                   "time_expire": "2099-01-01 00:00:00"}))}})}}
        if path.endswith("/user-coupon/effective-list"):
            if FAKE["coupon_fail"]:
                raise RuntimeError("离线模拟 F4 券查询失败")
            return {"errcode": "0", "data": {"pageList": list(LOGIN_EFFECTIVE.values()),
                                             "total": len(LOGIN_EFFECTIVE)}}
        if path.endswith("/user-coupon/historical-list"):
            if FAKE["coupon_fail"]:
                raise RuntimeError("离线模拟 F4 券查询失败")
            return {"errcode": "0", "data": {"pageList": list(LOGIN_HISTORICAL.values()),
                                             "total": len(LOGIN_HISTORICAL)}}
        raise AssertionError(f"未预期的 POST 协议调用: {path}")


bridge.build_client = lambda account: FakeClient(account)   # monkeypatch（watcher/校准/登录经此构造）
bridge.proto_login_sms = lambda client, phone, code: "0"    # 登录协议：离线直接成功
bridge.read_session_file = lambda account_id: {"token": f"login-tok-{account_id}",
                                               "sk": "login-sk"}   # 隔离 session 读回

# ---------- 3. 测试数据 ----------

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800009001").first():
        db.add(ChageeAccount(label="券生命周期冒烟", phone="13800009001",
                             device_uuid="uuid-offline-cl-1", token="fake.token.cl",
                             sk="fakesk", customer_id="1190018250", status="online",
                             group="默认"))
        db.commit()
    ACC_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800009001").one().id

ACC_LABEL = f"券生命周期冒烟#{ACC_ID}"
TOKEN_FP = "fake.token.cl...len=14"

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


def _seed_coupon(coupon_code: str, bucket: str = "settle_available") -> None:
    """已使用态券档案（bucket 可指定；last_used_at/last_order_no 与成单预记一致）。"""
    with database.SessionLocal() as db:
        db.add(CouponRecord(coupon_code=coupon_code, account_id=ACC_ID,
                            token_fingerprint=TOKEN_FP, template_name=f"券-{coupon_code}",
                            amount="10", bucket=bucket, synced_from="settle",
                            last_used_at=datetime.now(), last_order_no="SEED-ORDER"))
        db.commit()


def _seed_partial_with_coupon(order_no: str, coupon_code: str,
                              *, deadline_min: int = -30) -> str:
    """造一组「差额待支付单 + 有券支付会话」现场，返回 pay_token。"""
    now = datetime.now()
    with database.SessionLocal() as db:
        db.add(OrderRecord(account_id=ACC_ID, order_no=order_no, coupon_code=coupon_code,
                           status=1, status_label="待支付", scenario="partial",
                           total_amount="20", pay_amount="10",
                           pay_deadline=now + timedelta(minutes=deadline_min)))
        db.add(CouponRecord(coupon_code=coupon_code, account_id=ACC_ID,
                            token_fingerprint=TOKEN_FP, template_name=f"券-{coupon_code}",
                            amount="10", bucket="settle_available", synced_from="settle",
                            last_used_at=now, last_order_no=order_no))
        db.add(CouponUsageLog(coupon_code=coupon_code, coupon_name=f"券-{coupon_code}",
                              account_id=ACC_ID, account_label=ACC_LABEL, operator="admin",
                              order_no=order_no, deduction="10", total_amount="20",
                              pay_amount="10", scenario="partial", result="pending"))
        db.commit()
        return ps.ensure_pay_session(db, ACC_ID, order_no, _link(order_no),
                                     coupon_code=coupon_code).pay_token


def _wipe(order_no: str, coupon_code: str) -> None:
    """删除本测试造的订单/券档案/使用日志/支付会话，并清进程内探针计数。"""
    with database.SessionLocal() as db:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).first()
        if sess:
            db.query(PayAttempt).filter(PayAttempt.pay_session_id == sess.id).delete()
            db.delete(sess)
        db.query(PayEventLog).filter(PayEventLog.order_no == order_no).delete()
        db.query(CouponUsageLog).filter(CouponUsageLog.coupon_code == coupon_code).delete()
        db.query(OrderRecord).filter(OrderRecord.order_no == order_no).delete()
        db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).delete()
        db.commit()
    payportal_router._last_probe.pop(order_no, None)
    pe._pending_streak.pop(order_no, None)


# ---------- 4. 测试用例 ----------

def test_01_confirm_coupon_usage_unit():
    """单元：迁移 historical + 痕迹保留 + 券不存在 False + 不新增使用日志 + 幂等双调。"""
    coup = "CL-UNIT-1"
    _seed_coupon(coup)
    try:
        with database.SessionLocal() as db:
            assert confirm_coupon_usage(db, coup, "CL-ORDER-1") is True
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coup).one()
            assert c.bucket == "historical"
            assert c.last_used_at is not None and c.last_order_no == "SEED-ORDER"  # 痕迹保留
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == coup).count()) == 0    # 无双算
            assert confirm_coupon_usage(db, coup, "CL-ORDER-1") is True            # 幂等双调
            assert confirm_coupon_usage(db, "CL-NOSUCH", "CL-ORDER-1") is False    # 券不存在
            c2 = db.query(CouponRecord).filter(CouponRecord.coupon_code == coup).one()
            assert c2.bucket == "historical"
    finally:
        _wipe("CL-ORDER-1", coup)


def test_02_reconcile_paid_marks_historical():
    """校准通道：过期待支付单茶姬侧已支付(3) → 订单回填 + 券迁移历史桶 + 无 rolled_back。"""
    order_no, coup = "CL-REC-1", "CL-REC-C1"
    _seed_partial_with_coupon(order_no, coup)
    _reset_fake(status=3)
    try:
        r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth())
        assert r.status_code == 200, r.text
        rec = r.json()["reconciled"]
        assert rec["scanned"] == 1 and rec["confirmed"] == 1 and rec["cancelled"] == 0
        with database.SessionLocal() as db:
            order = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert order.status == 3
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coup).one()
            assert c.bucket == "historical"                     # 核销迁移
            assert c.last_used_at is not None and c.last_order_no == order_no
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == coup,
                              CouponUsageLog.result == "rolled_back").count()) == 0
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == coup,
                              CouponUsageLog.result == "success").count()) == 1     # 无双算
    finally:
        _reset_fake()
        _wipe(order_no, coup)


def test_03_watcher_paid_marks_historical():
    """pay-watcher 通道：watch_once 探得已支付(3) → 会话 paid + 券迁移历史桶。"""
    order_no, coup = "CL-WATCH-1", "CL-WATCH-C1"
    tok = _seed_partial_with_coupon(order_no, coup)
    assert tok
    _reset_fake(status=3, pickup_no="PW01")
    orig_path = pe.CALLBACK_CONFIG_PATH
    pe.CALLBACK_CONFIG_PATH = os.path.join(tempfile.gettempdir(), "cl_no_such_callback.json")
    try:                                                    # 回调配置不存在 → 分派 no-op
        with database.SessionLocal() as db:
            stats = pe.watch_once(db)
        assert stats["paid"] == 1
        with database.SessionLocal() as db:
            s = db.query(PaySession).filter(PaySession.order_no == order_no).one()
            assert s.status == "paid" and s.pickup_no == "PW01"
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coup).one()
            assert c.bucket == "historical"
    finally:
        pe.CALLBACK_CONFIG_PATH = orig_path
        _reset_fake()
        _wipe(order_no, coup)


def test_04_h5_probe_paid_marks_historical():
    """H5 收银台探针通道：/status 探得已支付(3) → 会话 paid + 券迁移历史桶。"""
    order_no, coup = "CL-PROBE-1", "CL-PROBE-C1"
    tok = _seed_partial_with_coupon(order_no, coup)
    _reset_fake(status=3, pickup_no="PW01")
    try:
        r = CLIENT.get(f"/pay/{tok}/status")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paid"
        with database.SessionLocal() as db:
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coup).one()
            assert c.bucket == "historical"
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
            assert rec.status == 3 and rec.pickup_no == "PW01"
    finally:
        _reset_fake()
        _wipe(order_no, coup)


def test_05_login_syncs_coupons():
    """登录自动同步：登录成功 → F4 两列表全量入库 + coupons_synced 字段 + 账号 online。"""
    with database.SessionLocal() as db:
        acc = ChageeAccount(label="登录同步专用", phone="13800009002",
                            device_uuid="uuid-offline-cl-2", token="", sk="",
                            status="pending", group="默认")
        db.add(acc)
        db.flush()
        db.add(LoginTicket(account_id=acc.id, phone=acc.phone, stage="wait_code",
                           expires_at=datetime.now() + timedelta(minutes=5)))
        db.commit()
        aid = acc.id
    try:
        r = CLIENT.post(f"/api/accounts/{aid}/login", json={"code": "123456"}, headers=_auth())
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True and d["status"] == "online"
        assert d["coupons_synced"]["total"] == 3
        assert d["coupons_synced"]["effective"] == 2
        assert d["coupons_synced"]["historical"] == 1
        with database.SessionLocal() as db:
            acc = db.get(ChageeAccount, aid)
            assert acc.status == "online" and acc.token == f"login-tok-{aid}"
            recs = {c.coupon_code: c for c in db.query(CouponRecord)
                    .filter(CouponRecord.account_id == aid).all()}
            assert set(recs) == {"C-LOGIN-E1", "C-LOGIN-E2", "C-LOGIN-H1"}
            assert recs["C-LOGIN-E1"].bucket == "effective"
            assert recs["C-LOGIN-H1"].bucket == "historical"
            assert recs["C-LOGIN-E1"].synced_from == "coupon_query"
            assert recs["C-LOGIN-E2"].usable_scenes == "自取"
            audits = db.query(AuditLog).filter(AuditLog.action == "account.login",
                                               AuditLog.target == f"登录同步专用#{aid}").all()
            assert audits and json.loads(audits[-1].detail).get("coupons_synced") == 3
    finally:
        with database.SessionLocal() as db:
            db.query(LoginTicket).filter(LoginTicket.account_id == aid).delete()
            db.query(CouponRecord).filter(CouponRecord.account_id == aid).delete()
            db.delete(db.get(ChageeAccount, aid))
            db.commit()


def test_06_login_coupon_sync_failure_tolerated():
    """同步失败容错：F4 抛异常 → 登录仍成功、coupons_synced=null、账号仍 online。"""
    with database.SessionLocal() as db:
        acc = ChageeAccount(label="登录失败容错", phone="13800009003",
                            device_uuid="uuid-offline-cl-3", token="", sk="",
                            status="pending", group="默认")
        db.add(acc)
        db.flush()
        db.add(LoginTicket(account_id=acc.id, phone=acc.phone, stage="wait_code",
                           expires_at=datetime.now() + timedelta(minutes=5)))
        db.commit()
        aid = acc.id
    FAKE["coupon_fail"] = True
    try:
        r = CLIENT.post(f"/api/accounts/{aid}/login", json={"code": "123456"}, headers=_auth())
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True and d["status"] == "online"
        assert d["coupons_synced"] is None
        with database.SessionLocal() as db:
            assert db.get(ChageeAccount, aid).status == "online"
            assert (db.query(CouponRecord)
                      .filter(CouponRecord.account_id == aid).count()) == 0
    finally:
        _reset_fake()
        with database.SessionLocal() as db:
            db.query(LoginTicket).filter(LoginTicket.account_id == aid).delete()
            db.delete(db.get(ChageeAccount, aid))
            db.commit()


def test_07_dashboard_push_after_confirm():
    """指纹联动：桶位迁移改变统计 → push_once 触发一次推送（SSE 零改动联动依据）。"""
    coup = "CL-PUSH-1"
    _seed_coupon(coup)
    try:
        assert dashboard_push.push_once() is True          # 进程内首推建立基线
        assert dashboard_push.push_once() is False         # 无变化 → 不重复推
        before = dashboard_push.latest_stats()["cards"]["coupons_historical"]
        with database.SessionLocal() as db:
            confirm_coupon_usage(db, coup, "CL-PUSH-ORDER-1")
        assert dashboard_push.push_once() is True          # 桶位迁移 → 推送
        after = dashboard_push.latest_stats()["cards"]["coupons_historical"]
        assert after == before + 1
        assert dashboard_push.push_once() is False         # 回归静默
    finally:
        _wipe("CL-PUSH-ORDER-1", coup)


def _create_partial_with_coupon():
    """走真实 settle → create 链路造一笔差额单（带券），返回 None（订单号/券码用模块常量）。"""
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle", json=SETTLE_BODY, headers=_auth())
    assert r.status_code == 200, f"settle 失败: {r.status_code} {r.text}"
    draft_id = r.json()["draft_id"]
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": draft_id, "coupon_code": CL_PART_COUPON["couponCode"]},
                    headers=_auth())
    assert r.status_code == 200, f"create 失败: {r.status_code} {r.text}"
    assert r.json()["result"] == "partial" and r.json()["order_no"] == PART_ORDER_NO


def _backdate_deadline(order_no: str, minutes: int = -30) -> None:
    """把订单支付截止回拨到宽限线之前，让校准线程纳入扫描范围。"""
    with database.SessionLocal() as db:
        rec = db.query(OrderRecord).filter(OrderRecord.order_no == order_no).one()
        rec.pay_deadline = datetime.now() + timedelta(minutes=minutes)
        db.commit()


def test_08_partial_create_pending_then_paid():
    """§18 全链路·转正：差额单成单记 pending → 支付确认原地迁 success（历史入轨迹）+ 统计口径。"""
    try:
        _create_partial_with_coupon()
        with database.SessionLocal() as db:
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == PART_ORDER_NO).one()
            assert rec.status == 1 and rec.coupon_code == CL_PART_COUPON["couponCode"]
            log = db.query(CouponUsageLog).filter(
                CouponUsageLog.coupon_code == CL_PART_COUPON["couponCode"]).one()
            assert log.result == "pending" and log.order_no == PART_ORDER_NO
            assert log.deduction == "10.00"
            assert json.loads(log.state_history or "[]") == []          # 成单时无流转
        _backdate_deadline(PART_ORDER_NO)
        _reset_fake(status=3)
        r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth())
        assert r.status_code == 200 and r.json()["reconciled"]["confirmed"] == 1, r.text
        with database.SessionLocal() as db:
            log = db.query(CouponUsageLog).filter(
                CouponUsageLog.coupon_code == CL_PART_COUPON["couponCode"]).one()
            assert log.result == "success"                              # 原地迁移（行数不变）
            hist = json.loads(log.state_history or "[]")
            assert len(hist) == 1 and hist[0]["from"] == "pending" and hist[0]["to"] == "success"
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == CL_PART_COUPON["couponCode"])
                      .count()) == 1
        d = CLIENT.get("/api/ops/coupon-usage-logs",
                       params={"keyword": CL_PART_COUPON["couponCode"]}, headers=_auth()).json()
        assert d["stats"]["success"] == 1 and d["stats"]["pending"] == 0
        assert Decimal(d["stats"]["total_deduction"]) == Decimal("10.00")   # 仅已核销计入
        assert d["items"][0]["state_history"] and d["items"][0]["result"] == "success"
    finally:
        _reset_fake()
        _wipe(PART_ORDER_NO, CL_PART_COUPON["couponCode"])
        with database.SessionLocal() as db:
            db.query(OrderRecord).filter(OrderRecord.order_no == PART_ORDER_NO).delete()
            db.commit()


def test_09_partial_create_pending_then_cancelled():
    """§18 全链路·回退：差额单 pending → 超时取消原地迁 rolled_back（单行、金额保留、不计抵扣）。"""
    try:
        _create_partial_with_coupon()
        _backdate_deadline(PART_ORDER_NO)
        _reset_fake(status=7)
        r = CLIENT.post("/api/ops/orders/reconcile", headers=_auth())
        assert r.status_code == 200 and r.json()["reconciled"]["cancelled"] == 1, r.text
        with database.SessionLocal() as db:
            logs = (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == CL_PART_COUPON["couponCode"]).all())
            assert len(logs) == 1 and logs[0].result == "rolled_back"  # 原地迁移不加行
            assert logs[0].deduction == "10.00" and logs[0].order_no == PART_ORDER_NO
            hist = json.loads(logs[0].state_history or "[]")
            assert any(h["from"] == "pending" and h["to"] == "rolled_back" for h in hist)
        d = CLIENT.get("/api/ops/coupon-usage-logs",
                       params={"keyword": CL_PART_COUPON["couponCode"]}, headers=_auth()).json()
        assert d["stats"]["success"] == 0 and d["stats"]["rolled_back"] == 1
        assert Decimal(d["stats"]["total_deduction"]) == Decimal("0")   # 未核销不计抵扣
    finally:
        _reset_fake()
        _wipe(PART_ORDER_NO, CL_PART_COUPON["couponCode"])
        with database.SessionLocal() as db:
            db.query(OrderRecord).filter(OrderRecord.order_no == PART_ORDER_NO).delete()
            db.commit()


def test_10_backfill_migration():
    """§18 历史回填：旧双行合并 + 误记 success 按订单实况校正（rolled_back/pending）+ 幂等。"""
    codes = ["CL-BF-DUAL", "CL-BF-CANC", "CL-BF-WAIT", "CL-BF-NOORD", "CL-BF-PAID"]
    with database.SessionLocal() as db:
        now = datetime.now()
        # 旧双行形态：success + rolled_back 同券同单
        for result, deduction in (("success", "10"), ("rolled_back", "10")):
            db.add(CouponUsageLog(coupon_code=codes[0], coupon_name="回填双行",
                                  account_id=ACC_ID, account_label=ACC_LABEL, operator="admin",
                                  order_no="CL-BF-O1", deduction=deduction, total_amount="20",
                                  pay_amount="10", scenario="partial", result=result))
        # 误记 success + 订单已取消(7) / 仍待支付(1) / 订单缺失 / 已支付(6)
        for code, order_no, st in ((codes[1], "CL-BF-O2", 7), (codes[2], "CL-BF-O3", 1),
                                   (codes[3], "CL-BF-O4", None), (codes[4], "CL-BF-O5", 6)):
            db.add(CouponUsageLog(coupon_code=code, coupon_name="回填单行",
                                  account_id=ACC_ID, account_label=ACC_LABEL, operator="admin",
                                  order_no=order_no, deduction="8", total_amount="20",
                                  pay_amount="12", scenario="partial", result="success"))
            if st is not None:
                db.add(OrderRecord(account_id=ACC_ID, order_no=order_no, status=st,
                                   status_label="x", scenario="partial", total_amount="20",
                                   pay_amount="12", pay_deadline=now))
        db.commit()
    try:
        seed._migrate_usage_log_lifecycle()
        seed._migrate_usage_log_lifecycle()          # 幂等：二跑不改结果
        with database.SessionLocal() as db:
            dual = db.query(CouponUsageLog).filter(CouponUsageLog.coupon_code == codes[0]).all()
            assert len(dual) == 1 and dual[0].result == "rolled_back"   # 合并为单行
            assert dual[0].deduction == "10"
            assert any(h["to"] == "rolled_back" for h in json.loads(dual[0].state_history or "[]"))
            results = {db.query(CouponUsageLog).filter(CouponUsageLog.coupon_code == c).one().result
                       for c in codes[1:]}
            assert results == {"rolled_back", "pending", "success", "success"}
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == codes[1]).one().result == "rolled_back")
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == codes[2]).one().result == "pending")
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == codes[3]).one().result == "success")  # 订单缺失不动
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == codes[4]).one().result == "success")  # 已支付不动
    finally:
        with database.SessionLocal() as db:
            db.query(CouponUsageLog).filter(CouponUsageLog.coupon_code.in_(codes)).delete()
            db.query(OrderRecord).filter(OrderRecord.order_no.like("CL-BF-O%")).delete()
            db.commit()


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:                              # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {name}: {e}")
            traceback.print_exc()
    print(f"---- {len(tests) - failed}/{len(tests)} passed ----")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
