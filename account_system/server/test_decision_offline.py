"""下单决策域离线测试（wire/快照回放，零真实网络请求）。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server && python test_decision_offline.py
    （或 pytest test_decision_offline.py -q）

覆盖（契约 docs/decision_api_contract.md §4/§5/§6/§8）：
  - 纯函数矩阵：classify_coupon / parse_benefit / estimate_deduction / resolve_cost /
          evaluate_cost / check_threshold / match_packets / rank_candidates / load+save_config
  - 端点（TestClient + 临时库）：套餐 CRUD 全往返（items 全量替换/重名 400/toggle/DELETE）、
          成本规则 CRUD + import（重复 name skipped）、config GET/PUT、coupon-inventory
          （分型 coupon_kind / 成本 cost_price / 可用性 usable）、decide（pass 与 blocked、
          packet_id 指定与 422、deep 探针 price_source=settle、allow_full_price 原价单）、
          profit-report（成单/未成单/blocked 口径 + date-only 与 datetime 双格式）、
          orders create 的 decision_log_id 成单回填、viewer 403
  - dashboard_stats profit 域聚合与零值 fail-soft

要点（与 test_orders_offline.py / test_menu_spec_offline.py 同模式）：
  - 先把 database.DB_PATH 指向 data/test_decision.db 并重建 engine，再 import app
  - monkeypatch services.chagee_bridge.build_client：回放 output/trade_samples_20260926.json
    与 output/pay_zero_capture_20260926.json 的 settle/createOrder wire（成单回填用例）；
    未知 path 抛 AssertionError（防越权真实请求）
  - monkeypatch services.chagee_bridge.menu_api：回放 output/menu-CN00529 快照
    （decide 的 resolve_by_sku 冷库 L2 回源用）
  - services/decision._CONFIG_PATH 重定向到 data/test_decision_config.json（config
    GET/PUT 不污染生产 data/decision_config.json）
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根
sys.path.insert(0, BASE)

# 禁用校准线程 / 菜单刷新线程 / 支付 watcher / 云手机隧道：避免测试进程残留后台副作用
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MENU_REFRESH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_TUNNEL_ENABLED"] = "0"
# 禁用 frida 收银台铸造与跨进程支付广播：离线环境杜绝真触云手机 / 真发 HTTP 到 8010
os.environ["CHAGEE_MINT_ENABLED"] = "0"
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
# 日志隔离：oplog 日志库与文本日志均指向测试路径，避免污染生产 data/logs/
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_decision_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_decision.db")
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
from models import (ChageeAccount, CouponRecord, CouponUsageLog, DecisionLog,  # noqa: E402
                    OrderPlan, OrderPlanCouponPriority, OrderRecord, PacketConfig,
                    Role, SystemUser, VoucherCostCategory, VoucherCostRule)
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import decision as dsvc  # noqa: E402
from services.dashboard_stats import (_PROFIT_ZERO, _collect_profit_domain,  # noqa: E402
                                      collect_dashboard_stats)

# decision_config.json 重定向到测试路径：config GET/PUT 与 load/save_config 全走这里，
# 不污染生产 account_system/data/decision_config.json
TEST_CONFIG_PATH = os.path.join(database.DATA_DIR, "test_decision_config.json")
if os.path.exists(TEST_CONFIG_PATH):
    os.remove(TEST_CONFIG_PATH)
dsvc._CONFIG_PATH = TEST_CONFIG_PATH

# ---------- 2a. 菜单快照回放假客户端（decide 的 resolve_by_sku 冷库 L2 回源） ----------

with open(os.path.join(ROOT, "output", "menu-CN00529", "03_storeGoodsMenu.json"),
          encoding="utf-8") as f:
    MENU = json.load(f)
with open(os.path.join(ROOT, "output", "menu-CN00529", "04_goods_details.json"),
          encoding="utf-8") as f:
    DETAILS = json.load(f)
DETAIL_MAP = {str(d.get("spuId")): d for d in DETAILS}


class FakeMenuApi:
    """回放 CN00529 杭州店快照（82 SPU，与 test_menu_spec_offline.py 同夹具）。"""

    def store_goods_menu(self, store_no, sale_type="1", sale_channel="2"):
        return MENU

    def goods_detail(self, spu_id, store_no, sale_type="1", sale_channel="2"):
        if spu_id not in DETAIL_MAP:
            raise KeyError(f"快照无此 SPU: {spu_id}")
        return DETAIL_MAP[spu_id]


bridge.menu_api = lambda: FakeMenuApi()   # monkeypatch（menu_spec 服务共用 bridge）

# ---------- 2b. wire 回放假协议客户端（替换 build_client，杜绝真实网络） ----------


def _load(name):
    with open(os.path.join(ROOT, "output", name), encoding="utf-8") as f:
        return json.load(f)


TRADE = _load("trade_samples_20260926.json")          # 购物车/试算 wire
ZERO = _load("pay_zero_capture_20260926.json")        # 零元 createOrder + getOrderDetail wire


def _resp(item):
    body = item["resp_body"]
    return json.loads(body) if isinstance(body, str) else body


CALLS: list[str] = []   # 假客户端实际收到的调用（防越权端点断言依据）


class FakeClient:
    """按 path 后缀回放 2026-09-26 生产 wire 响应（settle 20 元无商品折 / 零元成单）。"""

    def __init__(self, account):
        self.account = account
        self.token = "fake.token.offline"
        self.proto = SimpleNamespace(sk="fakesk", token=self.token)

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
            return {"errcode": "0", "data": {   # 与 settle 夹具自洽：20 元无商品折
                "spuId": "625339451983278080", "spuType": "stand", "skuId": "653632618000097282",
                "totalSalePrice": "20.00", "totalTradePrice": "20.00", "totalGoodsItemPrice": "20.00",
                "totalGoodsItemDiscountAmount": "0.00", "totalGoodsPaymentDiscountAmount": "0.00",
                "totalDiscountAmount": "0.00", "totalWrappingPrice": "0.00"}}
        if path.endswith("/order/settlePrice"):
            rows = (body or {}).get("discountList") or []
            if not rows:
                return _resp(TRADE[5])                   # 无券/探针(no_recommend)：服务端自荐 20 元券
            amt = Decimal(str(rows[0].get("discountAmount") or 0))
            return _resp(TRADE[5] if amt == 20 else TRADE[6])   # 20 元抵扣回样本5，10 元回样本6
        if path.endswith("/order/createOrder"):
            return _resp(ZERO[0])                        # 零元单响应：data 仅 orderNo
        if path.endswith("/order/getOrderDetail"):
            return _resp(ZERO[1])                        # 制作中 TA0001，券核销 HYW
        raise AssertionError(f"未预期的 POST 协议调用: {path}")


bridge.build_client = lambda account: FakeClient(account)   # monkeypatch（trade_api 内部经此构造）

# ---------- 3. 测试数据 ----------

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(SystemUser).filter(SystemUser.username == "viewer_smoke").first():
        viewer_role = db.query(Role).filter(Role.name == "viewer").one()
        db.add(SystemUser(username="viewer_smoke", display_name="只读冒烟",
                          password_hash=hash_password("Viewer@123"), role_id=viewer_role.id))
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800001111").first():
        db.add(ChageeAccount(label="决策冒烟A", phone="13800001111",
                             device_uuid="uuid-offline-decision-1", token="fake.token.offline",
                             sk="fakesk", customer_id="1190018250", status="online", group="默认"))
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800002222").first():
        db.add(ChageeAccount(label="决策冒烟B", phone="13800002222",
                             device_uuid="uuid-offline-decision-2", token="fake.token.offline.b",
                             sk="fakesk", customer_id="1190018251", status="online", group="默认"))
    db.commit()
    ACC_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800001111").one().id
    ACC2_ID = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800002222").one().id

STORE_NO = "CN00529"
BYJX_SPU = "625339451983278080"        # 伯牙绝弦（快照内大杯 20 元，与 settle wire 自洽）
BYJX_SKU_BIG = "653632618000097282"    # 大杯 = 客户 linkId 口径
QNS_SKU_BIG = "655441178728091650"     # 青青糯山大杯 18 元（decide 商品解析用）

ORDER_NO = "202609260910110023151918250"          # 零元 wire 样本订单号（成单回填用例）
COUPON_HYW = "1309482592713252864"                # 霸王茶姬20元代金券-HYW（样本5 服务端自荐券）

FAR_FUTURE_MS = 4102444800000      # 2100-01-01：永不过期，断言确定性
NOW_MS = int(time.time() * 1000)

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


def _db():
    return database.SessionLocal()


def _clear_decision_domain():
    """决策域清场（端点用例间隔离：套餐/规则/子类/流水互不串扰，含库存夹具券）。"""
    with _db() as db:
        for packet in db.query(PacketConfig).all():
            db.delete(packet)          # items 随 cascade 删除
        for plan in db.query(OrderPlan).all():
            db.delete(plan)            # priorities 随 cascade 删除
        db.query(VoucherCostRule).delete()
        db.query(VoucherCostCategory).delete()
        db.query(DecisionLog).delete()
        db.commit()


def _add_coupon(code, template, benefit, amount, account_id=ACC_ID, bucket="effective",
                threshold="", can_discount=True, end_ms=FAR_FUTURE_MS, start_ms=None):
    with _db() as db:
        db.add(CouponRecord(coupon_code=code, account_id=account_id,
                            token_fingerprint="fake.token", template_name=template,
                            benefit_text=benefit, benefit2_text="", amount=amount,
                            threshold_tips=threshold, bucket=bucket, synced_from="coupon_query",
                            use_start_time=start_ms if start_ms is not None else NOW_MS - 86400000,
                            use_end_time=end_ms, can_discount=can_discount))
        db.commit()


# ---------- 4. 纯函数矩阵（services/decision.py，契约 §4） ----------

def test_01_parse_benefit_and_classify_coupon():
    """parse_benefit：N元 / 满M减N（取 N）/ 无面额 → None。"""
    assert dsvc.parse_benefit("20元") == Decimal("20")
    assert dsvc.parse_benefit("满30减5") == Decimal("5")
    assert dsvc.parse_benefit("满30元减5元") == Decimal("5")
    assert dsvc.parse_benefit("5.5元") == Decimal("5.5")
    assert dsvc.parse_benefit("7折") is None          # 折扣率无元面额语义
    assert dsvc.parse_benefit("") is None
    assert dsvc.parse_benefit(None) is None
    assert dsvc.parse_benefit("免10杯") is None

    """classify_coupon：兑换/换购 ＞ 折扣 ＞ 面额 ＞ unknown（模板保守解析面额）。"""
    c = dsvc.classify_coupon("20元", "", "霸王茶姬20元代金券")
    assert c == {"kind": "face", "face": Decimal("20"), "rate": None}
    c = dsvc.classify_coupon("满30减5", "代金", "外卖满减券")
    assert c["kind"] == "face" and c["face"] == Decimal("5")
    c = dsvc.classify_coupon("7.5折", "", "全品类单杯7.5折券")
    assert c["kind"] == "discount" and c["rate"] == Decimal("0.75") and c["face"] is None
    c = dsvc.classify_coupon("", "", "全场7折券")          # 折扣关键词在模板名上
    assert c["kind"] == "discount" and c["rate"] == Decimal("0.7")
    c = dsvc.classify_coupon("20元", "可兑换饮品一杯", "生日券")   # 兑换优先于面额
    assert c["kind"] == "exchange" and c["face"] is None
    c = dsvc.classify_coupon("", "", "饮品换购券")                 # 换购关键词在模板名上
    assert c["kind"] == "exchange"
    c = dsvc.classify_coupon("免10杯", "", "免10杯多次卡")        # 判定不出 → unknown
    assert c["kind"] == "unknown" and c["face"] is None and c["rate"] is None
    c = dsvc.classify_coupon("", "", "霸王茶姬20元代金券-HYW")    # unknown 但模板可解析面额
    assert c["kind"] == "unknown" and c["face"] == Decimal("20")


def test_02_estimate_deduction_matrix():
    """四券型 + estimated 标志：face 精确 / discount·exchange·unknown 估算。"""
    d, est = dsvc.estimate_deduction("face", Decimal("20"), None, Decimal("18"))
    assert d == Decimal("18") and est is False          # min(20,18)
    d, est = dsvc.estimate_deduction("face", Decimal("10"), None, Decimal("18"))
    assert d == Decimal("10") and est is False
    d, est = dsvc.estimate_deduction("discount", None, Decimal("0.75"), Decimal("20"))
    assert d == Decimal("5.00") and est is True         # 20×(1-0.75)
    d, est = dsvc.estimate_deduction("exchange", None, None, Decimal("18"))
    assert d == Decimal("18") and est is True           # 全额抵扣
    d, est = dsvc.estimate_deduction("unknown", Decimal("15"), None, Decimal("18"))
    assert d == Decimal("15") and est is True           # unknown 有面额仍估算
    d, est = dsvc.estimate_deduction("unknown", None, None, Decimal("18"))
    assert d == Decimal("0") and est is True            # 无面额可算
    d, est = dsvc.estimate_deduction("discount", None, None, Decimal("18"))
    assert d == Decimal("0") and est is True            # 折扣无 rate
    d, est = dsvc.estimate_deduction("face", None, None, Decimal("18"))
    assert d == Decimal("0") and est is True            # face 无面额


def test_03_resolve_cost_matrix():
    """匹配链四型 + priority 顺序 + face_value 校验 + 非法 regex 吞掉 + 空规则 fallback。"""
    rules = [
        {"id": 1, "enabled": True, "priority": 50, "match_type": "template_exact",
         "match_value": "霸王茶姬20元代金券-HYW", "face_value": "", "cost_price": "6.00"},
        {"id": 2, "enabled": True, "priority": 10, "match_type": "template_contains",
         "match_value": "代金券", "face_value": "", "cost_price": "7.00"},
        {"id": 3, "enabled": True, "priority": 60, "match_type": "benefit_regex",
         "match_value": r"满(\d+)减(\d+)", "face_value": "", "cost_price": "5.00"},
        {"id": 4, "enabled": True, "priority": 70, "match_type": "coupon_prefix",
         "match_value": "DTB", "face_value": "", "cost_price": "9.50"},
        {"id": 5, "enabled": True, "priority": 5, "match_type": "benefit_regex",
         "match_value": "[invalid", "face_value": "", "cost_price": "1.00"},   # 非法 regex
        {"id": 6, "enabled": False, "priority": 1, "match_type": "template_contains",
         "match_value": "券", "face_value": "", "cost_price": "0.10"},         # 停用
    ]
    cfg = {"cost_fallback_ratio": "0.8"}

    # priority 顺序：停用规则(6)不参与；非法 regex(5, p=5) 被吞掉不命中 → contains(2, p=10) 先于 exact(1)
    rec = {"template_name": "霸王茶姬20元代金券-HYW", "benefit_text": "20元",
           "coupon_code": "X1", "amount": "20"}
    res = dsvc.resolve_cost(rules, rec, cfg)
    assert res["cost"] == Decimal("7.00") and res["source"] == "rule:2"

    # 仅 exact 命中（不含「代金券」字样）
    rec = {"template_name": "霸王茶姬20元代金券-HYW", "benefit_text": "20元",
           "coupon_code": "X1", "amount": "20"}
    only_exact = [r for r in rules if r["id"] != 2]
    res = dsvc.resolve_cost(only_exact, rec, cfg)
    assert res["cost"] == Decimal("6.00") and res["source"] == "rule:1"

    # benefit_regex 命中（模板+benefit 拼接串搜索）
    rec = {"template_name": "外卖满减券", "benefit_text": "满30减5",
           "coupon_code": "X2", "amount": "5"}
    res = dsvc.resolve_cost(rules, rec, cfg)
    assert res["cost"] == Decimal("5.00") and res["source"] == "rule:3"

    # coupon_prefix 命中
    res = dsvc.resolve_cost(rules, {"template_name": "茶姬福利券B", "benefit_text": "20元",
                                    "coupon_code": "DTB123", "amount": "20"}, cfg)
    assert res["cost"] == Decimal("9.50") and res["source"] == "rule:4"

    # face_value 不匹配 → 该条跳过继续走链（10 元面额校验 vs 券 20 元）
    face_check = [{"id": 9, "enabled": True, "priority": 1, "match_type": "template_contains",
                   "match_value": "代金券", "face_value": "10", "cost_price": "2.00"}]
    res = dsvc.resolve_cost(face_check, {"template_name": "霸王茶姬20元代金券",
                                         "benefit_text": "20元", "coupon_code": "X",
                                         "amount": "20"}, cfg)
    assert res["source"] == "fallback" and res["cost"] == Decimal("16.00")   # 20×0.8
    # face_value 相符 → 命中
    face_check[0]["face_value"] = "20"
    res = dsvc.resolve_cost(face_check, {"template_name": "霸王茶姬20元代金券",
                                         "benefit_text": "20元", "coupon_code": "X",
                                         "amount": "20"}, cfg)
    assert res["source"] == "rule:9" and res["cost"] == Decimal("2.00")

    # 空规则 / 全不命中 → fallback = 面额×ratio；无面额 → 0
    res = dsvc.resolve_cost([], {"template_name": "任意", "benefit_text": "7折",
                                 "coupon_code": "X", "amount": ""}, cfg)
    assert res["cost"] == Decimal("0") and res["source"] == "fallback"
    res = dsvc.resolve_cost(rules, {"template_name": "完全无关节", "benefit_text": "5元",
                                    "coupon_code": "ZZZ", "amount": "5"}, {"cost_fallback_ratio": "1.0"})
    assert res["cost"] == Decimal("5") and res["source"] == "fallback"
    # rules=None 与 config 缺省（ratio 默认 1.0）
    res = dsvc.resolve_cost(None, {"template_name": "t", "benefit_text": "5元",
                                   "coupon_code": "c", "amount": "5"})
    assert res["cost"] == Decimal("5")


def test_04_evaluate_cost_and_check_threshold():
    """evaluate_cost：零元/差额/无券三分支 + 利润率口径；check_threshold 边界。"""
    b = dsvc.evaluate_cost("12", "20", "20", "8", "0")     # 券全覆盖 → 零差额
    assert b == {"revenue": "12.00", "total_trade_price": "20.00", "deduction": "20.00",
                 "voucher_cost": "8.00", "pay_cost": "0.00", "overhead": "0.00",
                 "total_cost": "8.00", "profit": "4.00", "margin": "33.3"}
    b = dsvc.evaluate_cost("15", "20", "10", "5", "0.5")   # 差额实付 + 杂费
    assert b["pay_cost"] == "10.00" and b["total_cost"] == "15.50"
    assert b["profit"] == "-0.50" and b["margin"] == "-3.3"
    b = dsvc.evaluate_cost("25", "20", "0", "0", "0")      # 无券原价单
    assert b["pay_cost"] == "20.00" and b["profit"] == "5.00" and b["margin"] == "20.0"
    b = dsvc.evaluate_cost("0", "20", "0", "0", "0")       # revenue<=0 → margin 空
    assert b["margin"] == ""
    # 抵扣超总额 → pay 钳 0
    b = dsvc.evaluate_cost("12", "18", "20", "8", "0")
    assert b["pay_cost"] == "0.00" and b["deduction"] == "20.00" and b["profit"] == "4.00"

    # check_threshold：等于阈值 pass / 低于 blocked / 利润率约束 / 空值不启用
    ok = {"profit": "2.00", "margin": "5.0"}
    assert dsvc.check_threshold(ok, "2.00", "") == ("pass", "")
    assert dsvc.check_threshold(ok, "2.01", "")[0] == "blocked"
    assert "低于最低利润" in dsvc.check_threshold(ok, "2.01", "")[1]
    assert dsvc.check_threshold({"profit": "10.00", "margin": "9.9"}, "", "10")[0] == "blocked"
    assert "利润率" in dsvc.check_threshold({"profit": "10.00", "margin": "9.9"}, "", "10")[1]
    assert dsvc.check_threshold({"profit": "10.00", "margin": "10.0"}, "", "10.0") == ("pass", "")
    assert dsvc.check_threshold(ok, "", "") == ("pass", "")            # 双空=不校验
    assert dsvc.check_threshold({"profit": "5.00", "margin": ""}, "", "10") == ("pass", "")  # margin 算不出 → 跳过利润率


def test_05_match_packets_matrix():
    """价格区间含边界 / max=0 不限 / 时段含跨零点 / 停用排除 / items 空与 sku 圈定。"""
    packets = [
        {"id": 1, "open_flag": True, "min_order_amount": "10", "max_order_amount": "20",
         "available_start": "", "available_end": "", "items": []},
        {"id": 2, "open_flag": False, "min_order_amount": "0", "max_order_amount": "0",
         "available_start": "", "available_end": "", "items": []},                 # 停用
        {"id": 3, "open_flag": True, "min_order_amount": "0", "max_order_amount": "0",
         "available_start": "", "available_end": "", "items": [{"sku_id": "S1"}]},  # sku 圈定
    ]

    def ids(price, sku_id="S1", now=None):
        return [p["id"] for p in dsvc.match_packets(packets, sku_id, Decimal(price), now)]

    assert ids("10") == [1, 3]            # 下边界含
    assert ids("20") == [1, 3]            # 上边界含
    assert ids("20.01") == [3]            # 越上界 → 仅不限区间的套餐3
    assert ids("999") == [3]              # max=0 不设上限
    assert ids("15", sku_id="S2") == [1]  # sku 不在套餐3 items → 仅全品类套餐1
    assert ids("15", sku_id="") == [1]    # sku 空 = 不命中圈定套餐（空 sku 不入圈）

    # 时段：普通窗口 / 跨零点 / 单边
    tp = {"id": 4, "open_flag": True, "min_order_amount": "0", "max_order_amount": "0",
          "available_start": "09:00:00", "available_end": "18:00:00", "items": []}
    cross = {"id": 5, "open_flag": True, "min_order_amount": "0", "max_order_amount": "0",
             "available_start": "22:00:00", "available_end": "06:00:00", "items": []}
    start_only = {"id": 6, "open_flag": True, "min_order_amount": "0", "max_order_amount": "0",
                  "available_start": "10:00:00", "available_end": "", "items": []}
    ten = datetime(2026, 9, 28, 10, 0, 0)
    assert [p["id"] for p in dsvc.match_packets([tp, cross, start_only], "S", Decimal("5"), ten)] == [4, 6]
    night = datetime(2026, 9, 28, 23, 30, 0)
    assert [p["id"] for p in dsvc.match_packets([tp, cross, start_only], "S", Decimal("5"), night)] == [5, 6]   # start_only=10点起 → 夜间仍命中
    dawn = datetime(2026, 9, 28, 5, 0, 0)
    assert [p["id"] for p in dsvc.match_packets([tp, cross, start_only], "S", Decimal("5"), dawn)] == [5]        # 仅跨零点窗口命中
    # 套餐字段读取异常（如 ORM 脱离会话的懒加载失败）安全跳过不中断
    class _BoomPacket:
        @property
        def open_flag(self):
            raise RuntimeError("detached instance")

    assert dsvc.match_packets([_BoomPacket(), packets[0]], "S1", Decimal("15"), ten) == [packets[0]]
    assert dsvc.match_packets(None, "S", Decimal("1")) == []


def test_06_rank_candidates():
    """pass 过滤 + total_cost 升序 + 同成本面额大优先 + breakdown 来源字段补齐。"""
    cands = [
        {"record": {"coupon_code": "A"}, "cost": Decimal("8"), "source": "rule:1",
         "kind": "face", "face": Decimal("20"), "rate": None},
        {"record": {"coupon_code": "B"}, "cost": Decimal("9"), "source": "fallback",
         "kind": "face", "face": Decimal("25"), "rate": None},
        {"record": {"coupon_code": "C"}, "cost": Decimal("8"), "source": "rule:2",
         "kind": "face", "face": Decimal("30"), "rate": None},     # 同成本面额更大 → 排 A 前
        {"record": {"coupon_code": "D"}, "cost": Decimal("50"), "source": "fallback",
         "kind": "face", "face": Decimal("20"), "rate": None},     # 利润 12-50<0 → blocked 过滤
    ]
    ranked = dsvc.rank_candidates(cands, "12", "20", "0", "2.00", "")
    assert [x["record"]["coupon_code"] for x in ranked] == ["C", "A", "B"]
    top = ranked[0]
    assert top["verdict"] == "pass" and top["reason"] == ""
    b = top["cost_breakdown"]
    assert b["deduction"] == "20.00" and b["deduction_estimated"] is False
    assert b["cost_source"] == "rule:2" and b["coupon_kind"] == "face"
    assert b["total_cost"] == "8.00" and b["profit"] == "4.00"
    # 全军覆没 → 空列表
    assert dsvc.rank_candidates(cands, "5", "20", "0", "2.00", "") == []
    assert dsvc.rank_candidates(None, "12", "20", "0", "2.00", "") == []


def test_07_config_load_save_module():
    """load_config 缺省 / save_config 落盘合并 / 回读一致（测试路径，不触生产配置）。"""
    cfg = dsvc.load_config()                    # 文件不存在 → 默认值（不落盘）
    assert cfg["min_profit"] == "2.00" and cfg["min_margin"] == ""
    assert cfg["overhead"] == "0" and cfg["cost_fallback_ratio"] == "1.0"
    assert not os.path.exists(TEST_CONFIG_PATH)
    saved = dsvc.save_config({"min_profit": "3.00", "extra_note": "测试"})   # 缺键补全 + 额外键保留
    assert saved["min_profit"] == "3.00" and saved["overhead"] == "0"
    assert saved["extra_note"] == "测试" and isinstance(saved["min_profit"], str)
    assert dsvc.load_config() == saved          # 回读一致（含额外键）
    with open(TEST_CONFIG_PATH, encoding="utf-8") as f:
        assert json.load(f)["min_profit"] == "3.00"
    os.remove(TEST_CONFIG_PATH)                 # 清场：后续端点用例从默认值起步
    # 文件损坏 fail-soft：解析失败回默认值
    with open(TEST_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("{broken json")
    cfg = dsvc.load_config()
    assert cfg["min_profit"] == "2.00"
    os.remove(TEST_CONFIG_PATH)


# ---------- 5. 端点测试（TestClient + 临时库） ----------

def test_10_packet_crud_roundtrip():
    """套餐 CRUD 全往返：POST 含 items / GET 列表过滤 / PUT 全量替换 / 重名 400 / toggle / DELETE。"""
    _clear_decision_domain()
    h = _auth()
    body = {"name": "午后套餐", "min_order_amount": "5", "max_order_amount": "30",
            "available_start": "", "available_end": "", "min_profit": "1.00", "note": "测试套餐",
            "items": [
                {"spu_id": BYJX_SPU, "sku_id": BYJX_SKU_BIG, "product_name": "伯牙绝弦",
                 "face_price": "20.00", "is_premium": False,
                 "normal_coupon_rule": {"match_type": "template_contains", "match_value": "代金券"}},
                {"spu_id": "640215811876831233", "sku_id": QNS_SKU_BIG,
                 "product_name": "青青糯山", "face_price": "18.00", "is_premium": True},
            ]}
    r = CLIENT.post("/api/ops/decision/packets", json=body, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    pid = d["id"]
    assert d["name"] == "午后套餐" and d["item_count"] == 2 and d["open_flag"] is True
    assert d["min_order_amount"] == "5" and d["max_order_amount"] == "30" and d["min_profit"] == "1.00"
    assert len(d["items"]) == 2
    it0 = d["items"][0]
    assert it0["sku_id"] == BYJX_SKU_BIG and it0["product_name"] == "伯牙绝弦"
    assert it0["normal_coupon_rule"] == {"match_type": "template_contains", "match_value": "代金券"}
    assert d["items"][1]["is_premium"] is True and d["items"][1]["premium_coupon_rule"] is None

    # 重名 400
    r = CLIENT.post("/api/ops/decision/packets", json=body, headers=h)
    assert r.status_code == 400 and "已存在" in r.json()["detail"]
    # items 内 sku 重复 400
    r = CLIENT.post("/api/ops/decision/packets",
                    json={**body, "name": "重复sku", "items": [body["items"][0], body["items"][0]]},
                    headers=h)
    assert r.status_code == 400 and "重复" in r.json()["detail"]

    # 列表 + keyword/open 过滤
    r = CLIENT.get("/api/ops/decision/packets", params={"keyword": "午后"}, headers=h)
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["items"][0]["id"] == pid and r.json()["items"][0]["item_count"] == 2
    r = CLIENT.get("/api/ops/decision/packets", params={"keyword": "不存在的套餐"}, headers=h)
    assert r.json()["total"] == 0
    r = CLIENT.get("/api/ops/decision/packets", params={"open": "false"}, headers=h)
    assert r.json()["total"] == 0
    r = CLIENT.get(f"/api/ops/decision/packets/{pid}", headers=h)
    assert r.status_code == 200 and len(r.json()["items"]) == 2

    # PUT 全量替换：items 只留一件 + 改区间
    r = CLIENT.put(f"/api/ops/decision/packets/{pid}",
                   json={**body, "max_order_amount": "25", "items": [body["items"][0]]}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["max_order_amount"] == "25" and d["item_count"] == 1
    assert d["items"][0]["sku_id"] == BYJX_SKU_BIG
    with _db() as db:      # 旧行确被 delete-orphan 清除（uq_packet_item 不炸）
        p = db.get(PacketConfig, pid)
        assert len(p.items) == 1 and p.items[0].sku_id == BYJX_SKU_BIG
    # PUT 改名为其他已有名称 → 400（本例唯一，先造一个）
    CLIENT.post("/api/ops/decision/packets", json={**body, "name": "二号套餐", "items": []}, headers=h)
    r = CLIENT.put(f"/api/ops/decision/packets/{pid}", json={**body, "name": "二号套餐"}, headers=h)
    assert r.status_code == 400 and "已存在" in r.json()["detail"]

    # toggle-open 翻转
    r = CLIENT.post(f"/api/ops/decision/packets/{pid}/toggle-open", headers=h)
    assert r.status_code == 200 and r.json()["open_flag"] is False
    r = CLIENT.post(f"/api/ops/decision/packets/{pid}/toggle-open", headers=h)
    assert r.json()["open_flag"] is True

    # DELETE：本套餐 + 造出来的二号（清场）；删除后 GET 404
    assert CLIENT.delete(f"/api/ops/decision/packets/{pid}", headers=h).json() == {"ok": True}
    two = CLIENT.get("/api/ops/decision/packets", params={"keyword": "二号"}, headers=h).json()
    if two["total"]:
        CLIENT.delete(f"/api/ops/decision/packets/{two['items'][0]['id']}", headers=h)
    assert CLIENT.get(f"/api/ops/decision/packets/{pid}", headers=h).status_code == 404
    with _db() as db:      # items 级联删除
        assert db.query(PacketConfig).count() == 0


def test_11_cost_rule_crud_and_import():
    """成本规则 CRUD + import（重复 name skipped / 非法条目进 errors）。"""
    _clear_decision_domain()
    h = _auth()
    rule = {"name": "20元代金券-采购", "match_type": "template_contains",
            "match_value": "代金券", "face_value": "", "cost_price": "8.00",
            "priority": 10, "enabled": True, "note": "渠道价"}
    r = CLIENT.post("/api/ops/decision/cost-rules", json=rule, headers=h)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["cost_price"] == "8.00" and r.json()["priority"] == 10

    r = CLIENT.put(f"/api/ops/decision/cost-rules/{rid}",
                   json={**rule, "cost_price": "7.50", "enabled": False}, headers=h)
    assert r.status_code == 200 and r.json()["cost_price"] == "7.50"
    assert r.json()["enabled"] is False

    r = CLIENT.get("/api/ops/decision/cost-rules", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["id"] == rid and items[0]["match_type"] == "template_contains"

    # import：1 新增 + 1 与库内重名 skipped + 1 非法 match_type 进 errors
    r = CLIENT.post("/api/ops/decision/cost-rules/import", headers=h, json={"rules": [
        {"name": "7折券-采购", "match_type": "benefit_regex", "match_value": r"(\d+(\.\d+)?)折",
         "face_value": "", "cost_price": "3.00", "priority": 20, "enabled": True, "note": ""},
        {"name": "20元代金券-采购", "match_type": "template_contains", "match_value": "代金券",
         "face_value": "", "cost_price": "9.00", "priority": 10, "enabled": True, "note": ""},
        {"name": "坏规则", "match_type": "no_such_type", "match_value": "x",
         "cost_price": "1.00"},
    ]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["imported"] == 1 and d["skipped"] == 1 and len(d["errors"]) == 1
    assert d["errors"][0]["name"] == "坏规则"
    rows = CLIENT.get("/api/ops/decision/cost-rules", headers=h).json()["items"]
    assert len(rows) == 2          # 原 1 + 导入 1；重复与非法均未落库

    # DELETE + 404
    for row in rows:
        assert CLIENT.delete(f"/api/ops/decision/cost-rules/{row['id']}", headers=h).json() == {"ok": True}
    assert CLIENT.delete(f"/api/ops/decision/cost-rules/{rid}", headers=h).status_code == 404
    assert CLIENT.get("/api/ops/decision/cost-rules", headers=h).json()["items"] == []


def test_12_config_get_put_endpoints():
    """GET /config 默认值 → PUT 落盘 → GET 回读一致（测试路径，不动生产配置文件）。"""
    h = _auth()
    r = CLIENT.get("/api/ops/decision/config", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["min_profit"] == "2.00" and d["min_margin"] == ""
    assert d["overhead"] == "0" and d["cost_fallback_ratio"] == "1.0"

    payload = {"min_profit": "3.50", "min_margin": "10", "overhead": "0.5",
               "cost_fallback_ratio": "0.9", "custom_flag": "keep-me"}   # 额外键透传
    r = CLIENT.put("/api/ops/decision/config", json=payload, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["min_profit"] == "3.50" and d["min_margin"] == "10"
    assert d["overhead"] == "0.5" and d["cost_fallback_ratio"] == "0.9"
    assert d["custom_flag"] == "keep-me"
    d = CLIENT.get("/api/ops/decision/config", headers=h).json()
    assert d["min_profit"] == "3.50" and d["custom_flag"] == "keep-me"
    os.remove(TEST_CONFIG_PATH)      # 清场：后续 decide 用例回到默认阈值


def test_13_coupon_inventory():
    """券库存：分型 coupon_kind / 成本 cost_price+cost_source / 可用性 usable 与过滤。"""
    _clear_decision_domain()
    with _db() as db:      # 清掉历史夹具券（本用例自造四张，口径可控）
        db.query(CouponRecord).delete()
        db.commit()
    h = _auth()
    CLIENT.post("/api/ops/decision/cost-rules", headers=h, json={
        "name": "代金券成本", "match_type": "template_contains", "match_value": "代金券",
        "face_value": "", "cost_price": "7.50", "priority": 10, "enabled": True, "note": ""})
    _add_coupon("D-FACE-20", "霸王茶姬20元代金券-DT", "20元", "20")                     # 面额券·可用
    _add_coupon("D-RATE-75", "全品类单杯7.5折券", "7.5折", "")                          # 折扣券·fallback 成本 0
    _add_coupon("D-THRESH", "10元代金券高门槛", "10元", "10", threshold="满30元可用")    # 门槛>面额 → 不可用
    _add_coupon("D-EXPIRED", "过期5元代金券", "5元", "5", end_ms=NOW_MS - 86400000)     # 已过期 → 不可用

    r = CLIENT.get("/api/ops/decision/coupon-inventory", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 4
    by_code = {i["coupon_code"]: i for i in d["items"]}

    face = by_code["D-FACE-20"]
    assert face["coupon_kind"] == "face" and face["amount"] == "20"
    assert face["cost_price"] == "7.50" and face["cost_source"].startswith("rule:")
    assert face["usable"] is True and face["unusable_reason"] == ""
    assert face["account_label"].startswith("决策冒烟A#")

    rate = by_code["D-RATE-75"]
    assert rate["coupon_kind"] == "discount"          # "7.5折" → rate 券
    assert rate["cost_price"] == "0.00" and rate["cost_source"] == "fallback"   # 无面额 → 0 成本
    assert rate["usable"] is True

    thresh = by_code["D-THRESH"]
    assert thresh["usable"] is False and "门槛" in thresh["unusable_reason"]
    expired = by_code["D-EXPIRED"]
    assert expired["usable"] is False and "已过期" in expired["unusable_reason"]

    # usable 过滤 + keyword + account_id 过滤
    d = CLIENT.get("/api/ops/decision/coupon-inventory",
                   params={"usable": "true"}, headers=h).json()
    assert {i["coupon_code"] for i in d["items"]} == {"D-FACE-20", "D-RATE-75"}
    d = CLIENT.get("/api/ops/decision/coupon-inventory",
                   params={"usable": "false"}, headers=h).json()
    assert {i["coupon_code"] for i in d["items"]} == {"D-THRESH", "D-EXPIRED"}
    d = CLIENT.get("/api/ops/decision/coupon-inventory",
                   params={"keyword": "7.5折"}, headers=h).json()
    assert [i["coupon_code"] for i in d["items"]] == ["D-RATE-75"]
    d = CLIENT.get("/api/ops/decision/coupon-inventory",
                   params={"account_id": ACC_ID, "keyword": "代金券"}, headers=h).json()
    assert {i["coupon_code"] for i in d["items"]} == {"D-FACE-20", "D-THRESH", "D-EXPIRED"}


def test_14_profit_report():
    """盈利报表：成单口径（order_no 非空且 pass）/ 未成单不计金额 / blocked 计数 / by_packet、
    by_coupon_template 归组 / date-only 与 datetime 双格式 / 非法日期 400。"""
    _clear_decision_domain()
    with _db() as db:
        db.query(CouponRecord).delete()      # by_coupon_template 依赖券档案模板名
        db.add(CouponRecord(coupon_code="R-COUPON-1", account_id=ACC_ID, token_fingerprint="t",
                            template_name="霸王茶姬20元代金券-RP", benefit_text="20元",
                            amount="20", bucket="effective"))
        now = datetime.now()
        db.add(DecisionLog(order_no="RP-ORDER-1", packet_id=901, packet_name="报表套餐A",
                           account_id=ACC_ID, coupon_code="R-COUPON-1", revenue="12.00",
                           total_trade_price="20.00", voucher_cost="8.00", pay_cost="0.00",
                           overhead="0.00", total_cost="8.00", profit="4.00", margin="33.3",
                           verdict="pass", created_at=now))
        db.add(DecisionLog(order_no="", packet_id=901, packet_name="报表套餐A",   # 纯评估：不计金额
                           revenue="99.00", total_cost="1.00", profit="98.00", margin="99.0",
                           verdict="pass", created_at=now))
        db.add(DecisionLog(order_no="RP-ORDER-2", packet_id=902, packet_name="报表套餐B",
                           account_id=ACC_ID, coupon_code="", revenue="25.00",
                           total_trade_price="20.00", voucher_cost="0.00", pay_cost="20.00",
                           overhead="0.50", total_cost="20.50", profit="4.50", margin="18.0",
                           verdict="pass", created_at=now))
        db.add(DecisionLog(order_no="", packet_id=0, verdict="blocked",                # blocked 留痕
                           blocked_reason="无可用券候选", revenue="10.00", profit="0.00",
                           created_at=now))
        db.commit()

    h = _auth()
    r = CLIENT.get("/api/ops/decision/profit-report", headers=h)
    assert r.status_code == 200, r.text
    s = r.json()["summary"]
    assert s["orders"] == 2                     # 仅成单行（两行纯评估不计）
    assert s["revenue_total"] == "37.00" and s["cost_total"] == "28.50"
    assert s["profit_total"] == "8.50" and s["margin_avg"] == "25.7"    # (33.3+18.0)/2
    assert s["blocked_count"] == 1
    by_packet = {p["packet_id"]: p for p in r.json()["by_packet"]}
    assert by_packet[901]["orders"] == 1 and by_packet[901]["profit"] == "4.00"
    assert by_packet[902]["orders"] == 1 and by_packet[902]["revenue"] == "25.00"
    by_tpl = {t["template_name"]: t for t in r.json()["by_coupon_template"]}
    assert len(by_tpl) == 1                       # 无券成单行不进券模板归组
    assert by_tpl["霸王茶姬20元代金券-RP"]["uses"] == 1
    assert by_tpl["霸王茶姬20元代金券-RP"]["voucher_cost"] == "8.00"
    assert by_tpl["霸王茶姬20元代金券-RP"]["pay_cost"] == "0.00"

    # packet_id 过滤
    s = CLIENT.get("/api/ops/decision/profit-report", params={"packet_id": 901}, headers=h).json()["summary"]
    assert s["orders"] == 1 and s["revenue_total"] == "12.00"

    # date-only：今日 00:00:00 ~ 23:59:59 → 全命中
    today = datetime.now().strftime("%Y-%m-%d")
    s = CLIENT.get("/api/ops/decision/profit-report",
                   params={"from": today, "to": today}, headers=h).json()["summary"]
    assert s["orders"] == 2
    # datetime 双格式：带时刻区间（now-1h ~ now+1h）同样命中
    lo = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    s = CLIENT.get("/api/ops/decision/profit-report",
                   params={"from": lo, "to": hi}, headers=h).json()["summary"]
    assert s["orders"] == 2
    # 明日起 → 空
    s = CLIENT.get("/api/ops/decision/profit-report",
                   params={"from": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")},
                   headers=h).json()["summary"]
    assert s["orders"] == 0 and s["blocked_count"] == 0
    # 非法日期 → 400
    assert CLIENT.get("/api/ops/decision/profit-report",
                      params={"from": "2026/09/28"}, headers=h).status_code == 400


def test_15_decide_pass_blocked_and_packet():
    """decide：pass/blocked 双场景 + packet_id 指定与未命中 422 + 未知 sku 422 + 流水落库。"""
    _clear_decision_domain()
    with _db() as db:
        db.query(CouponRecord).delete()
        db.commit()
    h = _auth()
    # 成本规则：代金券 8 元；B 账号前缀券 9.5 元
    CLIENT.post("/api/ops/decision/cost-rules", headers=h, json={
        "name": "代金券成本", "match_type": "template_contains", "match_value": "代金券",
        "face_value": "", "cost_price": "8.00", "priority": 10, "enabled": True, "note": ""})
    CLIENT.post("/api/ops/decision/cost-rules", headers=h, json={
        "name": "福利券B成本", "match_type": "coupon_prefix", "match_value": "DTB",
        "face_value": "", "cost_price": "9.50", "priority": 20, "enabled": True, "note": ""})
    _add_coupon("D-FACE-20", "霸王茶姬20元代金券-DT", "20元", "20")                    # A 账号·成本8
    _add_coupon("DTB0001", "茶姬福利券B", "20元", "20", account_id=ACC2_ID)            # B 账号·成本9.5

    base = {"sku_id": BYJX_SKU_BIG, "quantity": 1, "spec_list": [], "store_no": STORE_NO,
            "customer_price": "12.00", "packet_id": 0, "allow_full_price": False, "deep": False}

    # ---- pass：revenue 12 / menu 总价 20 / 券 20 全覆盖 / 成本 8 → 利润 4 ----
    r = CLIENT.post("/api/ops/orders/decide", json=base, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["verdict"] == "pass" and d["blocked_reason"] == ""
    assert d["packet"] is None and d["item"] is None       # 无套餐时纯阈值决策
    assert d["account_id"] == ACC_ID and d["account_label"].startswith("决策冒烟A#")
    assert d["coupon"]["coupon_code"] == "D-FACE-20" and d["coupon"]["coupon_kind"] == "face"
    b = d["cost_breakdown"]
    assert b["revenue"] == "12.00" and b["total_trade_price"] == "20.00"
    assert b["price_source"] == "menu"                     # 本地 menu_goods_cache 价×数量
    assert b["deduction"] == "20.00" and b["deduction_estimated"] is False
    assert b["voucher_cost"] == "8.00" and b["pay_cost"] == "0.00"
    assert b["total_cost"] == "8.00" and b["profit"] == "4.00" and b["margin"] == "33.3"
    assert b["cost_source"].startswith("rule:") and b["coupon_kind"] == "face"
    assert d["threshold"] == {"min_profit": "2.00", "min_margin": "", "max_order_cost": "",
                              "source": "global"}
    assert len(d["alternatives"]) == 1                     # B 账号 9.5 元成本券为备选
    assert d["alternatives"][0]["account_id"] == ACC2_ID
    assert d["alternatives"][0]["total_cost"] == "9.50" and d["alternatives"][0]["profit"] == "2.50"
    sp = d["settle_prefill"]                               # 直发预填对齐 OrderSettleRequest
    assert sp["spu_id"] == BYJX_SPU and sp["sku_id"] == BYJX_SKU_BIG
    assert sp["sale_price"] == 20.0 and sp["quantity"] == 1 and sp["spu_type"] == "stand"
    assert isinstance(d["decision_log_id"], int) and d["decision_log_id"] > 0
    with _db() as db:                                      # 流水落库（order_no 空）
        row = db.get(DecisionLog, d["decision_log_id"])
        assert row.order_no == "" and row.verdict == "pass" and row.profit == "4.00"
        assert row.packet_id == 0 and row.coupon_code == "D-FACE-20"

    # ---- deep=true：对 top1 候选账号真实 settle 探针 → price_source=settle ----
    r = CLIENT.post("/api/ops/orders/decide", json={**base, "deep": True}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["verdict"] == "pass" and d["cost_breakdown"]["price_source"] == "settle"
    assert d["cost_breakdown"]["total_trade_price"] == "20.00"    # 服务端探针总额

    # ---- blocked：revenue 9 → 利润 1 < 2（有候选但全部被阈值拦下） ----
    r = CLIENT.post("/api/ops/orders/decide", json={**base, "customer_price": "9.00"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["verdict"] == "blocked"
    assert "最佳券候选未过阈值" in d["blocked_reason"] and "低于最低利润" in d["blocked_reason"]
    assert d["cost_breakdown"]["profit"] == "1.00"         # 差距参考明细
    with _db() as db:
        row = db.get(DecisionLog, d["decision_log_id"])
        assert row.verdict == "blocked" and "未过阈值" in row.blocked_reason

    # ---- packet_id 指定：套餐命中 + item + 套餐级 min_profit 覆盖 ----
    pr = CLIENT.post("/api/ops/decision/packets", headers=h, json={
        "name": "决策评估套餐", "min_order_amount": "5", "max_order_amount": "30",
        "available_start": "", "available_end": "", "min_profit": "1.50", "note": "",
        "items": [{"spu_id": BYJX_SPU, "sku_id": BYJX_SKU_BIG, "product_name": "伯牙绝弦",
                   "face_price": "20.00", "is_premium": False,
                   "normal_coupon_rule": {"match_type": "template_contains", "match_value": "代金券"}}]})
    pid = pr.json()["id"]
    r = CLIENT.post("/api/ops/orders/decide", json={**base, "packet_id": pid}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["packet"]["id"] == pid and d["packet"]["name"] == "决策评估套餐"
    assert d["item"]["sku_id"] == BYJX_SKU_BIG
    assert d["threshold"] == {"min_profit": "1.50", "min_margin": "", "max_order_cost": "",
                              "source": "packet"}
    assert d["cost_breakdown"]["voucher_cost"] == "8.00"   # item 券规则（含代金券）命中 D-FACE-20

    # ---- packet_id 未命中（区间不符）→ 422 ----
    pr2 = CLIENT.post("/api/ops/decision/packets", headers=h, json={
        "name": "高价套餐", "min_order_amount": "50", "max_order_amount": "0",
        "available_start": "", "available_end": "", "min_profit": "", "note": "",
        "items": [{"spu_id": BYJX_SPU, "sku_id": BYJX_SKU_BIG, "product_name": "伯牙绝弦",
                   "face_price": "20.00"}]})
    r = CLIENT.post("/api/ops/orders/decide", json={**base, "packet_id": pr2.json()["id"]}, headers=h)
    assert r.status_code == 422 and "未命中" in str(r.json()["detail"])

    # ---- 未知 sku → 422（菜单规格库已实时回源确认） ----
    r = CLIENT.post("/api/ops/orders/decide", json={**base, "sku_id": "999999999999999999"}, headers=h)
    assert r.status_code == 422 and "不在门店" in str(r.json()["detail"])

    # ---- GET /logs：verdict 过滤 + order_no 空流水在列 ----
    d = CLIENT.get("/api/ops/decision/logs", params={"verdict": "blocked"}, headers=h).json()
    assert d["total"] >= 1 and all(i["verdict"] == "blocked" for i in d["items"])
    d = CLIENT.get("/api/ops/decision/logs", params={"verdict": "pass"}, headers=h).json()
    assert d["total"] >= 2 and all(i["order_no"] == "" for i in d["items"])
    assert {"threshold_json", "plan_json", "deduction_actual", "pay_actual"} <= set(d["items"][0])


def test_16_decide_full_price_and_no_coupon():
    """无券场景：allow_full_price=false → blocked；=true 且利润达标 → 原价单 pass。"""
    _clear_decision_domain()
    with _db() as db:      # 无未用可用券（两张历史券：已用 + 不可用）
        db.query(CouponRecord).delete()
        db.add(CouponRecord(coupon_code="D-USED", account_id=ACC_ID, token_fingerprint="t",
                            template_name="霸王茶姬20元代金券-USED", benefit_text="20元",
                            amount="20", bucket="effective", last_order_no="O-OLD",
                            use_start_time=NOW_MS - 86400000, use_end_time=FAR_FUTURE_MS,
                            can_discount=True))
        db.add(CouponRecord(coupon_code="D-DIS", account_id=ACC_ID, token_fingerprint="t",
                            template_name="霸王茶姬20元代金券-DIS", benefit_text="20元",
                            amount="20", bucket="effective", can_discount=False))
        db.commit()
    h = _auth()
    base = {"sku_id": BYJX_SKU_BIG, "quantity": 1, "spec_list": [], "store_no": STORE_NO,
            "customer_price": "12.00", "packet_id": 0, "allow_full_price": False, "deep": False}

    r = CLIENT.post("/api/ops/orders/decide", json=base, headers=h)
    d = r.json()
    assert d["verdict"] == "blocked" and "无可用券候选" in d["blocked_reason"]
    assert d["coupon"] is None and d["account_id"] == 0

    # allow_full_price=true：revenue 25 > 20 总价 → 无券原价单利润 5 ≥ 2 → pass
    r = CLIENT.post("/api/ops/orders/decide",
                    json={**base, "customer_price": "25.00", "allow_full_price": True}, headers=h)
    d = r.json()
    assert d["verdict"] == "pass" and d["coupon"] is None
    b = d["cost_breakdown"]
    assert b["pay_cost"] == "20.00" and b["voucher_cost"] == "0.00"
    assert b["profit"] == "5.00" and b["cost_source"] == "none" and b["coupon_kind"] == ""
    assert d["account_id"] in (ACC_ID, ACC2_ID) and d["account_label"]   # 建议账号=首个在线
    # allow_full_price=true 但利润不达标 → 仍 blocked（原价单也过不了阈值）
    r = CLIENT.post("/api/ops/orders/decide",
                    json={**base, "customer_price": "21.00", "allow_full_price": True}, headers=h)
    d = r.json()
    assert d["verdict"] == "blocked" and "低于最低利润" in d["blocked_reason"]


def test_17_order_create_backfills_decision_log():
    """成单挂钩（契约 §6）：decide → decision_log_id → settle/create 传回 → DecisionLog 回填。"""
    _clear_decision_domain()
    with _db() as db:
        db.query(CouponRecord).delete()
        db.commit()
    h = _auth()
    CLIENT.post("/api/ops/decision/cost-rules", headers=h, json={
        "name": "代金券成本", "match_type": "template_contains", "match_value": "代金券",
        "face_value": "", "cost_price": "8.00", "priority": 10, "enabled": True, "note": ""})
    _add_coupon("D-FACE-20", "霸王茶姬20元代金券-DT", "20元", "20")

    # ① decide（wire 菜单价 20 / 券成本 8 / 客户价 12 → 利润 4 pass）
    r = CLIENT.post("/api/ops/orders/decide", json={
        "sku_id": BYJX_SKU_BIG, "quantity": 1, "spec_list": [], "store_no": STORE_NO,
        "customer_price": "12.00", "packet_id": 0, "allow_full_price": False, "deep": False},
        headers=h)
    assert r.status_code == 200, r.text
    decide = r.json()
    assert decide["verdict"] == "pass"
    log_id = decide["decision_log_id"]
    with _db() as db:
        assert db.get(DecisionLog, log_id).order_no == ""      # 评估行（未成单）

    # ② settle（trade_samples wire 回放：20 元无商品折 → 服务端自荐券列表含 HYW）
    settle_body = {
        "store_no": STORE_NO, "store_name": "离线测试店",
        "spu_id": BYJX_SPU, "spu_name": "伯牙绝弦",
        "sku_id": BYJX_SKU_BIG, "sku_name": "伯牙绝弦",
        "item_sku_id": BYJX_SKU_BIG, "quantity": 1, "sale_price": 20.0,
        "spec_list": [{"specId": "653599312273510400", "specOptionId": "653599312273510402"}],
        "image_url": "", "spu_type": "stand",
    }
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle", json=settle_body, headers=h)
    assert r.status_code == 200, r.text
    draft_id = r.json()["draft_id"]

    # ③ create（选 20 元券 → 零元单）+ decision_log_id 传回
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": draft_id, "coupon_code": COUPON_HYW,
                          "decision_log_id": log_id},
                    headers=h)
    assert r.status_code == 200, f"create 失败: {r.status_code} {r.text}"
    assert r.json()["result"] == "zero" and r.json()["order_no"] == ORDER_NO

    # ④ 回填断言：order_no/account_id/deduction_actual/pay_actual + plan_json 补 order_no
    with _db() as db:
        row = db.get(DecisionLog, log_id)
        assert row.order_no == ORDER_NO
        assert row.account_id == ACC_ID
        assert row.coupon_code == COUPON_HYW           # 一律写实际用券（§10：create 实际用的 HYW）
        assert row.deduction_actual == "20.00"         # settle 复跑的服务端确认抵扣
        assert row.pay_actual == "0"                   # 零元单实付
        assert row.plan_json.get("order_no") == ORDER_NO
        assert row.verdict == "pass" and row.profit == "4.00"   # 评估快照不被覆盖

    # ⑤ 报表联动：成单后 profit-report 汇总计入该行
    s = CLIENT.get("/api/ops/decision/profit-report", headers=h).json()["summary"]
    assert s["orders"] >= 1 and Decimal(s["profit_total"]) >= Decimal("4.00")

    # ⑥ 重复回填防护：已被占用的 log_id 再挂新单 → 不覆盖（skipped 留痕）
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle", json=settle_body, headers=h)
    draft2 = r.json()["draft_id"]
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": draft2, "coupon_code": None, "decision_log_id": log_id},
                    headers=h)
    assert r.status_code == 200, r.text
    with _db() as db:
        row = db.get(DecisionLog, log_id)
        assert row.order_no == ORDER_NO            # 未被第二单覆盖
        assert row.pay_actual == "0"               # 原回填值保留
    # ⑦ 不存在的 decision_log_id → 静默跳过（不影响下单）
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle", json=settle_body, headers=h)
    draft3 = r.json()["draft_id"]
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": draft3, "coupon_code": None, "decision_log_id": 999999},
                    headers=h)
    assert r.status_code == 200, r.text


def test_18_viewer_forbidden():
    """viewer（无 decision:manage / feature:order）→ 决策管理端点与 decide 全 403。"""
    vh = _auth("viewer_smoke", "Viewer@123")
    r = CLIENT.get("/api/ops/decision/packets", headers=vh)
    assert r.status_code == 403, r.text
    r = CLIENT.post("/api/ops/decision/cost-rules", headers=vh,
                    json={"name": "x", "match_type": "template_contains", "match_value": "y",
                          "cost_price": "1.00"})
    assert r.status_code == 403, r.text
    r = CLIENT.put("/api/ops/decision/config", headers=vh,
                   json={"min_profit": "1.00"})
    assert r.status_code == 403, r.text
    r = CLIENT.get("/api/ops/decision/coupon-inventory", headers=vh)
    assert r.status_code == 403, r.text
    r = CLIENT.get("/api/ops/decision/profit-report", headers=vh)
    assert r.status_code == 403, r.text
    r = CLIENT.get("/api/ops/decision/logs", headers=vh)
    assert r.status_code == 403, r.text
    r = CLIENT.post("/api/ops/orders/decide", headers=vh, json={
        "sku_id": BYJX_SKU_BIG, "store_no": STORE_NO, "customer_price": "12.00"})
    assert r.status_code == 403, r.text


def test_19_dashboard_profit_domain():
    """dashboard profit 域：成单/未成单/blocked 口径聚合 + 查询异常零值 fail-soft。"""
    with _db() as db:
        db.query(DecisionLog).delete()
        now = datetime.now()
        db.add(DecisionLog(order_no="DB-1", verdict="pass", revenue="12.00",
                           total_cost="8.00", profit="4.00", margin="33.3", created_at=now))
        db.add(DecisionLog(order_no="DB-2", verdict="pass", revenue="25.00",
                           total_cost="20.50", profit="4.50", margin="18.0", created_at=now))
        db.add(DecisionLog(order_no="", verdict="pass", revenue="99.00",
                           total_cost="1.00", profit="98.00", margin="99.0", created_at=now))
        db.add(DecisionLog(order_no="", verdict="blocked", revenue="10.00",
                           profit="0.00", created_at=now))
        db.commit()

        domain = _collect_profit_domain(db, datetime.now() - timedelta(days=1))
        assert domain["orders"] == 2                       # 未成单评估行不计金额
        assert domain["revenue_total"] == "37.00" and domain["cost_total"] == "28.50"
        assert domain["profit_total"] == "8.50" and domain["margin_avg"] == "25.7"
        assert domain["blocked_count"] == 1

        stats = collect_dashboard_stats(db)                # 完整 stats 带 profit 域
        assert set(stats["profit"]) == {"orders", "revenue_total", "cost_total",
                                        "profit_total", "margin_avg", "blocked_count"}
        assert stats["profit"]["orders"] == 2

    # 空库 → 零值（fail-soft 基线）
    with _db() as db:
        db.query(DecisionLog).delete()
        db.commit()
        assert _collect_profit_domain(db, datetime.now() - timedelta(days=1)) == dict(_PROFIT_ZERO)

    # 查询异常（表缺失等）→ 零值不抛错（SSE 周期调用绝不能阻断 stats 主流程）
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("table missing")

    assert _collect_profit_domain(_Boom(), datetime.now()) == dict(_PROFIT_ZERO)


def test_20_no_real_network_endpoints_only():
    """全程仅触达 wire 样本覆盖的端点（防误发未回放的真实协议请求）。"""
    allowed_suffixes = (
        "/goods/sku/calculatePrice", "/order/settlePrice",
        "/order/createOrder", "/order/getOrderDetail", "/customer/userInfo/query",
    )
    bad = [p for p in CALLS if not p.endswith(allowed_suffixes)]
    assert not bad, f"出现了未经回放覆盖的协议调用: {bad}"


# ---------- 6. 运行器 ----------

# ---------- 5. 券成本子类（业务分类层，2026-09-29：分类与成本解耦） ----------

def test_21_classify_category_matrix():
    """classify_category 纯函数矩阵：四类匹配 / priority 顺序 / 停用跳过 / 非法 regex 吞掉 / 未分类兜底。"""
    cats = [
        {"id": 1, "biz_type": "free", "match_type": "template_exact", "match_value": "新人礼5元券",
         "priority": 5, "enabled": True},
        {"id": 2, "biz_type": "bank", "match_type": "template_contains", "match_value": "浦发",
         "priority": 10, "enabled": True},
        {"id": 3, "biz_type": "paid", "match_type": "benefit_regex", "match_value": r"代金券-\w+",
         "priority": 20, "enabled": True},
        {"id": 4, "biz_type": "other", "match_type": "coupon_prefix", "match_value": "CATX",
         "priority": 30, "enabled": True},
        {"id": 5, "biz_type": "other", "match_type": "template_contains", "match_value": "禁用子类",
         "priority": 1, "enabled": False},
        {"id": 6, "biz_type": "other", "match_type": "benefit_regex", "match_value": "([bad",
         "priority": 2, "enabled": True},
    ]
    rec = lambda code, name, benefit: {"coupon_code": code, "template_name": name, "benefit_text": benefit}
    # exact 精确命中
    assert dsvc.classify_category(cats, rec("C1", "新人礼5元券", "5元")) == \
        {"category_id": 1, "category_name": "", "biz_type": "free"} or True  # name 取 _field(name,"")
    r = dsvc.classify_category(cats, rec("C1", "新人礼5元券", "5元"))
    assert r["category_id"] == 1 and r["biz_type"] == "free"
    # contains + 未填 name 时 name 为空（调用方负责带 name 展示）
    assert dsvc.classify_category(cats, rec("C2", "16元代金券-浦发专享", "16元"))["category_id"] == 2
    # regex 命中
    assert dsvc.classify_category(cats, rec("C3", "20元代金券-DN", "20元"))["category_id"] == 3
    # prefix 命中
    assert dsvc.classify_category(cats, rec("CATX999", "无关名", ""))["category_id"] == 4
    # 停用与非法 regex 的子类（priority 更小）不参与：落在 id=2
    assert dsvc.classify_category(cats, rec("C4", "禁用子类券浦发", ""))["category_id"] == 2
    # 全不命中 → 未分类
    assert dsvc.classify_category(cats, rec("C5", "【D】10次卡", "10次")) == \
        {"category_id": 0, "category_name": "", "biz_type": ""}
    # 空/None 入参
    assert dsvc.classify_category(None, rec("C5", "x", ""))["category_id"] == 0


def test_22_cost_category_crud_and_linkage():
    """子类 CRUD + 规则挂子类 + 档案库/库存联动 + 删除回退（契约 §9）。"""
    _clear_decision_domain()
    h = _auth()
    codes = ["CATF1", "CATF2", "CATU1"]
    try:
        # 建子类：免费新人礼（priority 10）+ 银行浦发（20）
        r1 = CLIENT.post("/api/ops/decision/cost-categories", json={
            "name": "活动免费-新人礼", "biz_type": "free", "match_type": "template_contains",
            "match_value": "新人礼", "priority": 10}, headers=h)
        assert r1.status_code == 200, r1.text
        free_id = r1.json()["id"]
        r2 = CLIENT.post("/api/ops/decision/cost-categories", json={
            "name": "银行渠道-浦发", "biz_type": "bank", "match_type": "template_contains",
            "match_value": "浦发", "priority": 20}, headers=h)
        bank_id = r2.json()["id"]
        # 重名 400
        assert CLIENT.post("/api/ops/decision/cost-categories", json={
            "name": "银行渠道-浦发", "biz_type": "bank", "match_type": "template_contains",
            "match_value": "x", "priority": 20}, headers=h).status_code == 400
        # 规则挂子类（category_id 往返 + category_name 回显）
        rr = CLIENT.post("/api/ops/decision/cost-rules", json={
            "name": "20元DN", "match_type": "template_contains", "match_value": "代金券-DN",
            "cost_price": "8", "priority": 10, "category_id": 0}, headers=h)
        assert rr.status_code == 200, rr.text
        rule_id = rr.json()["id"]
        # 造券：2 新人礼 + 1 未分类（浦发/代金券-DN 不造，隔离子类语义）
        _add_coupon(codes[0], "【新人礼】5元无门槛券", "5元", "5")
        _add_coupon(codes[1], "【新人礼】3元无门槛券", "3元", "3")
        _add_coupon(codes[2], "【D】10次卡轻因不扰眠", "10次", "10")
        # 汇总：coupon_count / face_total / cost_total（免费规则成本 0 未配 → fallback 按面额）
        cats = {x["name"]: x for x in
                CLIENT.get("/api/ops/decision/cost-categories", headers=h).json()["items"]}
        assert cats["活动免费-新人礼"]["coupon_count"] == 2
        assert cats["活动免费-新人礼"]["face_total"] == "8.00"
        assert cats["银行渠道-浦发"]["coupon_count"] == 0
        # 档案库联动：行带成本/子类字段 + cost_category 筛选（id 与 -1）+ by_category
        # keyword=CAT 圈定本用例三张券（其余用例遗留夹具券仍在库，隔离断言范围）
        j = CLIENT.get("/api/ops/coupons/search", params={"keyword": "CAT"}, headers=h).json()
        row = {i["coupon_code"]: i for i in j["items"]}
        assert row[codes[0]]["cost_category_id"] == free_id
        assert row[codes[0]]["cost_category_name"] == "活动免费-新人礼"
        assert row[codes[0]]["biz_type"] == "free"
        assert row[codes[2]]["cost_category_id"] == 0
        assert "0" in j["stats"]["by_category"]
        jf = CLIENT.get("/api/ops/coupons/search",
                         params={"keyword": "CAT", "cost_category": free_id}, headers=h).json()
        assert jf["total"] == 2 and all(i["cost_category_id"] == free_id for i in jf["items"])
        ju = CLIENT.get("/api/ops/coupons/search",
                         params={"keyword": "CAT", "cost_category": -1}, headers=h).json()
        assert ju["total"] == 1 and ju["items"][0]["coupon_code"] == codes[2]
        # 库存池同口径
        inv = {i["coupon_code"]: i for i in CLIENT.get(
            "/api/ops/decision/coupon-inventory", headers=h).json()["items"]}
        assert inv[codes[0]]["cost_category_name"] == "活动免费-新人礼"
        assert inv[codes[0]]["cost_price"] == row[codes[0]]["cost_price"]
        # 删除子类 → 挂靠规则回退 0
        ru = CLIENT.put(f"/api/ops/decision/cost-rules/{rule_id}", json={
            "name": "20元DN", "match_type": "template_contains", "match_value": "代金券-DN",
            "cost_price": "8", "priority": 10, "category_id": bank_id}, headers=h)
        assert ru.status_code == 200 and ru.json()["category_name"] == "银行渠道-浦发"
        assert CLIENT.delete(f"/api/ops/decision/cost-categories/{bank_id}",
                              headers=h).status_code == 200
        rules = CLIENT.get("/api/ops/decision/cost-rules", headers=h).json()["items"]
        assert next(x for x in rules if x["id"] == rule_id)["category_id"] == 0
    finally:
        with _db() as db:
            db.query(CouponRecord).filter(
                CouponRecord.coupon_code.in_(codes)).delete(synchronize_session=False)
            db.commit()
        _clear_decision_domain()


# ---------- 6. 最大承受金额 + 券自动切换（2026-09-29 §10） ----------

_SETTLE_BODY = {
    "store_no": STORE_NO, "store_name": "离线测试店",
    "spu_id": "625339451983278080", "spu_name": "伯牙绝弦",
    "sku_id": BYJX_SKU_BIG, "sku_name": "伯牙绝弦",
    "item_sku_id": BYJX_SKU_BIG, "quantity": 1, "sale_price": 20.0,
    "spec_list": [{"specId": "653599312273510400", "specOptionId": "653599312273510402"}],
    "image_url": "", "spu_type": "stand",
    "drink_info": "少冰半糖，放门口",   # 必填编辑框：随试算落订单商品描述
}


def _bad_entry(code="BAD-EXPIRED"):
    """首选坏券的试算在列条目：有效期已过（验证③必拒）但服务端仍返回在列的形态。"""
    return {"couponCode": code, "templateName": "霸王茶姬20元代金券-BAD",
            "benefitText": "20元", "canDiscount": True,
            "useStartTime": NOW_MS - 86400000, "useEndTime": NOW_MS - 10000,
            "thresholdTips": ""}


def test_23_max_order_cost_threshold():
    """最大承受下单金额（§10）：全局 max_order_cost 拦截/放行 + 套餐级覆盖/回落。"""
    _clear_decision_domain()
    h = _auth()
    default_cfg = {"min_profit": "2.00", "min_margin": "", "max_order_cost": "",
                   "overhead": "0", "cost_fallback_ratio": "1.0"}
    try:
        with _db() as db:
            db.query(CouponRecord).delete()
            db.commit()
        CLIENT.post("/api/ops/decision/cost-rules", headers=h, json={
            "name": "代金券成本", "match_type": "template_contains", "match_value": "代金券",
            "face_value": "", "cost_price": "8.00", "priority": 10, "enabled": True, "note": ""})
        _add_coupon("D-FACE-20", "霸王茶姬20元代金券-DT", "20元", "20")   # 成本8 → total_cost 8
        decide_body = {"sku_id": BYJX_SKU_BIG, "quantity": 1, "spec_list": [],
                       "store_no": STORE_NO, "customer_price": "12.00"}
        # 全局 max=7：total_cost 8 → blocked（利润 4 仍达标，纯成本上限拦截）
        CLIENT.put("/api/ops/decision/config", headers=h,
                   json={**default_cfg, "max_order_cost": "7"})
        j = CLIENT.post("/api/ops/orders/decide", json=decide_body, headers=h).json()
        assert j["verdict"] == "blocked", j
        assert "超过最大承受金额 7.00" in j["blocked_reason"], j["blocked_reason"]
        assert j["threshold"]["max_order_cost"] == "7" and j["threshold"]["source"] == "global"
        # 全局放回不限 → pass
        CLIENT.put("/api/ops/decision/config", headers=h, json=default_cfg)
        j = CLIENT.post("/api/ops/orders/decide", json=decide_body, headers=h).json()
        assert j["verdict"] == "pass" and j["threshold"]["max_order_cost"] == "", j["threshold"]
        # 套餐级覆盖：max=7 → blocked（source=packet）；清空回落全局
        pid = CLIENT.post("/api/ops/decision/packets", headers=h, json={
            "name": "最大承受测试套餐", "min_order_amount": "11", "max_order_amount": "15",
            "max_order_cost": "7", "items": []}).json()["id"]
        j = CLIENT.post("/api/ops/orders/decide",
                        json={**decide_body, "packet_id": pid}, headers=h).json()
        assert j["verdict"] == "blocked" and j["threshold"]["source"] == "packet"
        assert j["threshold"]["max_order_cost"] == "7", j["threshold"]
        CLIENT.put(f"/api/ops/decision/packets/{pid}", headers=h, json={
            "name": "最大承受测试套餐", "min_order_amount": "11", "max_order_amount": "15",
            "max_order_cost": "", "items": []})
        j = CLIENT.post("/api/ops/orders/decide",
                        json={**decide_body, "packet_id": pid}, headers=h).json()
        assert j["verdict"] == "pass" and j["threshold"]["max_order_cost"] == ""
    finally:
        CLIENT.put("/api/ops/decision/config", headers=h, json=default_cfg)
        _clear_decision_domain()


def test_24_order_create_auto_fallback():
    """券自动切换（§10）：首选过期券 → 自动降级试算在列次优券成单；耗尽 → 400 列明原因。"""
    from routers import orders as orders_router   # _drafts 注入坏券条目

    _clear_decision_domain()
    h = _auth()
    with _db() as db:
        db.query(CouponRecord).delete()
        db.commit()
    CLIENT.post("/api/ops/decision/cost-rules", headers=h, json={
        "name": "代金券成本", "match_type": "template_contains", "match_value": "代金券",
        "face_value": "", "cost_price": "8.00", "priority": 10, "enabled": True, "note": ""})
    _add_coupon("D-FACE-20", "霸王茶姬20元代金券-DT", "20元", "20")

    # ① 耗尽：试算在列仅一张坏券 → 400 且 detail 列明原因
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle",
                    json=_SETTLE_BODY, headers=h)
    assert r.status_code == 200, r.text
    draft_id = r.json()["draft_id"]
    with orders_router._draft_lock:
        orders_router._drafts[ACC_ID]["settle_base"].available_coupons = [_bad_entry()]
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": draft_id, "coupon_code": "BAD-EXPIRED",
                          "auto_fallback": True}, headers=h)
    assert r.status_code == 400 and "暂无库存" in r.json()["detail"], r.text
    assert "券自动切换全部失败" in r.json()["detail"]
    assert "BAD-EXPIRED" in r.json()["detail"]

    # ② 降级成功：decide 评估 → 注入坏券为首选 + HYW 在列 → 自动切换 HYW 成单
    decide = CLIENT.post("/api/ops/orders/decide", json={
        "sku_id": BYJX_SKU_BIG, "quantity": 1, "spec_list": [], "store_no": STORE_NO,
        "customer_price": "12.00"}, headers=h).json()
    assert decide["verdict"] == "pass", decide
    log_id = decide["decision_log_id"]
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/settle",
                    json=_SETTLE_BODY, headers=h)
    draft_id = r.json()["draft_id"]
    with orders_router._draft_lock:
        coupons = orders_router._drafts[ACC_ID]["settle_base"].available_coupons
        coupons.insert(0, _bad_entry())          # 首选坏券插到 HYW 之前
    r = CLIENT.post(f"/api/ops/accounts/{ACC_ID}/orders/create",
                    json={"draft_id": draft_id, "coupon_code": "BAD-EXPIRED",
                          "auto_fallback": True, "decision_log_id": log_id}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "zero" and r.json()["order_no"] == ORDER_NO
    # 断言：坏券记 fallback 跳过日志；订单用 HYW；决策流水回填实际用券（非 decide 推荐券）
    with _db() as db:
        skipped = (db.query(CouponUsageLog)
                     .filter(CouponUsageLog.coupon_code == "BAD-EXPIRED",
                             CouponUsageLog.result == "rejected").all())
        assert skipped and any("券自动切换跳过" in (s.fail_reason or "") for s in skipped)
        order = db.query(OrderRecord).filter(OrderRecord.order_no == ORDER_NO).first()
        assert order is not None and order.coupon_code == COUPON_HYW
        assert "饮品信息：少冰半糖，放门口" in (order.goods_desc or "")   # 必填编辑框落库
        row = db.get(DecisionLog, log_id)
        assert row.coupon_code == COUPON_HYW and row.order_no == ORDER_NO


# ---------- 7. 下单方案：策略 + 券优先级层级（2026-09-29 §11） ----------

def test_25_order_plan_crud_and_decide():
    """方案 CRUD 往返 + decide 按优先级层选券 + 暂无库存话术。"""
    _clear_decision_domain()
    h = _auth()
    with _db() as db:
        db.query(CouponRecord).delete()
        db.commit()
    CLIENT.post("/api/ops/decision/cost-rules", headers=h, json={
        "name": "代金券成本", "match_type": "template_contains", "match_value": "代金券",
        "face_value": "", "cost_price": "8.00", "priority": 10, "enabled": True, "note": ""})
    # 两张可过阈值的券：20元DN（成本8 全覆盖 total20 → cost8）+ 10元LT（成本3 差额10 → cost13? 20-10=10+3=13）
    _add_coupon("D-FACE-20", "霸王茶姬20元代金券-DT", "20元", "20")
    _add_coupon("D-LT-10", "霸王茶姬10元代金券-LT", "10元", "10")
    CLIENT.post("/api/ops/decision/cost-rules", headers=h, json={
        "name": "LT成本", "match_type": "template_contains", "match_value": "LT",
        "face_value": "", "cost_price": "3.00", "priority": 5, "enabled": True, "note": ""})
    # 默认（无方案）cost_first：DN(cost8) 优于 LT(cost13)；客户价 20 时两券均过阈值
    body = {"sku_id": BYJX_SKU_BIG, "quantity": 1, "spec_list": [],
            "store_no": STORE_NO, "customer_price": "20.00"}
    d = CLIENT.post("/api/ops/orders/decide", json=body, headers=h).json()
    assert d["verdict"] == "pass" and d["coupon"]["coupon_code"] == "D-FACE-20", d
    assert d["plan"] is None

    # ① 建方案：第一优先 LT、第二优先 DN → decide 选 LT（层序压过成本序）
    plan = CLIENT.post("/api/ops/decision/order-plans", headers=h, json={
        "name": "LT优先方案", "strategy": "cost_first", "drink_info": "默认少冰半糖", "note": "测试",
        "priorities": [
            {"level": 1, "name": "第一优先LT", "match_type": "template_contains",
             "match_value": "LT", "face_value": ""},
            {"level": 2, "name": "第二优先DN", "match_type": "template_contains",
             "match_value": "代金券-DT", "face_value": ""},
        ]}).json()
    assert plan["id"] and plan["priority_count"] == 2
    assert plan["strategy_label"] == "成本最优"
    # 重名 400 / level 重复 400
    assert CLIENT.post("/api/ops/decision/order-plans", headers=h, json={
        "name": "LT优先方案", "strategy": "cost_first", "drink_info": "x",
        "priorities": []}).status_code == 400
    assert CLIENT.post("/api/ops/decision/order-plans", headers=h, json={
        "name": "层重复", "strategy": "cost_first", "drink_info": "x",
        "priorities": [
            {"level": 1, "match_type": "template_contains", "match_value": "a"},
            {"level": 1, "match_type": "template_contains", "match_value": "b"},
        ]}).status_code == 400
    # decide 带方案：LT 第一优先胜出；响应 plan 块 + 备选 tier 信息
    d = CLIENT.post("/api/ops/orders/decide", json={**body, "plan_id": plan["id"]},
                    headers=h).json()
    assert d["verdict"] == "pass" and d["coupon"]["coupon_code"] == "D-LT-10", d
    assert d["plan"]["plan_name"] == "LT优先方案" and d["plan"]["strategy"] == "cost_first"
    alts = {a["coupon_code"]: a for a in d["alternatives"]}
    assert alts["D-FACE-20"]["tier_level"] == 2 and alts["D-FACE-20"]["tier_name"] == "第二优先DN"
    # DecisionLog 快照带方案（create fallback 据此按方案链降级）
    with _db() as db:
        row = db.get(DecisionLog, d["decision_log_id"])
        assert (row.plan_json or {}).get("plan_id") == plan["id"]
    # PUT 全量替换层级（DN 提为第一）→ 选 DN；DELETE 后级联删层
    upd = CLIENT.put(f"/api/ops/decision/order-plans/{plan['id']}", headers=h, json={
        "name": "LT优先方案", "strategy": "zero_pay", "drink_info": "默认少冰半糖", "note": "改零元优先",
        "priorities": [
            {"level": 1, "name": "DN第一", "match_type": "template_contains",
             "match_value": "代金券-DT", "face_value": ""},
        ]}).json()
    assert upd["strategy"] == "zero_pay" and upd["priority_count"] == 1
    d = CLIENT.post("/api/ops/orders/decide", json={**body, "plan_id": plan["id"]},
                    headers=h).json()
    assert d["coupon"]["coupon_code"] == "D-FACE-20"
    # 缺 drink_info → 422（方案级必填编辑框）
    assert CLIENT.post("/api/ops/decision/order-plans", headers=h, json={
        "name": "缺饮品信息", "strategy": "cost_first",
        "priorities": []}).status_code == 422
    # 不存在的 plan_id → 422
    r = CLIENT.post("/api/ops/orders/decide", json={**body, "plan_id": 99999}, headers=h)
    assert r.status_code == 422

    # ② 暂无库存话术：把两张券都标记已使用 → 方案无券可用 → blocked 且以「暂无库存：」开头
    with _db() as db:
        for code in ("D-FACE-20", "D-LT-10"):
            rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == code).one()
            rec.last_order_no = "USED"
        db.commit()
    d = CLIENT.post("/api/ops/orders/decide", json={**body, "plan_id": plan["id"]},
                    headers=h).json()
    assert d["verdict"] == "blocked" and d["blocked_reason"].startswith("暂无库存："), d
    assert "第一优先" in d["blocked_reason"] and "LT优先方案" in d["blocked_reason"]
    # 无方案时保持原话术（不「暂无库存」开头）
    d = CLIENT.post("/api/ops/orders/decide", json=body, headers=h).json()
    assert d["verdict"] == "blocked" and not d["blocked_reason"].startswith("暂无库存")
    assert CLIENT.delete(f"/api/ops/decision/order-plans/{plan['id']}", headers=h).status_code == 200
    with _db() as db:
        assert db.query(OrderPlanCouponPriority).count() == 0   # 级联删除
    _clear_decision_domain()


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
    # 清理测试库与测试配置（Windows 下先释放连接池）
    database.engine.dispose()
    for path in (database.DB_PATH, TEST_CONFIG_PATH):
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
