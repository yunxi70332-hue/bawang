"""官方收银台支付参数串（pay_param_records / services/pay_params）离线回归测试。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server
    ..\\.venv_verify\\Scripts\\python.exe test_pay_params_offline.py
    （或 pytest test_pay_params_offline.py -q；须单独跑，不与其它套件合跑——engine 互踩是既定约定）

覆盖（编号即用例名前缀，main() 按字典序执行）：
  01 建表：pay_param_records 表与全列就位
  02 parse_cashier_url：真实形态 URL 解析（utdid URL 解码 / 键全保留）；缺三元组 /
     非 mclient 域 / 非 https → None
  03 build_param_str + load_pay_param_str：round-trip 字段一致；缺必需参数 → None；
     坏 JSON / 版本不符 / params 缺失 → ValueError
  04 save/get：落库关联字段（order_no/account_id/token 前缀/source）；二次 save 覆盖
     仍一单一条（最新短窗语义）
  05 ensure_pay_session 静态构造联动：注入 cashier 三元组配置 → 会话 + 参数串两表同落
     （source=static-config）；无配置 → 会话正常但参数串为空（fail-soft）
  06 cashier_mint._fill_back 联动：URL 回填 + 参数串 + cashier_updated 事件三同步；
     非 issued 会话放弃回填
  07 HTTP：POST cashier-url 人工回填 → 响应带 pay_params/pay_param_str + 库记录；
     GET cashier 轮询透出 pay_param_str；无 token 401
  08 GET pay-params 显式端点：200 全字段（generated/解析对象）；无会话 404
  09 payload_fields / pay_link_payload_with_session：有记录随单下发；无记录空字段；
     坏数据兜底（坏 JSON 不抛、pay_params=None、原文透出）

要点（与 test_payportal_offline.py 同模式）：
  - 先把 database.DB_PATH 指向 data/test_pay_params.db 并重建 engine，再 import app
  - import 前置 CHAGEE_MINT_ENABLED=0 等环境变量（禁一切后台线程/铸造/广播）
  - 收银台凭证缓存冻结为空（不读真实 data/autopay_config.json），需要配置的用例注入缓存
  - 全程零真实网络：bridge.build_client 替换为即抛断言（本套件不应触达任何协议端点）
"""

import contextlib
import json
import os
import sys
import time
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根
sys.path.insert(0, BASE)

# 禁用订单校准线程 + 支付 watcher 线程 + 收银台铸造 + 跨进程广播（必须在 import app 之前）
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MINT_ENABLED"] = "0"
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
# 日志隔离：oplog 库与文本日志均指向测试路径
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_pay_params_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_pay_params.db")
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
from models import ChageeAccount, PayAttempt, PayEventLog, PayParamRecord, PaySession, SystemUser  # noqa: E402
from security import hash_password  # noqa: E402
from services import cashier_mint  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import pay_params as pp  # noqa: E402
from services import pay_session as ps  # noqa: E402

# ---------- 2. 零网络保障：任何真实协议调用即炸（本套件只造 PayLink，不触 trade 路径） ----------


def _no_network(account):
    raise AssertionError("pay_params 测试不应触达任何真实协议端点")


bridge.build_client = _no_network

# ---------- 3. 测试数据 ----------

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800009999").first():
        db.add(ChageeAccount(label="参数串冒烟", phone="13800009999", device_uuid="uuid-offline-ppm-1",
                             token="fake.token.ppm", sk="fakesk", customer_id="1190099999",
                             status="online", group="默认"))
        db.commit()
    ACC_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800009999").one().id
    assert db.query(SystemUser).filter(SystemUser.username == "admin").first()  # seed 兜底

# 收银台凭证缓存冻结为空（真实 data/autopay_config.json 存在三元组，冻结后测试不依赖它）
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


def _auth():
    return {"Authorization": f"Bearer {_login('admin', 'Admin@123')}"}


@contextlib.contextmanager
def _cashier(cfg: dict):
    """临时注入 cashier 三元组（缓存态，结束恢复为空配置）。"""
    ps._CASHIER_CFG_CACHE.update(cfg={"cashier": cfg} if cfg else {}, at=time.time())
    try:
        yield
    finally:
        ps._CASHIER_CFG_CACHE.update(cfg={}, at=time.time())


def _order_str(out_trade_no: str, amount: str = "16.00") -> str:
    biz = {"out_trade_no": out_trade_no, "total_amount": amount, "subject": "霸王茶姬",
           "product_code": "QUICK_MSECURITY_PAY",
           "time_expire": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 590))}
    return ("alipay_sdk=alipay-sdk-java-4.9.28.ALL&app_id=202100117&charset=utf-8"
            "&biz_content=" + urllib.parse.quote(json.dumps(biz, ensure_ascii=False))
            + "&sign=OFFLINETESTSIGN&sign_type=RSA2")


def _link(order_no: str, amount: str = "16.00") -> bridge.PayLink:
    expire_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 590))
    out = f"OT-{order_no[-6:]}"
    return bridge.PayLink(order_no=order_no, pay_no=f"PN-{order_no[-6:]}",
                          order_str=_order_str(out, amount),
                          out_trade_no=out, total_amount=amount, expire_at=expire_at)


def _events(order_no: str) -> list[str]:
    with database.SessionLocal() as db:
        return [e.event for e in db.query(PayEventLog).filter(PayEventLog.order_no == order_no)
                .order_by(PayEventLog.id).all()]


def _rec(order_no: str) -> PayParamRecord | None:
    with database.SessionLocal() as db:
        return db.query(PayParamRecord).filter(PayParamRecord.order_no == order_no).first()


def _wipe(order_no: str) -> None:
    with database.SessionLocal() as db:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).first()
        if sess:
            db.query(PayAttempt).filter(PayAttempt.pay_session_id == sess.id).delete()
            db.delete(sess)
        db.query(PayParamRecord).filter(PayParamRecord.order_no == order_no).delete()
        db.query(PayEventLog).filter(PayEventLog.order_no == order_no).delete()
        db.commit()


# 真实 wire 形态收银台 URL（2026-09-28 抓包样本：utdid 含 URL 编码的 '/'）
SAMPLE_URL = ("https://mclient.alipay.com/cashierRoutePay.htm?route_pay_from=h5"
              "&init_from=SDKLite&session=RZZFB105B8PcpDZEFCVbuSiwa8FiuZmobilecashierRZZFB10"
              "&utdid=ard6vj24ouoDAPwfC0RN0%2Fhz"
              "&tid=2e80dd74198790910ee488e446e945e319c838fd490f3d80dcddeae2fe3827aa&cc=y")
SAMPLE_SESSION = "RZZFB105B8PcpDZEFCVbuSiwa8FiuZmobilecashierRZZFB10"
SAMPLE_UTDI = "ard6vj24ouoDAPwfC0RN0/hz"   # 解码后
SAMPLE_TID = "2e80dd74198790910ee488e446e945e319c838fd490f3d80dcddeae2fe3827aa"

# ---------- 4. 测试用例 ----------


def test_01_schema():
    """建表：pay_param_records 表存在且全列就位（独立存储域，关联 order_no/account_id）。"""
    insp = inspect(database.engine)
    assert "pay_param_records" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("pay_param_records")}
    assert {"order_no", "account_id", "pay_token_prefix", "param_str",
            "source", "created_at", "updated_at"} <= cols, cols


def test_02_parse_cashier_url():
    """URL 解析：三元组齐 → 全参数（URL 解码 / 键全保留）；缺 tid / 换域 / http → None。"""
    parsed = pp.parse_cashier_url(SAMPLE_URL)
    assert parsed, "真实形态 URL 应解析成功"
    assert parsed["base_url"] == "https://mclient.alipay.com/cashierRoutePay.htm"
    params = parsed["params"]
    assert params["session"] == SAMPLE_SESSION
    assert params["utdid"] == SAMPLE_UTDI          # %2F → /（URL 解码）
    assert params["tid"] == SAMPLE_TID
    assert params["route_pay_from"] == "h5" and params["init_from"] == "SDKLite" and params["cc"] == "y"
    # 缺 tid → None（宁缺毋滥：半截参数拒收）
    assert pp.parse_cashier_url(SAMPLE_URL.replace(f"&tid={SAMPLE_TID}", "")) is None
    # 域名/协议不符 → None
    assert pp.parse_cashier_url(SAMPLE_URL.replace("https://mclient.alipay.com",
                                                   "https://evil.example.com")) is None
    assert pp.parse_cashier_url(SAMPLE_URL.replace("https://", "http://")) is None
    assert pp.parse_cashier_url("") is None and pp.parse_cashier_url(None) is None


def test_03_build_and_load_roundtrip():
    """生成/解析往返：字段一致（紧凑 JSON 单行）；缺必需参数 → None；坏串 → ValueError。"""
    s = pp.build_param_str(order_no="PP-RT-1", cashier_url=SAMPLE_URL, pay_no="PN-RT",
                           out_trade_no="OT-RT", pay_amount="16.00", total_amount="19.00",
                           source="manual", expires_at="2026-09-28 18:10:11")
    assert s and "\n" not in s, "库内存储为紧凑单行 JSON"
    doc = pp.load_pay_param_str(s)
    assert doc["v"] == 1 and doc["order_no"] == "PP-RT-1"
    assert doc["pay_no"] == "PN-RT" and doc["out_trade_no"] == "OT-RT"
    assert doc["pay_amount"] == "16.00" and doc["total_amount"] == "19.00"
    assert doc["cashier_url"] == SAMPLE_URL and doc["source"] == "manual"
    assert doc["expires_at"] == "2026-09-28 18:10:11" and doc["generated_at"]
    assert doc["params"]["utdid"] == SAMPLE_UTDI
    # 参数不完整 → 生成 None（fail-soft，不落库）
    assert pp.build_param_str(order_no="PP-RT-2",
                              cashier_url=SAMPLE_URL.replace(f"&tid={SAMPLE_TID}", "")) is None
    # 解析端强校验：坏 JSON / 版本不符 / params 缺失 / 必需参数空 → ValueError
    for bad in ("not-json", json.dumps({"v": 2, "params": {"session": "x", "utdid": "y", "tid": "z"}}),
                json.dumps({"v": 1}), json.dumps({"v": 1, "params": {"session": "", "utdid": "y", "tid": "z"}})):
        try:
            pp.load_pay_param_str(bad)
            raise AssertionError(f"坏串应抛 ValueError: {bad[:40]}")
        except ValueError:
            pass


def test_04_save_and_upsert():
    """存储：落库关联字段齐（account_id/token 前缀/source）；二次 save 覆盖仍一单一条。"""
    order = "PP-SAVE-1"
    try:
        with database.SessionLocal() as db:
            sess = ps.ensure_pay_session(db, ACC_ID, order, _link(order))
            rec = pp.save_pay_params(db, order, SAMPLE_URL, source="manual", sess=sess)
            assert rec and rec.order_no == order
            assert rec.account_id == ACC_ID
            assert rec.pay_token_prefix == sess.pay_token[:8]
            assert rec.source == "manual"
            first_id, first_str = rec.id, rec.param_str
        # 覆盖式更新：重铸新短窗仍是同一条记录（unique order_no），source/内容更新
        with database.SessionLocal() as db:
            rec2 = pp.save_pay_params(db, order, SAMPLE_URL.replace(SAMPLE_SESSION, "NEWSESS"),
                                      source="protocol-mint")
            assert rec2.id == first_id and rec2.source == "protocol-mint"
            assert rec2.param_str != first_str
        with database.SessionLocal() as db:
            assert db.query(PayParamRecord).filter(PayParamRecord.order_no == order).count() == 1
            s = pp.get_pay_param_str(db, order)
            assert pp.load_pay_param_str(s)["params"]["session"] == "NEWSESS"
        # 参数不全的 URL：不落库、不覆盖已有记录
        with database.SessionLocal() as db:
            assert pp.save_pay_params(db, order, SAMPLE_URL.replace(f"&tid={SAMPLE_TID}", ""),
                                      source="manual") is None
            assert pp.get_pay_param_str(db, order) == s
    finally:
        _wipe(order)


def test_05_ensure_pay_session_static_config():
    """静态构造联动：注入三元组配置 → 会话 + 参数串两表同落（source=static-config）；
    无配置 → 会话正常、参数串为空（fail-soft 不阻塞主流程）。"""
    order_cfg, order_nocfg = "PP-STATIC-1", "PP-STATIC-2"
    try:
        with _cashier({"session": SAMPLE_SESSION, "utdid": SAMPLE_UTDI, "tid": SAMPLE_TID}):
            with database.SessionLocal() as db:
                sess = ps.ensure_pay_session(db, ACC_ID, order_cfg, _link(order_cfg))
                assert sess.alipay_cashier_url, "注入配置后应静态构造出收银台 URL"
                rec = _rec(order_cfg)
                assert rec, "ensure_pay_session 应同步落支付参数串"
                assert rec.source == "static-config"
                doc = pp.load_pay_param_str(rec.param_str)   # 强校验通过
                assert doc["params"]["tid"] == SAMPLE_TID
                assert doc["pay_amount"] == "16.00"          # 会话上下文自动补齐
        with database.SessionLocal() as db:
            sess2 = ps.ensure_pay_session(db, ACC_ID, order_nocfg, _link(order_nocfg))
            assert sess2.alipay_cashier_url == ""            # 无配置 → 不构造
            assert _rec(order_nocfg) is None                 # → 不落参数串
    finally:
        _wipe(order_cfg)
        _wipe(order_nocfg)


def test_06_fill_back_linkage():
    """自动铸造回填联动：URL + 参数串 + cashier_updated 事件三同步；非 issued 会话放弃。"""
    order, order_paid = "PP-FILL-1", "PP-FILL-2"
    try:
        with database.SessionLocal() as db:
            ps.ensure_pay_session(db, ACC_ID, order, _link(order))
        cashier_mint._fill_back(order, SAMPLE_URL, "protocol-mint")
        with database.SessionLocal() as db:
            sess = db.query(PaySession).filter(PaySession.order_no == order).one()
            assert sess.alipay_cashier_url == SAMPLE_URL[:512]
        rec = _rec(order)
        assert rec and rec.source == "protocol-mint"
        assert pp.load_pay_param_str(rec.param_str)["params"]["session"] == SAMPLE_SESSION
        assert _events(order) == ["link_issued", "cashier_updated"]
        # 会话已支付（非 issued）→ 回填整体放弃（URL 与参数串都不动）
        with database.SessionLocal() as db:
            ps.ensure_pay_session(db, ACC_ID, order_paid, _link(order_paid))
            ps.mark_session(db, order_paid, "paid")
        cashier_mint._fill_back(order_paid, SAMPLE_URL, "protocol-mint")
        with database.SessionLocal() as db:
            sess = db.query(PaySession).filter(PaySession.order_no == order_paid).one()
            assert sess.alipay_cashier_url == ""
        assert _rec(order_paid) is None
    finally:
        _wipe(order)
        _wipe(order_paid)


def test_07_cashier_url_endpoint():
    """POST cashier-url 人工回填：响应带 pay_params/pay_param_str + 库记录；GET cashier 透出。"""
    order = "PP-EP-1"
    base = f"/api/ops/accounts/{ACC_ID}/orders/{order}"
    try:
        with database.SessionLocal() as db:
            ps.ensure_pay_session(db, ACC_ID, order, _link(order))
        r = CLIENT.post(f"{base}/cashier-url", json={"url": SAMPLE_URL})
        assert r.status_code == 401, "无 token 应 401"
        r = CLIENT.post(f"{base}/cashier-url", json={"url": SAMPLE_URL}, headers=_auth())
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["alipay_cashier_url"] == SAMPLE_URL
        assert d["pay_params"]["params"]["utdid"] == SAMPLE_UTDI    # 解析对象
        assert d["pay_param_str"] == _rec(order).param_str         # 紧凑原文与库内一致
        r = CLIENT.get(f"{base}/cashier", headers=_auth())
        d = r.json()
        assert d["alipay_cashier_url"] == SAMPLE_URL
        assert d["pay_param_str"] == _rec(order).param_str
        assert d["pay_params"]["v"] == 1
        # 形态不符的 URL 仍被端点原有校验拦下（400，未触及参数串生成；
        # 长度需 ≥32 先过 Pydantic min_length，此处才能到达端点自身的形态校验）
        r = CLIENT.post(f"{base}/cashier-url",
                        json={"url": "https://example.com/pay/xxxx?session=placeholder"},
                        headers=_auth())
        assert r.status_code == 400
    finally:
        _wipe(order)


def test_08_pay_params_endpoint():
    """GET pay-params 显式端点：200 全字段（generated/解析对象/原文）；无 token 401；无会话 404。"""
    order = "PP-EP-2"
    url = f"/api/ops/accounts/{ACC_ID}/orders/{order}/pay-params"
    assert CLIENT.get(url).status_code == 401
    r = CLIENT.get(url, headers=_auth())
    assert r.status_code == 404, "无支付会话应 404"
    try:
        with database.SessionLocal() as db:
            ps.ensure_pay_session(db, ACC_ID, order, _link(order))
        r = CLIENT.get(url, headers=_auth())
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["order_no"] == order and d["session_status"] == "issued"
        assert d["generated"] is False and d["pay_params"] is None and d["pay_param_str"] == ""
        cashier_mint._fill_back(order, SAMPLE_URL, "frida-mint")
        r = CLIENT.get(url, headers=_auth())
        d = r.json()
        assert d["generated"] is True
        assert d["pay_params"]["params"]["session"] == SAMPLE_SESSION
        assert d["pay_param_str"] and d["alipay_cashier_url"] == SAMPLE_URL
    finally:
        _wipe(order)


def test_09_payload_fields_and_wiring():
    """下发与兜底：payload 有记录随单下发 / 无记录空字段；坏 JSON 不抛（原文透出）。"""
    order_a, order_b = "PP-PAY-1", "PP-PAY-2"
    try:
        # 无参数串：字段为空不报错
        p = ps.pay_link_payload_with_session(database.SessionLocal(), _link(order_a), ACC_ID)
        assert p["pay_params"] is None and p["pay_param_str"] == ""
        # 有参数串：随单下发且与库内一致
        with database.SessionLocal() as db:
            ps.ensure_pay_session(db, ACC_ID, order_a, _link(order_a))
        cashier_mint._fill_back(order_a, SAMPLE_URL, "protocol-mint")
        p = ps.pay_link_payload_with_session(database.SessionLocal(), _link(order_a), ACC_ID)
        assert p["pay_param_str"] == _rec(order_a).param_str
        assert p["pay_params"]["order_no"] == order_a
        # 坏数据兜底：库中 param_str 被写坏 → payload_fields 不抛、原文透出、解析端显式 ValueError
        with database.SessionLocal() as db:
            db.query(PayParamRecord).filter(PayParamRecord.order_no == order_a).update(
                {"param_str": "{broken-json"})
            db.commit()
        fields = pp.payload_fields(database.SessionLocal(), order_a)
        assert fields["pay_params"] is None and fields["pay_param_str"] == "{broken-json"
        try:
            pp.load_pay_param_str("{broken-json")
            raise AssertionError("坏串应抛 ValueError")
        except ValueError:
            pass
    finally:
        _wipe(order_a)
        _wipe(order_b)


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
    ps._CASHIER_CFG_TTL = _ORIG_CASHIER_TTL
    ps._CASHIER_CFG_CACHE.update(cfg=None, at=0.0)
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
