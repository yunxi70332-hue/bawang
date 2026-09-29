"""H5 收银台（payportal / pay_session / payment_events）离线回归测试（FakeClient 回放，零真实网络）。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server && python test_payportal_offline.py
    （或 pytest test_payportal_offline.py -q；须单独跑，不与其它套件合跑——engine 互踩是既定约定）

覆盖（编号即用例名前缀，main() 按字典序执行）：
  01 建表/迁移：pay_sessions/pay_attempts/pay_event_logs 表 + order_records.order_target
     + pay_sessions.alipay_cashier_url 列
  02 会话核心：ensure_pay_session 新建（token≈43 字符且唯一 / PayAttempt 1 行 / link_issued 事件）
     与续铸（token 不变 / PayAttempt 2 行 / remint 事件）
  03 mark_session CAS（同态再 mark 返回 False / 白名单外字段不生效）+ is_active/remaining_seconds 边界
  04 alipay_cashier_url：临时配置文件（重定向 __file__ 产出的 data 路径）→ mclient.alipay.com 形态 URL；
     空配置→None；坏 order_str→None（fail-soft 不抛）
  05 pay_link_payload_with_session：h5_url=官方收银台（无配置回退 portal 壳页）、
     portal_url=<base>/pay/<token>、note 含「支付宝收银台」；CHAGEE_PAY_BASE_URL 环境变量生效
  06 HTTP 公开路由：GET /pay/{token} 页面壳；info 200 全字段断言 + page_opened 事件
  07 乱 token info/status 404；status 本地态返回（不活跃/节流窗口内不触发远程探针）
  08 remint 409（已支付会话不可续付）
  09 状态跃迁：FakeClient getOrderStatus→3 + getOrderDetail→PW01 → GET /status 探针 → paid 收口
  10 状态跃迁：→7 已取消 + 有券 → cancelled 收口 + rolled_back 事件 + CouponRecord 复位
  11 switch-full-price：取消旧单原价重下（旧会话 cancelled / 新 OrderRecord 无券 / 新会话 token /
     switch_full_price 事件 / 券回滚）
  12 switch-full-price 拒绝：缺商品快照→400；非券差额单→400
  13 watch_once（直调不启线程）：paid CAS 收口 + 事件链 + 伪用户审计（feature.pay_watcher）；
     重复 watch 不重复收口
  14 pay-events 管理端点：admin JWT 分页/过滤字段；无 token 401
  15 回调分派成功：本地 http.server 接收 + X-CHAGEE-Signature hmac 复算验签 + callback_dispatched 事件
  16 回调分派失败：无人监听端口 → 1+3 次尝试（退避清零控时）→ callback_failed 事件链 retries_left 3→0
  17 线程不启动：CHAGEE_*_INTERVAL_SECONDS=0 时 start_pay_watcher/start_reconcile_thread 不建线程
  18 零网络保障：全程仅触达回放覆盖的协议端点（未知 path 由 FakeClient 抛 AssertionError 拦截）
  19 MINT_ENABLED=0：pay_link_payload_with_session 正常返回且无 mint 事件（铸造触发被开关旁路）
  20 GET /orders/{order_no}/cashier 端点：无 token 401；有会话 200 四字段
     （order_no/alipay_cashier_url/session_status/note）；回填后透出直链；无会话 404

要点（与 test_orders_offline.py / test_reconcile_offline.py 同模式）：
  - 先把 database.DB_PATH 指向 data/test_payportal.db 并重建 engine，再 import app
  - import app 之前置 CHAGEE_RECONCILE_INTERVAL_SECONDS=0 且 CHAGEE_PAYWATCH_INTERVAL_SECONDS=0
    （双线程都不启动，避免后台探针测试库里的支付会话干扰断言）
  - monkeypatch services.chagee_bridge.build_client 为 FakeClient：未知 path 抛 AssertionError（防越权）
  - 收银台凭证配置走临时文件/缓存注入，绝不读写真实 data/autopay_config.json 的判定结果；
    回调配置 monkeypatch payment_events.CALLBACK_CONFIG_PATH 到临时路径
"""

import contextlib
import hashlib
import hmac
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根
sys.path.insert(0, BASE)

# 禁用订单校准线程 + 支付 watcher 线程（必须在 import app 之前）：
# 两者 startup 挂载，置 0 后不创建后台线程，避免后台探针干扰测试库里的支付会话断言
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
# 禁用 frida 收银台铸造（pay_link_payload_with_session 会触发）：离线环境杜绝真触云手机
os.environ["CHAGEE_MINT_ENABLED"] = "0"
# 禁用跨进程状态广播（mark_session 收口会触发）：离线环境杜绝真发 HTTP 到 8010
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
# 日志隔离：oplog 日志库与文本日志均指向测试路径，避免污染生产 data/logs/
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_payportal_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_payportal.db")
for _suffix in ("", "-journal", "-wal", "-shm"):
    _p = database.DB_PATH + _suffix
    if os.path.exists(_p):
        os.remove(_p)
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

database.engine = create_engine(
    f"sqlite:///{database.DB_PATH}", connect_args={"check_same_thread": False}, pool_pre_ping=True)
database.SessionLocal = sessionmaker(bind=database.engine, autoflush=False,
                                     autocommit=False, expire_on_commit=False)

import seed  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from models import (AuditLog, ChageeAccount, CouponRecord, CouponUsageLog,  # noqa: E402
                    OrderRecord, PayAttempt, PayEventLog, PaySession, Role, SystemUser)
from routers import payportal as payportal_router  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import order_reconcile as reconcile_module  # noqa: E402
from services import pay_session as ps  # noqa: E402
from services import payment_events as pe  # noqa: E402

# ---------- 2. FakeClient（替换 build_client，杜绝真实网络） ----------

# getOrderStatus 可控行为（回放裸 int）；getOrderDetail 回放 FAKE["detail"]；
# createOrder 回放「差额单 payUrl」响应（switch 原价重下的新单号固定 NEW_ORDER_NO）
FAKE: dict = {
    "status": 3,
    "error": None,
    "detail": {"orderNo": "", "orderStatus": 3, "orderStatusText": "制作中",
               "payAmount": "10.00", "totalAmount": "20.00", "payTypeText": "支付宝",
               "pickupNo": "PW01", "orderPromotions": []},
}
CALLS: list[str] = []          # 假客户端实际收到的调用（零网络断言用）

NEW_ORDER_NO = "PPSWGNEW0001"  # switch-full-price 重下的新订单号（FakeClient 回放）
STORE_NO, STORE_NAME = "CN03324", "福建龙岩新罗万达广场店"


def _reset_fake(status: int = 3, pickup_no: str = "PW01") -> None:
    detail = dict(FAKE["detail"], orderStatus=status, pickupNo=pickup_no)
    FAKE.update(status=status, error=None, detail=detail)


def _order_str(out_trade_no: str, amount: str = "10.00", expire_at: str = None) -> str:
    """构造 alipay.trade.app.pay 形态签名串（parse_order_str/parse_pay_payload 可解析）。"""
    expire_at = expire_at or time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(time.time() + 590))
    biz = {"out_trade_no": out_trade_no, "total_amount": amount, "subject": "霸王茶姬",
           "product_code": "QUICK_MSECURITY_PAY", "time_expire": expire_at}
    return ("alipay_sdk=alipay-sdk-java-4.9.28.ALL&app_id=202100117&charset=utf-8"
            "&biz_content=" + urllib.parse.quote(json.dumps(biz, ensure_ascii=False))
            + "&sign=OFFLINETESTSIGN&sign_type=RSA2")


def _link(order_no: str, *, pay_no: str = None, out_trade_no: str = None,
          amount: str = "10.00", expire_in: float = 590) -> bridge.PayLink:
    """构造测试 PayLink（不经任何网络）。"""
    expire_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + expire_in))
    out = out_trade_no or f"OT-{order_no[-6:]}"
    return bridge.PayLink(order_no=order_no, pay_no=pay_no or f"PN-{order_no[-6:]}",
                          order_str=_order_str(out, amount, expire_at),
                          out_trade_no=out, total_amount=amount, expire_at=expire_at)


def _pay_url_data(order_no: str, out_trade_no: str, amount: str = "20.00") -> dict:
    """createOrder/continuePay 的差额单响应（payUrl 内嵌 orderStr，与真实 wire 同构）。"""
    inner = json.dumps({"requestJson": {"orderStr": _order_str(out_trade_no, amount)}},
                       ensure_ascii=False)
    return {"errcode": "0", "data": {"orderNo": order_no, "payNo": f"PN-{order_no[-4:]}",
                                     "payUrl": inner}}


class FakeClient:
    """按 path 后缀回放最小合成响应（结构与引擎读取字段一致）；未知 path 抛 AssertionError。"""

    def __init__(self, account):
        self.account = account

    def get(self, path, **kw):
        CALLS.append(path)
        if path.endswith("/customer/userInfo/query"):
            return {"errcode": "0", "data": {"customerId": "1190018250",
                                             "mobileEncrypt": "AESxFAKEPAY", "nickName": "离线支付"}}
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
        if path.endswith("/order/cancelOrder"):        # switch-full-price 第一步
            return {"errcode": "0", "data": True}
        if path.endswith("/goods/sku/calculatePrice"):  # switch 原价复算：20 元无折
            return {"errcode": "0", "data": {
                "spuId": "625339451983278080", "spuType": "stand", "skuId": "653632618000097282",
                "totalSalePrice": "20.00", "totalTradePrice": "20.00",
                "totalGoodsItemPrice": "20.00", "totalGoodsItemDiscountAmount": "0.00"}}
        if path.endswith("/order/settlePrice"):        # switch 原价 settle：buyerRealPrice=20（差额单）
            return {"errcode": "0", "data": {
                "confirmOrderKey": "ck-pp-offline",
                "tradeFundInfo": {"totalTradePrice": "20.00", "buyerRealPrice": "20.00"},
                "assetInfo": {"userCouponInfo": {"availableCouponList": []}},
                "orderGroupList": [{"goodsList": [], "tradeFundInfo": {"buyerRealPrice": "20.00"}}],
                "discountList": []}}
        if path.endswith("/order/createOrder"):
            return _pay_url_data(NEW_ORDER_NO, "OT-PPSWGNEW", "20.00")
        if path.endswith("/order/continuePay"):
            return _pay_url_data(str((body or {}).get("orderNo") or "PPCONT"), "OT-PPCONT", "10.00")
        raise AssertionError(f"未预期的 POST 协议调用: {path}")


bridge.build_client = lambda account: FakeClient(account)   # monkeypatch（payportal/watcher 经 bridge.trade_api 命中）

# ---------- 3. 测试数据 ----------

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(SystemUser).filter(SystemUser.username == "viewer_pp").first():
        viewer_role = db.query(Role).filter(Role.name == "viewer").one()
        db.add(SystemUser(username="viewer_pp", display_name="收银台只读",
                          password_hash=hash_password("Viewer@123"), role_id=viewer_role.id))
        db.commit()
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800005678").first():
        db.add(ChageeAccount(label="收银台冒烟", phone="13800005678", device_uuid="uuid-offline-pp-1",
                             token="fake.token.pp", sk="fakesk", customer_id="1190018250",
                             status="online", group="默认"))
        db.commit()
    ACC_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800005678").one().id

ACC_LABEL = f"收银台冒烟#{ACC_ID}"
TOKEN_FP = "fake.token.pp...len=14"

# 收银台凭证缓存冻结为空（真实 data/autopay_config.json 存在 cashier 三元组，冻结后测试不依赖它）；
# TTL 拉大避免长跑中途缓存过期回读真实配置；需要配置的用例经 _cashier() 注入或临时文件重载
ps._CASHIER_CFG_CACHE.update(cfg={}, at=time.time())
_ORIG_CASHIER_TTL = ps._CASHIER_CFG_TTL
ps._CASHIER_CFG_TTL = 10 ** 9

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


@contextlib.contextmanager
def _cashier(cfg: dict):
    """临时注入 cashier 三元组（缓存态，结束恢复为空配置）。"""
    ps._CASHIER_CFG_CACHE.update(cfg={"cashier": cfg} if cfg else {}, at=time.time())
    try:
        yield
    finally:
        ps._CASHIER_CFG_CACHE.update(cfg={}, at=time.time())


def _events(order_no: str) -> list[str]:
    with database.SessionLocal() as db:
        return [e.event for e in db.query(PayEventLog).filter(PayEventLog.order_no == order_no)
                .order_by(PayEventLog.id).all()]


def _wipe_pay(order_no: str) -> None:
    """删除本测试造的支付会话/铸造记录/事件流/订单快照，并清进程内探针/清扫计数。"""
    with database.SessionLocal() as db:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).first()
        if sess:
            db.query(PayAttempt).filter(PayAttempt.pay_session_id == sess.id).delete()
            db.delete(sess)
        db.query(PayEventLog).filter(PayEventLog.order_no == order_no).delete()
        db.query(OrderRecord).filter(OrderRecord.order_no == order_no).delete()
        db.commit()
    payportal_router._last_probe.pop(order_no, None)
    pe._pending_streak.pop(order_no, None)


def _wipe_coupon(coupon_code: str) -> None:
    with database.SessionLocal() as db:
        db.query(CouponUsageLog).filter(CouponUsageLog.coupon_code == coupon_code).delete()
        db.query(CouponRecord).filter(CouponRecord.coupon_code == coupon_code).delete()
        db.commit()


# ---------- 4. 测试用例 ----------

def test_01_schema_and_migrations():
    """建表/迁移：支付会话三表存在；order_records.order_target 与 pay_sessions.alipay_cashier_url 列就位。"""
    insp = inspect(database.engine)
    tables = set(insp.get_table_names())
    assert {"pay_sessions", "pay_attempts", "pay_event_logs", "order_records"} <= tables, tables
    assert "order_target" in {c["name"] for c in insp.get_columns("order_records")}
    assert "alipay_cashier_url" in {c["name"] for c in insp.get_columns("pay_sessions")}
    assert "pay_token" in {c["name"] for c in insp.get_columns("pay_sessions")}


def test_02_ensure_pay_session_lifecycle():
    """会话核心：新建（token≈43 字符 / PayAttempt 1 行 / link_issued）；续铸（token 不变 / 2 行 / remint）。"""
    o1, o2 = "PP-ENS-1", "PP-ENS-2"
    try:
        with database.SessionLocal() as db:
            s1 = ps.ensure_pay_session(db, ACC_ID, o1, _link(o1, pay_no="PN-A1", out_trade_no="OT-A1"))
            t1 = s1.pay_token
            assert 40 <= len(t1) <= 46                     # token_urlsafe(32) ≈ 43 字符
            assert s1.status == "issued" and s1.mode == "partial"
            assert s1.pay_amount == "10.00" and s1.pay_deadline is not None
            attempts = db.query(PayAttempt).filter(PayAttempt.pay_session_id == s1.id).all()
            assert len(attempts) == 1 and attempts[0].pay_no == "PN-A1"
            assert attempts[0].expire_at == s1.pay_deadline.strftime("%Y-%m-%d %H:%M:%S")
            rows = db.query(PayEventLog).filter(PayEventLog.order_no == o1).all()
            assert [e.event for e in rows] == ["link_issued"]
            assert rows[0].pay_token_prefix == t1[:8]      # 事件流只存 token 前 8 位
            # 二次 ensure：token 不变、PayAttempt 追加为 2 行、remint 事件、支付字段原地更新
            s2 = ps.ensure_pay_session(db, ACC_ID, o1,
                                       _link(o1, pay_no="PN-A2", out_trade_no="OT-A2", amount="8.00"))
            assert s2.id == s1.id and s2.pay_token == t1   # 一单一链接：token 永不变
            assert s2.pay_no == "PN-A2" and s2.out_trade_no == "OT-A2" and s2.pay_amount == "8.00"
            assert s2.status == "issued" and s2.fail_count == 0
            assert db.query(PayAttempt).filter(PayAttempt.pay_session_id == s1.id).count() == 2
            assert _events(o1) == ["link_issued", "remint"]
            # 另一单：token 唯一
            s3 = ps.ensure_pay_session(db, ACC_ID, o2, _link(o2))
            assert s3.pay_token != t1 and len(s3.pay_token) == len(t1)
    finally:
        _wipe_pay(o1)
        _wipe_pay(o2)


def test_03_mark_session_cas_and_boundaries():
    """mark_session CAS（同态返回 False、白名单外字段不生效）+ is_active/remaining_seconds 边界。"""
    order = "PP-CAS-1"
    try:
        with database.SessionLocal() as db:
            sess = ps.ensure_pay_session(db, ACC_ID, order, _link(order))
            tok = sess.pay_token
            paid_at = datetime(2026, 9, 27, 12, 0, 0)
            assert ps.mark_session(db, order, "paid", paid_at=paid_at) is True
            db.refresh(sess)
            assert sess.status == "paid" and sess.paid_at == paid_at
            # 同态再 mark：CAS 失败返回 False，paid_at 不被覆盖
            assert ps.mark_session(db, order, "paid",
                                   paid_at=datetime(2026, 9, 27, 13, 0, 0)) is False
            db.refresh(sess)
            assert sess.paid_at == paid_at
            # 白名单外字段（锚点 pay_token）不生效；白名单内 pickup_no 生效
            assert ps.mark_session(db, order, "cancelled", pay_token="HACK-TOKEN", pickup_no="PK9") is True
            db.refresh(sess)
            assert sess.pay_token == tok and sess.pickup_no == "PK9" and sess.status == "cancelled"
        # 边界：None 会话 / 非 issued 态
        assert ps.is_active(None) is False
        assert ps.remaining_seconds(None) is None
        with database.SessionLocal() as db:
            s = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert ps.is_active(s) is False                # cancelled
        with database.SessionLocal() as db:
            s = db.query(PaySession).filter(PaySession.order_no == order).one()
            s.status = "issued"
            s.pay_deadline = datetime.now() + timedelta(seconds=600)
            db.commit()
            assert ps.is_active(s) is True
            assert 500 < ps.remaining_seconds(s) <= 600
            s.pay_deadline = datetime.now() - timedelta(seconds=10)   # 过线但在 120s 宽限内
            db.commit()
            assert ps.is_active(s) is True and ps.remaining_seconds(s) == 0
            s.pay_deadline = datetime.now() - timedelta(seconds=300)  # 过宽限线
            db.commit()
            assert ps.is_active(s) is False
            s.pay_deadline = None                                    # 无截止视为仍活跃
            db.commit()
            assert ps.is_active(s) is True and ps.remaining_seconds(s) is None
    finally:
        _wipe_pay(order)


def test_04_alipay_cashier_url_config_paths():
    """alipay_cashier_url：临时配置文件→mclient 形态 URL；空配置→None；坏 order_str→None（fail-soft）。"""
    good = _order_str("OT-PP-CFG")
    tmp = tempfile.mkdtemp(prefix="pp_autopay_")
    # _load_cashier_config 取 __file__ 上三层拼 data/autopay_config.json：
    # __file__=<tmp>/a/b/pay_session.py → 三层上溯 = <tmp> → 配置须落 <tmp>/data/
    os.makedirs(os.path.join(tmp, "data"))
    cfg_path = os.path.join(tmp, "data", "autopay_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"cashier": {"session": "ppcfg-session", "utdid": "ud/cfg/01", "tid": "tidcfg01"}}, f)
    orig_file = ps.__file__
    try:
        ps.__file__ = os.path.join(tmp, "a", "b", "pay_session.py")   # 重定向 _load_cashier_config 的 data 路径
        ps._CASHIER_CFG_CACHE.update(cfg=None, at=0.0)       # 强制下轮重读
        url = ps.alipay_cashier_url(good)
        assert url is not None, "临时配置下应构造出收银台 URL"
        assert url.startswith("https://mclient.alipay.com/cashierRoutePay.htm?")
        for frag in ("route_pay_from=h5", "init_from=SDKLite", "session=ppcfg-session",
                     "utdid=ud/cfg/01", "tid=tidcfg01", "cc=y"):
            assert frag in url, f"收银台 URL 缺片段 {frag}: {url}"
        # 空配置（文件缺失，fail-soft 读取失败）→ None（不依赖真实仓库 data/ 配置）
        os.remove(cfg_path)
        ps._CASHIER_CFG_CACHE.update(cfg=None, at=0.0)
        assert ps.alipay_cashier_url(good) is None
        # 配置恢复 + 坏 order_str → None（fail-soft 不抛异常）
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"cashier": {"session": "ppcfg-session"}}, f)
        ps._CASHIER_CFG_CACHE.update(cfg=None, at=0.0)
        assert ps.alipay_cashier_url("not-an-alipay-order-str") is None
        assert ps.alipay_cashier_url("") is None
        # enabled=false（生产默认：mobilegw 短窗过期后停用静态构造）→ 即使有凭证也 None
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"enabled": False, "cashier": {"session": "ppcfg-session"}}, f)
        ps._CASHIER_CFG_CACHE.update(cfg=None, at=0.0)
        assert ps.alipay_cashier_url(good) is None
    finally:
        ps.__file__ = orig_file
        ps._CASHIER_CFG_CACHE.update(cfg={}, at=time.time())
        shutil.rmtree(tmp, ignore_errors=True)


def test_05_pay_link_payload_and_base_url():
    """payload：h5_url 恒为稳定壳页（portal_url 同值）；alipay_cashier_url 为官方收银台
    直链（有配置时非空、无配置 None——mobilegw 短窗语义）；CHAGEE_PAY_BASE_URL 优先。"""
    o1, o2 = "PP-PAY-1", "PP-PAY-2"
    os.environ.pop("CHAGEE_PAY_BASE_URL", None)
    try:
        ps._H5_BASE_CACHE.update(base=None, at=0.0)
        with _cashier({"session": "pppay-session", "utdid": "ud/pay", "tid": "tidpay"}):
            with database.SessionLocal() as db:
                p1 = ps.pay_link_payload_with_session(db, _link(o1), ACC_ID)
                assert p1["result"] == "partial" and p1["order_no"] == o1
                # h5_url 恒为壳页（稳定可达）；官方收银台走独立字段 alipay_cashier_url
                assert p1["h5_url"] == p1["portal_url"]
                assert p1["portal_url"].startswith("http://") and ":8010/pay/" in p1["portal_url"]
                assert p1["portal_url"].endswith(f"/pay/{p1['pay_token']}")
                assert p1["alipay_cashier_url"].startswith("https://mclient.alipay.com/cashierRoutePay.htm")
                assert "收银台" in p1["note"]
                assert p1["pay_window_seconds"] == 600 and p1["order_str"]
        # 无收银台配置：h5_url 仍为壳页，alipay_cashier_url 为 None
        with database.SessionLocal() as db:
            p2 = ps.pay_link_payload_with_session(db, _link(o2), ACC_ID)
            assert p2["h5_url"] == p2["portal_url"]
            assert p2["portal_url"].endswith(f"/pay/{p2['pay_token']}")
            assert p2["alipay_cashier_url"] is None
            assert "收银台" in p2["note"]
        # 环境变量覆盖基址（rstrip 尾斜杠后拼接）
        os.environ["CHAGEE_PAY_BASE_URL"] = "https://pay.example.cn/"
        ps._H5_BASE_CACHE.update(base=None, at=0.0)
        assert ps.build_h5_url("TOK123") == "https://pay.example.cn/pay/TOK123"
    finally:
        os.environ.pop("CHAGEE_PAY_BASE_URL", None)
        ps._H5_BASE_CACHE.update(base=None, at=0.0)
        _wipe_pay(o1)
        _wipe_pay(o2)


def test_06_public_page_and_info():
    """公开路由：GET /pay/{token} 页面壳 200；info 200 全字段断言 + page_opened 事件（ua/ip 摘要）。"""
    order, coup = "PP-INFO-1", "COUP-INFO-1"
    link = _link(order)
    try:
        with database.SessionLocal() as db:
            db.add(OrderRecord(account_id=ACC_ID, order_no=order, store_no=STORE_NO,
                               store_name=STORE_NAME, goods_desc="伯牙绝弦(大杯) x1",
                               scenario="partial", status=1, status_label="待支付",
                               total_amount="20.00", pay_amount="10.00", coupon_code=coup))
            db.add(CouponRecord(coupon_code=coup, account_id=ACC_ID, token_fingerprint=TOKEN_FP,
                                template_name="霸王茶姬10元代金券-INFO", amount="10",
                                bucket="settle_available", synced_from="settle"))
            db.commit()
            with _cashier({"session": "ppinfo-session", "utdid": "ud/info", "tid": "tidinfo"}):
                sess = ps.ensure_pay_session(db, ACC_ID, order, link, coupon_code=coup)
                tok = sess.pay_token
                assert sess.alipay_cashier_url.startswith(
                    "https://mclient.alipay.com/cashierRoutePay.htm")
        # 页面壳：静态分发 + 可诊断关键字
        r = CLIENT.get(f"/pay/{tok}")
        assert r.status_code == 200, r.text
        assert "霸王茶姬" in r.text and "收银台" in r.text and 'id="btnCashier"' in r.text
        # info：全字段断言
        r = CLIENT.get(f"/pay/{tok}/info")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["order_no"] == order
        assert d["store_name"] == STORE_NAME and d["goods_desc"] == "伯牙绝弦(大杯) x1"
        assert d["total_amount"] == "20.00" and d["pay_amount"] == "10.00"
        assert d["coupon_code"] == coup and d["coupon_name"] == "霸王茶姬10元代金券-INFO"
        assert d["deduction"] == "10.00"
        assert d["mode"] == "partial" and d["status"] == "issued" and d["status_label"] == "待支付"
        assert d["pay_no"] == link.pay_no and d["out_trade_no"] == link.out_trade_no
        assert d["expire_at"] == link.expire_at
        assert isinstance(d["remaining_seconds"], int) and d["remaining_seconds"] > 0
        assert d["order_str"] == link.order_str
        assert d["alipay_cashier_url"].startswith("https://mclient.alipay.com/cashierRoutePay.htm")
        assert d["portal_url"].endswith(f"/pay/{tok}")
        assert d["can_switch_full_price"] is True
        # page_opened 事件（含 UA/IP 摘要）
        assert _events(order) == ["link_issued", "page_opened"]
        with database.SessionLocal() as db:
            row = db.query(PayEventLog).filter(PayEventLog.order_no == order,
                                               PayEventLog.event == "page_opened").one()
            assert row.payload.get("ip") and row.payload.get("ua")
            assert row.pay_token_prefix == tok[:8]
    finally:
        _wipe_pay(order)
        _wipe_coupon(coup)


def test_07_unknown_token_and_local_status():
    """乱 token info/status 404；status 本地态返回（不活跃/节流窗口内都不触发远程探针）。"""
    assert CLIENT.get("/pay/not-a-real-token/info").status_code == 404
    assert CLIENT.get("/pay/not-a-real-token/status").status_code == 404
    order = "PP-LOCAL-1"
    try:
        with database.SessionLocal() as db:
            sess = ps.ensure_pay_session(db, ACC_ID, order, _link(order, expire_in=-400))
            tok = sess.pay_token                       # 截止已过宽限线（>120s）→ 不活跃
        calls0 = len(CALLS)
        payportal_router._last_probe.pop(order, None)
        r = CLIENT.get(f"/pay/{tok}/status")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "issued" and d["status_label"] == "待支付" and d["pickup_no"] == ""
        assert len(CALLS) == calls0                    # 未触发远程探针（纯本地态）
        # 节流窗口内（活跃会话但 2s 内刚探过）→ 同样只返回本地态
        with database.SessionLocal() as db:
            s = db.query(PaySession).filter(PaySession.order_no == order).one()
            s.pay_deadline = datetime.now() + timedelta(seconds=300)
            db.commit()
        payportal_router._last_probe[order] = time.time()
        r = CLIENT.get(f"/pay/{tok}/status")
        assert r.status_code == 200 and r.json()["status"] == "issued"
        assert len(CALLS) == calls0
    finally:
        _wipe_pay(order)


def test_08_remint_conflict_on_paid():
    """remint：已支付会话 → 409（不可续付），不触达协议层。"""
    order = "PP-REMT-1"
    try:
        with database.SessionLocal() as db:
            sess = ps.ensure_pay_session(db, ACC_ID, order, _link(order))
            tok = sess.pay_token
            assert ps.mark_session(db, order, "paid", paid_at=datetime.now()) is True
        calls0 = len(CALLS)
        r = CLIENT.post(f"/pay/{tok}/remint")
        assert r.status_code == 409, r.text
        assert "不可续付" in r.json()["detail"]
        assert len(CALLS) == calls0
    finally:
        _wipe_pay(order)


def test_09_status_probe_paid_transition():
    """状态跃迁：茶姬 3 + 详情 PW01 → /status 探针 → 会话 paid、取餐号回写、OrderRecord 同步、事件链。"""
    order = "PP-PAID-1"
    try:
        with database.SessionLocal() as db:
            db.add(OrderRecord(account_id=ACC_ID, order_no=order, scenario="partial", status=1,
                               status_label="待支付", total_amount="20.00", pay_amount="10.00"))
            db.commit()
            tok = ps.ensure_pay_session(db, ACC_ID, order, _link(order)).pay_token
        _reset_fake(status=3, pickup_no="PW01")
        payportal_router._last_probe.pop(order, None)
        r = CLIENT.get(f"/pay/{tok}/status")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] == "paid" and d["status_label"] == "已支付"
        assert d["pickup_no"] == "PW01" and d["paid_at"]
        with database.SessionLocal() as db:
            s = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert s.status == "paid" and s.pickup_no == "PW01" and s.paid_at is not None
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == order).one()
            assert rec.status == 3 and rec.status_label == "制作中"
            assert rec.pickup_no == "PW01"
            assert rec.pay_amount == "10.00" and rec.total_amount == "20.00"  # 详情回填
            evs = _events(order)
            assert "paid_detected" in evs and "pickup_fetched" in evs
    finally:
        _reset_fake()
        _wipe_pay(order)


def test_10_status_probe_cancelled_rolls_back():
    """状态跃迁：茶姬 7 + 有券 → 会话 cancelled + OrderRecord 同步 + rolled_back 事件 + 券档案复位。"""
    order, coup = "PP-CANC-1", "COUP-CANC-1"
    now = datetime.now()
    try:
        with database.SessionLocal() as db:
            db.add(OrderRecord(account_id=ACC_ID, order_no=order, scenario="partial", status=1,
                               status_label="待支付", coupon_code=coup,
                               total_amount="20.00", pay_amount="10.00"))
            db.add(CouponRecord(coupon_code=coup, account_id=ACC_ID, token_fingerprint=TOKEN_FP,
                                template_name="霸王茶姬10元代金券-CANC", amount="10",
                                bucket="settle_available", synced_from="settle",
                                last_used_at=now, last_order_no=order))
            db.add(CouponUsageLog(coupon_code=coup, coupon_name="霸王茶姬10元代金券-CANC",
                                  account_id=ACC_ID, account_label=ACC_LABEL, operator="admin",
                                  order_no=order, deduction="10.00", total_amount="20.00",
                                  pay_amount="10.00", scenario="partial", result="success"))
            db.commit()
            tok = ps.ensure_pay_session(db, ACC_ID, order, _link(order), coupon_code=coup).pay_token
        _reset_fake(status=7)
        payportal_router._last_probe.pop(order, None)
        r = CLIENT.get(f"/pay/{tok}/status")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled"
        with database.SessionLocal() as db:
            s = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert s.status == "cancelled"
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == order).one()
            assert rec.status == 7 and rec.status_label == "已取消"
            evs = _events(order)
            assert "order_cancelled" in evs and "rolled_back" in evs
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coup).one()
            assert c.bucket == "effective" and c.last_used_at is None and c.last_order_no == ""
            log = (db.query(CouponUsageLog)
                     .filter(CouponUsageLog.coupon_code == coup,
                             CouponUsageLog.result == "rolled_back")
                     .order_by(CouponUsageLog.id.desc()).first())
            assert log is not None and log.order_no == order and log.operator == "admin"
            assert log.deduction == "10.00"               # 原行金额快照保留（§18 单行生命周期）
            # 原地迁移：不新增行；流转 actor=pay-portal 记入 state_history
            assert (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == coup).count()) == 1
            hist = json.loads(log.state_history or "[]")
            assert any(h.get("by") == "pay-portal" and h.get("to") == "rolled_back"
                       for h in hist)
    finally:
        _reset_fake()
        _wipe_pay(order)
        _wipe_coupon(coup)


def _switch_snapshot() -> dict:
    """order_target 快照（settle target 形态 + 门店/商品描述），switch 原价重下的算价入参。"""
    return {"target": {"spuId": "625339451983278080", "spuType": "stand",
                       "skuId": "653632618000097282", "quantity": 1, "salePrice": 20.0,
                       "storeNo": STORE_NO, "skuName": "伯牙绝弦",
                       "specList": [{"specId": "653599312273510400",
                                     "specOptionId": "653599312273510402"}]},
            "extra_list": [], "store_no": STORE_NO, "store_name": STORE_NAME,
            "goods_desc": "伯牙绝弦(大杯) x1"}


def test_11_switch_full_price_flow():
    """switch-full-price：取消旧单原价重下 → 旧会话 cancelled、新 OrderRecord（full 无券）+ 新会话
    token、switch_full_price 事件、券回滚（rolled_back 日志 operator=pay-portal）。"""
    order, new_no, coup = "PP-SW-OLD-1", NEW_ORDER_NO, "COUP-SW-1"
    now = datetime.now()
    try:
        with database.SessionLocal() as db:
            db.add(OrderRecord(account_id=ACC_ID, order_no=order, store_no=STORE_NO,
                               store_name=STORE_NAME, goods_desc="伯牙绝弦(大杯) x1",
                               scenario="partial", status=1, status_label="待支付",
                               coupon_code=coup, total_amount="20.00", pay_amount="10.00",
                               order_target=_switch_snapshot()))
            db.add(CouponRecord(coupon_code=coup, account_id=ACC_ID, token_fingerprint=TOKEN_FP,
                                template_name="霸王茶姬10元代金券-SW", amount="10",
                                bucket="settle_available", synced_from="settle",
                                last_used_at=now, last_order_no=order))
            db.add(CouponUsageLog(coupon_code=coup, coupon_name="霸王茶姬10元代金券-SW",
                                  account_id=ACC_ID, account_label=ACC_LABEL, operator="admin",
                                  order_no=order, deduction="10.00", total_amount="20.00",
                                  pay_amount="10.00", scenario="partial", result="success"))
            db.commit()
            old_tok = ps.ensure_pay_session(db, ACC_ID, order, _link(order),
                                            coupon_code=coup).pay_token
        r = CLIENT.post(f"/pay/{old_tok}/switch-full-price")
        assert r.status_code == 200, f"switch 失败: {r.status_code} {r.text}"
        d = r.json()
        assert d["old_order_no"] == order and d["new_order_no"] == new_no
        assert d["result"] == "partial" and d["coupon_rolled_back"] is True
        assert "/pay/" in d["new_h5_url"] and d["new_h5_url"].endswith(d["pay_token"])
        with database.SessionLocal() as db:
            old = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert old.status == "cancelled"
            old_rec = db.query(OrderRecord).filter(OrderRecord.order_no == order).one()
            assert old_rec.status == 7 and old_rec.status_label == "已取消"
            new_sess = db.query(PaySession).filter(PaySession.order_no == new_no).one()
            assert new_sess.status == "issued" and new_sess.mode == "full"
            assert new_sess.coupon_code == "" and new_sess.pay_token != old_tok
            new_rec = db.query(OrderRecord).filter(OrderRecord.order_no == new_no).one()
            assert new_rec.scenario == "partial" and new_rec.coupon_code == ""
            assert new_rec.status == 1 and new_rec.pay_amount == "20.00"   # 原价无券
            assert (new_rec.order_target or {}).get("target")              # 快照随新单续存
            # 事件链：旧单 order_cancelled(source=switch)/rolled_back/switch_full_price；新单 link_issued
            evs = _events(order)
            assert "order_cancelled" in evs and "rolled_back" in evs and "switch_full_price" in evs
            sw = (db.query(PayEventLog)
                    .filter(PayEventLog.order_no == order, PayEventLog.event == "switch_full_price")
                    .order_by(PayEventLog.id.desc()).first())
            assert sw.payload["new_order_no"] == new_no and sw.payload["result"] == "partial"
            assert sw.payload["new_pay_token"] == new_sess.pay_token[:8]
            assert _events(new_no) == ["link_issued"]
            c = db.query(CouponRecord).filter(CouponRecord.coupon_code == coup).one()
            assert c.bucket == "effective" and c.last_used_at is None and c.last_order_no == ""
            log = (db.query(CouponUsageLog)
                     .filter(CouponUsageLog.coupon_code == coup,
                             CouponUsageLog.result == "rolled_back").first())
            assert log is not None and log.order_no == order and log.operator == "admin"
            assert any(h.get("by") == "pay-portal" and h.get("to") == "rolled_back"
                       for h in json.loads(log.state_history or "[]"))   # 流转 actor 入轨迹（§18）
    finally:
        _reset_fake()
        _wipe_pay(order)
        _wipe_pay(new_no)
        _wipe_coupon(coup)


def test_12_switch_full_price_rejections():
    """switch-full-price 拒绝路径：缺商品快照→400；非券差额单→400（会话/订单均不动）。"""
    o_nosnap, o_nocoup, coup = "PP-SW-NOSNAP-1", "PP-SW-NOCOUP-1", "COUP-SW-NOSNAP"
    try:
        with database.SessionLocal() as db:
            for order, coupon, target in ((o_nosnap, coup, None),
                                          (o_nocoup, "", _switch_snapshot())):
                db.add(OrderRecord(account_id=ACC_ID, order_no=order, scenario="partial",
                                   status=1, status_label="待支付", coupon_code=coupon,
                                   total_amount="20.00", pay_amount="10.00",
                                   order_target=target))
                db.commit()
                tok = ps.ensure_pay_session(db, ACC_ID, order, _link(order),
                                            coupon_code=coupon).pay_token
                if order == o_nosnap:
                    tok_nosnap = tok
                else:
                    tok_nocoup = tok
        r = CLIENT.post(f"/pay/{tok_nosnap}/switch-full-price")
        assert r.status_code == 400, r.text
        assert "快照" in r.json()["detail"]
        r = CLIENT.post(f"/pay/{tok_nocoup}/switch-full-price")
        assert r.status_code == 400, r.text
        assert "非券差额单" in r.json()["detail"]
        with database.SessionLocal() as db:                 # 拒绝后现场不动
            for order in (o_nosnap, o_nocoup):
                assert (db.query(PaySession).filter(PaySession.order_no == order)
                        .one().status) == "issued"
                assert (db.query(OrderRecord).filter(OrderRecord.order_no == order)
                        .one().status) == 1
            assert db.query(PaySession).filter(PaySession.order_no == NEW_ORDER_NO).count() == 0
    finally:
        _wipe_pay(o_nosnap)
        _wipe_pay(o_nocoup)
        _wipe_coupon(coup)


def test_13_watch_once_paid_and_audit():
    """watch_once（直调不启线程）：paid CAS 收口 + 事件链 + 伪用户审计（feature.pay_watcher）；
    重复 watch 不重复收口（会话已终态不进扫描集）。"""
    order = "PP-WATCH-1"
    orig_path = pe.CALLBACK_CONFIG_PATH
    pe.CALLBACK_CONFIG_PATH = os.path.join(tempfile.gettempdir(), "pp_no_such_callback.json")
    try:                                                    # 回调配置不存在 → 分派 no-op（隔离真实 data/）
        with database.SessionLocal() as db:
            db.add(OrderRecord(account_id=ACC_ID, order_no=order, scenario="partial", status=1,
                               status_label="待支付", total_amount="20.00", pay_amount="10.00"))
            db.commit()
            ps.ensure_pay_session(db, ACC_ID, order, _link(order))
        _reset_fake(status=3, pickup_no="PW01")
        with database.SessionLocal() as db:
            stats = pe.watch_once(db)
        assert stats["paid"] == 1 and stats["probed"] >= 1
        assert stats["cancelled"] == 0 and stats["expired"] == 0
        with database.SessionLocal() as db:
            s = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert s.status == "paid" and s.pickup_no == "PW01" and s.paid_at is not None
            rec = db.query(OrderRecord).filter(OrderRecord.order_no == order).one()
            assert rec.status == 3 and rec.pickup_no == "PW01"
            audits = db.query(AuditLog).filter(AuditLog.action == "feature.pay_watcher",
                                               AuditLog.target == order).all()
            assert len(audits) == 1 and audits[0].username == "pay-watcher"
            paid_evs = db.query(PayEventLog).filter(PayEventLog.order_no == order,
                                                    PayEventLog.event == "paid_detected").all()
            assert len(paid_evs) == 1 and paid_evs[0].payload.get("source") == "pay-watcher"
            assert (db.query(PayEventLog).filter(PayEventLog.order_no == order,
                    PayEventLog.event == "pickup_fetched").count()) == 1
            assert (db.query(PayEventLog).filter(PayEventLog.order_no == order,
                    PayEventLog.event == "callback_dispatched").count()) == 0
        # 重复 watch：paid 会话不进扫描集 → 不重复收口（审计/事件计数不变）
        with database.SessionLocal() as db:
            stats2 = pe.watch_once(db)
        assert stats2["scanned"] == 0 and stats2["probed"] == 0
        with database.SessionLocal() as db:
            assert (db.query(AuditLog).filter(AuditLog.action == "feature.pay_watcher",
                    AuditLog.target == order).count()) == 1
            assert (db.query(PayEventLog).filter(PayEventLog.order_no == order,
                    PayEventLog.event == "paid_detected").count()) == 1
    finally:
        pe.CALLBACK_CONFIG_PATH = orig_path
        _reset_fake()
        _wipe_pay(order)


def test_14_pay_events_endpoint():
    """pay-events 管理端点：admin JWT 分页/过滤字段（id 倒序）；无 token 401。"""
    order = "PP-EVT-1"
    with database.SessionLocal() as db:
        ps.record_event(db, order, "abcd1234", "link_issued", {"pay_no": "PN-EVT"})
        ps.record_event(db, order, "abcd1234", "page_opened", {"ip": "127.0.0.1"})
    try:
        assert CLIENT.get("/api/ops/pay-events").status_code == 401
        h = _auth()
        r = CLIENT.get("/api/ops/pay-events", params={"order_no": order}, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] == 2 and len(d["items"]) == 2
        for key in ("id", "order_no", "pay_token_prefix", "event", "payload", "created_at"):
            assert key in d["items"][0]
        assert d["items"][0]["id"] > d["items"][1]["id"]          # id 倒序（最新在前）
        assert d["items"][0]["event"] == "page_opened"
        assert d["items"][0]["order_no"] == order and d["items"][0]["pay_token_prefix"] == "abcd1234"
        r = CLIENT.get("/api/ops/pay-events", params={"order_no": order, "event": "link_issued"},
                       headers=h)
        d = r.json()
        assert d["total"] == 1 and d["items"][0]["event"] == "link_issued"
        r = CLIENT.get("/api/ops/pay-events", params={"order_no": order, "page_size": 1}, headers=h)
        d = r.json()
        assert d["total"] == 2 and len(d["items"]) == 1           # 分页生效
    finally:
        with database.SessionLocal() as db:
            db.query(PayEventLog).filter(PayEventLog.order_no == order).delete()
            db.commit()


def test_15_callback_dispatch_success():
    """回调分派成功：本地 http.server 接收 + X-CHAGEE-Signature hmac 复算验签 + callback_dispatched 事件。"""
    order = "PP-CB-1"
    received: list[tuple[dict, bytes]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            received.append((dict(self.headers), self.rfile.read(n)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):        # 静默（避免污染测试输出）
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True, name="pp-callback-server").start()
    tmpdir = tempfile.mkdtemp(prefix="pp_cbcfg_")
    cfg_path = os.path.join(tmpdir, "pay_callback_config.json")   # 临时配置（不落 data/）
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"urls": [f"http://127.0.0.1:{port}/cb"], "secret": "pp-cb-secret",
                   "timeout": 3}, f)
    orig_path = pe.CALLBACK_CONFIG_PATH
    pe.CALLBACK_CONFIG_PATH = cfg_path
    try:
        with database.SessionLocal() as db:
            sess = ps.ensure_pay_session(db, ACC_ID, order, _link(order))
            prefix = ps.token_prefix(sess.pay_token)
            pe.dispatch_payment_callback(db, sess, "paid")
        assert len(received) == 1, "接收端应收到一次 POST"
        headers, raw = received[0]
        hdrs = {k.lower(): v for k, v in headers.items()}   # HTTP 头大小写不敏感（urllib 会 capitalize）
        expect_sig = hmac.new(b"pp-cb-secret", raw, hashlib.sha256).hexdigest()
        assert hdrs.get("x-chagee-signature") == expect_sig  # 接收端同 secret 复算验签
        assert hdrs.get("content-type") == "application/json"
        body = json.loads(raw)
        assert body["event"] == "paid" and body["order_no"] == order
        assert body["pay_amount"] == "10.00" and body["dispatched_at"]
        with database.SessionLocal() as db:
            ev = (db.query(PayEventLog)
                    .filter(PayEventLog.order_no == order,
                            PayEventLog.event == "callback_dispatched")
                    .order_by(PayEventLog.id.desc()).first())
            assert ev is not None
            assert ev.payload["http_status"] == 200 and ev.payload["event"] == "paid"
            assert ev.payload["url"].endswith(f":{port}/cb")
            assert ev.pay_token_prefix == prefix
    finally:
        pe.CALLBACK_CONFIG_PATH = orig_path
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        _wipe_pay(order)


def test_16_callback_failure_retries():
    """回调分派失败：无人监听端口 → 初始 1 + 重试 3（退避清零控时）→ callback_failed 事件链
    retries_left 3→0，且无 callback_dispatched。"""
    order = "PP-CBF-1"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()                                                  # 端口无人监听 → 连接拒绝
    tmpdir = tempfile.mkdtemp(prefix="pp_cbfail_")
    cfg_path = os.path.join(tmpdir, "pay_callback_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"urls": [f"http://127.0.0.1:{port}/cb"], "secret": "pp-cb-secret",
                   "timeout": 2}, f)
    orig_path = pe.CALLBACK_CONFIG_PATH
    orig_backoff = pe._CALLBACK_RETRY_BACKOFF
    pe.CALLBACK_CONFIG_PATH = cfg_path
    pe._CALLBACK_RETRY_BACKOFF = (0.0, 0.0, 0.0)                  # 退避清零：4 次尝试瞬时完成
    try:
        with database.SessionLocal() as db:
            sess = ps.ensure_pay_session(db, ACC_ID, order, _link(order))
            pe.dispatch_payment_callback(db, sess, "paid")
        with database.SessionLocal() as db:
            fails = (db.query(PayEventLog)
                       .filter(PayEventLog.order_no == order,
                               PayEventLog.event == "callback_failed")
                       .order_by(PayEventLog.id).all())
            assert len(fails) == 4                                 # 初始尝试 + 3 次重试
            assert [f.payload.get("retries_left") for f in fails] == [3, 2, 1, 0]
            assert all(f.payload.get("error") for f in fails)
            assert all(f.payload.get("event") == "paid" for f in fails)
            assert (db.query(PayEventLog)
                      .filter(PayEventLog.order_no == order,
                              PayEventLog.event == "callback_dispatched").count()) == 0
    finally:
        pe.CALLBACK_CONFIG_PATH = orig_path
        pe._CALLBACK_RETRY_BACKOFF = orig_backoff
        shutil.rmtree(tmpdir, ignore_errors=True)
        _wipe_pay(order)


def test_17_threads_not_started():
    """线程不启动：CHAGEE_*_INTERVAL_SECONDS<=0 时 start_pay_watcher/start_reconcile_thread
    不置标志、不创建线程；当前进程也无 pay-watcher/order-reconcile 线程。"""
    os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
    os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
    orig_w, orig_r = pe._started, reconcile_module._thread_started
    pe._started = False
    reconcile_module._thread_started = False
    try:
        pe.start_pay_watcher()
        reconcile_module.start_reconcile_thread()
        assert pe._started is False                     # 未置启动标志
        assert reconcile_module._thread_started is False
        names = [t.name for t in threading.enumerate()]
        assert "pay-watcher" not in names
        assert "order-reconcile" not in names
    finally:
        pe._started = orig_w
        reconcile_module._thread_started = orig_r


def test_18_no_real_network_calls():
    """零网络保障：全程仅触达回放覆盖的协议端点（未知 path 已由 FakeClient AssertionError 拦截）。"""
    allowed_suffixes = (
        "/customer/userInfo/query",
        "/goods/sku/calculatePrice", "/order/settlePrice", "/order/createOrder",
        "/order/continuePay", "/order/getOrderStatus", "/order/getOrderDetail",
        "/order/cancelOrder",
    )
    bad = [p for p in CALLS if not p.endswith(allowed_suffixes)]
    assert not bad, f"出现了未经回放覆盖的协议调用: {bad}"
    assert CALLS, "本套件应至少发生过一次协议层回放调用"


def test_19_mint_disabled_clean_payload():
    """CHAGEE_MINT_ENABLED=0：pay_link_payload_with_session 正常返回且无 mint 事件、无异常
    （环境开关在 trigger_mint 入口拦截，不建线程、不触云手机）。"""
    order = "PP-MINT-1"
    os.environ["CHAGEE_MINT_ENABLED"] = "0"   # 双保险：本套件模块头已置 0，此处显式钉死
    try:
        with database.SessionLocal() as db:
            payload = ps.pay_link_payload_with_session(db, _link(order), ACC_ID)
            assert payload["result"] == "partial" and payload["order_no"] == order
            assert payload["pay_token"] and payload["h5_url"] == payload["portal_url"]
        # 触发链被开关拦下：只有 ensure_pay_session 的 link_issued，无 mint_failed / cashier_updated
        assert _events(order) == ["link_issued"]
    finally:
        os.environ["CHAGEE_MINT_ENABLED"] = "0"
        _wipe_pay(order)


def test_20_cashier_endpoint_auth_and_fields():
    """GET /orders/{order_no}/cashier：无 token 401；有会话 200（order_no / alipay_cashier_url /
    session_status / note，未铸造时 alipay_cashier_url=None）；无会话 404。构造会话直查，
    不触发真铸造（ensure_pay_session 不走 trigger，且本套件 MINT_ENABLED=0）。"""
    order = "PP-MINT-EP-1"
    url = f"/api/ops/accounts/{ACC_ID}/orders/{order}/cashier"
    assert CLIENT.get(url).status_code == 401
    try:
        with database.SessionLocal() as db:
            ps.ensure_pay_session(db, ACC_ID, order, _link(order))
        h = _auth()
        r = CLIENT.get(url, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["order_no"] == order
        assert d["alipay_cashier_url"] is None          # 未铸造
        assert d["session_status"] == "issued"
        assert "稍候" in d["note"]
        # 模拟 frida-mint 完成态：回填后端点透出官方收银台直链
        with database.SessionLocal() as db:
            s = db.query(PaySession).filter(PaySession.order_no == order).one()
            s.alipay_cashier_url = ("https://mclient.alipay.com/cashierRoutePay.htm"
                                    "?route_pay_from=h5&init_from=SDKLite&session=x&cc=y")
            db.commit()
        r = CLIENT.get(url, headers=h)
        d = r.json()
        assert d["alipay_cashier_url"].startswith("https://mclient.alipay.com/cashierRoutePay.htm")
        assert d["session_status"] == "issued"
        # 无会话订单 → 404
        r = CLIENT.get(f"/api/ops/accounts/{ACC_ID}/orders/NO-SUCH-ORDER/cashier", headers=h)
        assert r.status_code == 404
    finally:
        _wipe_pay(order)


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
    # 还原模块级 monkeypatch（收银台凭证缓存 TTL）
    ps._CASHIER_CFG_TTL = _ORIG_CASHIER_TTL
    ps._CASHIER_CFG_CACHE.update(cfg=None, at=0.0)
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
