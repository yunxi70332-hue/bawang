"""优惠券全量查询 / 多维模糊搜索 / 仪表盘统计联动 —— 离线测试（零真实网络请求）。

运行：
    cd E:\\霸王茶姬\\account_system\\server && python -m pytest test_coupons_offline.py -q

覆盖：
  - POST /api/ops/coupons/sync-all：多账号 token 遍历、券ID 全局去重收集、券档案落库（含使用范围）、
    单账号凭证失效不中断遍历并回写 expired 状态、disabled/无 token 账号被跳过
  - GET  /api/ops/coupons/search：券码/名称/使用范围等多维 LIKE 模糊匹配 + bucket 精确过滤 + 命中统计
  - GET  /api/dashboard/stats：券域/订单域卡片、近 7 日趋势与使用范围分布（仪表盘联动数据源）
"""

import os
import sys
from types import SimpleNamespace

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
# 日志隔离：oplog 日志库与文本日志均指向测试路径，避免污染生产 data/logs/
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_coupons_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_coupons.db")
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
from models import ChageeAccount, CouponRecord  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402

with TestClient(app_module.app) as client:   # startup → init_db（含 usable_scenes 列迁移路径）
    pass

# ---------- 2. 假协议客户端（回放 effective/historical 列表） ----------

# 账号1：2 张可用 + 1 张历史；账号3：1 张可用（含使用范围）；账号4：凭证失效
EFFECTIVE = {
    1: [
        {"couponCode": "C1001", "templateName": "霸王茶姬20元代金券-DN", "bizType": 1,
         "benefitText": "20元", "benefit2Text": "代金", "status": 10, "amount": "20",
         "useStartTime": 1790092800000, "useEndTime": 1792684799000, "usableScenes": [2, 6]},
        {"couponCode": "C1002", "templateName": "免10杯多次卡", "bizType": 1,
         "benefitText": "免10杯", "benefit2Text": "", "status": 10,
         "useStartTime": 1790092800000, "useEndTime": 1792684799000, "usableScenes": "自取"},
    ],
    3: [
        {"couponCode": "C3001", "templateName": "外卖专属5元代金券", "bizType": 1,
         "benefitText": "5元", "benefit2Text": "代金", "status": 10,
         "useStartTime": 1790092800000, "useEndTime": 1792684799000, "usableScenes": [6]},
    ],
}
HISTORICAL = {
    1: [
        {"couponCode": "C1003", "templateName": "已使用10元代金券", "bizType": 1,
         "benefitText": "10元", "benefit2Text": "代金", "status": 10,
         "useStartTime": 1790092800000, "useEndTime": 1792684799000, "usableScenes": [62]},
    ],
    3: [],
}
EXPIRED_ACCOUNTS = {4}


class FakeCouponClient:
    def __init__(self, account):
        self.account = account
        self.token = "tok-" + str(account.id)
        self.proto = SimpleNamespace(sk="sk", token=self.token)

    def post(self, path, body=None, **kw):
        if self.account.id in EXPIRED_ACCOUNTS:
            raise bridge.SessionExpiredError("12320120400401", "token 已失效")
        if path.endswith("/user-coupon/effective-list"):
            items = EFFECTIVE.get(self.account.id, [])
            return {"errcode": "0", "data": {"pageList": items, "total": len(items)}}
        if path.endswith("/user-coupon/historical-list"):
            items = HISTORICAL.get(self.account.id, [])
            return {"errcode": "0", "data": {"pageList": items, "total": len(items)}}
        raise AssertionError(f"未预期的 POST 协议调用: {path}")


bridge.build_client = lambda account: FakeCouponClient(account)   # monkeypatch

# ---------- 3. 测试数据与夹具 ----------

db = database.SessionLocal()
seed.init_db()
from models import Role, SystemUser  # noqa: E402

admin_role = db.query(Role).filter(Role.name == "admin").one()
if not db.query(SystemUser).filter(SystemUser.username == "op").first():
    db.add(SystemUser(username="op", password_hash=hash_password("Op@123"),
                      display_name="运营", role_id=admin_role.id))
    db.commit()

# 账号1/3 在线有 token；账号2 无 token（应被跳过）；账号4 在线但服务端判定失效；账号5 停用（应被跳过）
for aid, status, token in ((1, "online", "t1"), (2, "pending", ""), (3, "online", "t3"),
                           (4, "online", "t4"), (5, "disabled", "t5")):
    if not db.get(ChageeAccount, aid):
        db.add(ChageeAccount(id=aid, label=f"测试账号{aid}", phone=f"1380000000{aid}",
                             device_uuid=f"uuid-{aid}", token=token, status=status))
db.commit()
db.close()

client = TestClient(app_module.app)
tok = client.post("/api/auth/login", json={"username": "op", "password": "Op@123"}).json()
H = {"Authorization": f"Bearer {tok['token']}"}


def fresh_db():
    return database.SessionLocal()


# ---------- 4. 用例 ----------


def test_sync_all_collects_distinct_coupon_ids():
    r = client.post("/api/ops/coupons/sync-all", headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    # 只遍历 有token且未停用 的账号：1/3/4（2 无 token、5 disabled 被跳过）
    assert d["scanned"] == 3
    assert d["ok"] == 2 and d["expired"] == 1 and d["failed"] == 0
    # 券ID 全局去重：账号1 3 张 + 账号3 1 张（账号4 失效无券）
    assert d["distinct_coupon_ids"] == 4
    codes = {c["couponCode"] for c in d["coupons"]}
    assert codes == {"C1001", "C1002", "C1003", "C3001"}
    assert all("account_label" in c for c in d["coupons"])
    # 汇总与账号级结果
    assert d["summary"]["effective_total"] == 3
    assert d["summary"]["historical_total"] == 1
    assert d["summary"]["effective_usable_times"] == 12   # 20元券1 + 免10杯10 + 5元券1
    row4 = next(a for a in d["accounts"] if a["id"] == 4)
    assert row4["result"] == "expired"
    # 失效账号状态已回写 DB
    db = fresh_db()
    assert db.get(ChageeAccount, 4).status == "expired"
    # 券档案落库：使用范围归一化 + 归属映射
    rec = db.query(CouponRecord).filter(CouponRecord.coupon_code == "C1001").one()
    assert rec.usable_scenes == "自取/外卖"
    assert rec.account_id == 1
    assert rec.bucket == "effective" and rec.synced_from == "coupon_query"
    rec3 = db.query(CouponRecord).filter(CouponRecord.coupon_code == "C3001").one()
    assert rec3.usable_scenes == "外卖" and rec3.account_id == 3
    rec_h = db.query(CouponRecord).filter(CouponRecord.coupon_code == "C1003").one()
    assert rec_h.bucket == "historical" and rec_h.usable_scenes == "团餐自提"
    db.close()


def test_sync_all_no_available_accounts():
    db = fresh_db()
    for a in db.query(ChageeAccount).all():
        a.status = "disabled"
    db.commit()
    db.close()
    r = client.post("/api/ops/coupons/sync-all", headers=H)
    assert r.status_code == 400
    assert "没有可查询的账号" in r.json()["detail"]


def test_coupons_search_multi_dimension():
    # 名称模糊
    r = client.get("/api/ops/coupons/search", headers=H, params={"keyword": "代金券"})
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 3   # C1001/C1003/C3001 名称含「代金券」（C1002 多次卡不含）
    # 券码精确模糊（前缀 LIKE）
    d = client.get("/api/ops/coupons/search", headers=H, params={"keyword": "C300"}).json()
    assert [i["coupon_code"] for i in d["items"]] == ["C3001"]
    assert d["items"][0]["account_label"] == "测试账号3#3"
    # 使用范围维度：外卖 → C1001(自取/外卖) + C3001(外卖)
    d = client.get("/api/ops/coupons/search", headers=H, params={"keyword": "外卖"}).json()
    assert {i["coupon_code"] for i in d["items"]} == {"C1001", "C3001"}
    # scene 过滤叠加
    d = client.get("/api/ops/coupons/search", headers=H, params={"scene": "团餐"}).json()
    assert {i["coupon_code"] for i in d["items"]} == {"C1003"}
    # bucket 过滤 + 命中统计
    d = client.get("/api/ops/coupons/search", headers=H, params={"bucket": "historical"}).json()
    assert d["total"] == 1 and d["stats"]["historical"] == 1
    # 归属账号名维度
    d = client.get("/api/ops/coupons/search", headers=H, params={"keyword": "测试账号1"}).json()
    assert {i["coupon_code"] for i in d["items"]} == {"C1001", "C1002", "C1003"}


def test_dashboard_stats_coupon_and_order_domain():
    db = fresh_db()
    from models import CouponUsageLog, OrderRecord
    db.add(CouponUsageLog(coupon_code="C1001", coupon_name="20元代金券", account_id=1,
                          account_label="测试账号1#1", operator="op", order_no="O1",
                          deduction="20", total_amount="20", pay_amount="0",
                          scenario="zero", result="success"))
    db.add(OrderRecord(account_id=1, order_no="O1", status=6, scenario="zero",
                       total_amount="20", pay_amount="0", status_label="已完成"))
    db.commit()
    db.close()

    r = client.get("/api/dashboard/stats", headers=H)
    assert r.status_code == 200
    d = r.json()
    cards = d["cards"]
    # 券域（与全量同步联动）
    assert cards["coupons_total"] == 4
    assert cards["coupons_effective"] == 3
    assert cards["coupons_historical"] == 1
    assert cards["coupon_used_success"] == 1
    assert cards["coupon_deduction_total"] == "20.00"
    # 订单域
    assert cards["orders_total"] == 1 and cards["orders_done"] == 1 and cards["orders_zero"] == 1
    # 趋势与联动结构
    assert len(d["coupon_usage_trend"]) == 7 and len(d["orders_trend"]) == 7
    assert sum(t["success"] for t in d["coupon_usage_trend"]) == 1
    assert sum(t["count"] for t in d["orders_trend"]) == 1
    assert any(s["scene"] == "外卖" and s["value"] == 2 for s in d["coupon_scenes"])
    assert d["generated_at"]


def test_permission_guard():
    # 未登录 401
    assert client.get("/api/ops/coupons/search").status_code == 401


def test_coupon_days_fields_and_bucket_overlap():
    """「剩余 N 天」端到端 + 桶覆盖修复（2026-09-29）：
    - sync-all 响应每张券带 days_remaining/validity_status（服务端唯一口径）；
    - coupons/search 每行同名字段 + stats.expiring/expired 统计（期望天数按同一
      自然日口径现场计算，抗运行日期漂移）；
    - wire 真实形态（historical-list 含全部券）：同券码在可用+历史两列表并见时，
      effective 必须胜出（此前历史后写覆盖致 40 张全 historical 的根因修复）。"""
    from datetime import date, datetime

    # 前序 no_available_accounts 用例把账号全置 disabled：先恢复在线，本用例要再跑全量同步
    db = fresh_db()
    for aid in (1, 3, 4):
        db.get(ChageeAccount, aid).status = "online"
    db.commit()
    db.close()
    r = client.post("/api/ops/coupons/sync-all", headers=H)
    assert r.status_code == 200
    for c in r.json()["coupons"]:
        assert "days_remaining" in c and "validity_status" in c

    end_ms = 1792684799000   # 夹具券统一截止毫秒（2026-11-22 23:59:59 CST）
    expected = (datetime.fromtimestamp(end_ms / 1000).date() - date.today()).days
    d = client.get("/api/ops/coupons/search", headers=H, params={"keyword": "C1001"}).json()
    assert d["total"] == 1
    item = d["items"][0]
    assert item["days_remaining"] == expected, item
    assert item["validity_status"] in ("active", "expiring", "expired", "unknown")
    assert "expiring" in d["stats"] and "expired" in d["stats"]

    from routers.ops import _persist_coupon_records
    db = fresh_db()
    acc = db.get(ChageeAccount, 1)
    dup = {"templateName": "重叠券", "benefitText": "5元",
           "useStartTimeStr": "2026-09-01 00:00", "useEndTimeStr": "2026-10-01 00:00"}
    result = {"coupons": [
        {**dup, "couponCode": "OVL1", "bucket": "可用"},
        {**dup, "couponCode": "OVL1", "bucket": "历史"},
        {**dup, "couponCode": "OVL2", "bucket": "历史"},
    ]}
    assert _persist_coupon_records(db, acc, result) == 3
    b1 = db.query(CouponRecord).filter_by(coupon_code="OVL1").one().bucket
    b2 = db.query(CouponRecord).filter_by(coupon_code="OVL2").one().bucket
    assert b1 == "effective", "同券码两列表并见时可用标签必须胜出"
    assert b2 == "historical"
    db.query(CouponRecord).filter(CouponRecord.coupon_code.in_(("OVL1", "OVL2"))).delete()
    db.commit()
    db.close()
    assert client.post("/api/ops/coupons/sync-all").status_code == 401
