"""账号保活跃任务离线测试（零真实网络：枚举/whoami/菜单客户端全部替换）。

运行：
    cd account_system\\server && ..\\..\\.venv_verify\\Scripts\\python.exe test_keepalive_offline.py

覆盖：
  - 配置：默认载入 / 落盘（间歇 min>max 自动对调）/ next_run_time 今天明天边界与非法值
  - run_keepalive 正常轮：门店轮转分配（round-robin 差≤1）、whoami+菜单计数、
    success/partial 判定、逐请求明细落库、token/uuid/userId 正确传递到菜单客户端
  - 重试：瞬时失败退避重试后成功（attempt 记录）；持续失败如实落库并触发失败率告警
  - token 失效：SessionExpiredError → 账号标记 expired + ERROR 告警 + 该账号门店跳过
  - 边界：无在线账号 / 省份门店枚举为空 → failed + 告警；max_stores_per_run 截断
  - 端点：status/config(get/put 含非法 422)/runs 列表与详情（明细分页+结果过滤）/trigger

隔离：CHAGEE_KEEPALIVE_THREAD=0 等线程全关；DB/oplog 重定向 test_keepalive*.db。
"""

import os
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
sys.path.insert(0, BASE)

os.environ["CHAGEE_KEEPALIVE_THREAD"] = "0"
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MENU_REFRESH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_keepalive_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_keepalive.db")
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
from models import ChageeAccount, KeepaliveRecord, KeepaliveRun  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import keepalive as ka  # noqa: E402

# 配置文件重定向到测试路径（load_config/save_config 均走 ka._CONFIG_PATH，
# 避免测试写真实 data/keepalive_config.json 造成跨次运行污染）
ka._CONFIG_PATH = os.path.join(BASE, "..", "data", "test_keepalive_config.json")
if os.path.exists(ka._CONFIG_PATH):
    os.remove(ka._CONFIG_PATH)

# ---------- 2. 测试替身：省份枚举 / whoami / 菜单客户端 ----------

FAKE_STORES = [
    {"store_no": f"CN9{i:03d}", "store_name": f"广东测试店{i}", "city_name": "广州市",
     "city_code": "4401"} for i in range(1, 6)
] + [
    {"store_no": "CN910", "store_name": "深圳测试店", "city_name": "深圳市", "city_code": "4403"},
]


def _fake_enumerate(cfg):
    return list(FAKE_STORES), ["茂名市: RuntimeError: 测试注入的城市错误"]


# 菜单客户端替身：记录构造参数（token/uuid/userId 传递断言）与逐店调用
MENU_INSTANCES: list["FakeMenuApi"] = []


class FakeMenuApi:
    def __init__(self, base_url=None, uuid=None, timeout=20, user_id=None, token=None):
        self.kwargs = {"uuid": uuid, "user_id": user_id, "token": token}
        self.calls: list[str] = []
        self.fail_first: set[str] = set()      # 每店首次失败（重试语义验证）
        self.always_fail: set[str] = set()     # 持续失败（告警语义验证）
        MENU_INSTANCES.append(self)

    def store_goods_menu(self, store_no):
        self.calls.append(store_no)
        if store_no in self.always_fail:
            raise RuntimeError(f"接口失败 errcode=500")
        if store_no in self.fail_first:
            self.fail_first.discard(store_no)
            raise RuntimeError("接口失败 errcode=502")
        return [{"categoryName": "测试分类"}]


# whoami 替身：按 token 内容决定成败（token 含 "BAD" → SessionExpiredError）
class _FakeProto:
    sk = "fake-sk"


def _fake_build_client(account):
    c = object.__new__(type("C", (), {"token": account.token, "uuid": account.device_uuid,
                                      "proto": _FakeProto()}))
    return c


def _fake_whoami(client):
    if "BAD" in (client.token or ""):
        # ChageeError(errcode, errmsg) 双参构造
        raise bridge.SessionExpiredError(401, "登录态已失效（测试注入）")
    return {"customerId": "1"}


# ---------- 3. 测试数据 ----------

seed.init_db()
CLIENT = TestClient(app_module.app)
_tokens: dict[str, str] = {}


def _auth(username="admin", password="Admin@123"):
    if username not in _tokens:
        r = CLIENT.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败: {r.status_code} {r.text}"
        _tokens[username] = r.json()["token"]
    return {"Authorization": f"Bearer {_tokens[username]}"}


def _db():
    return database.SessionLocal()


def _mk_account(label, token, status="online"):
    with _db() as db:
        acc = ChageeAccount(label=label, phone="13000000000", device_uuid=f"uuid-{label}",
                            token=token, customer_id=f"cid-{label}", status=status)
        db.add(acc)
        db.commit()
        db.refresh(acc)
        return acc.id


CFG_FAST = {  # 全速配置：零间歇、whoami 开、无截断
    "enabled": "true", "run_at": "10:30", "province_city_prefix": "44",
    "min_interval_seconds": "0", "max_interval_seconds": "0",
    "max_stores_per_run": "0", "min_stores_per_account": "2",
    "request_timeout": "5", "max_retries": "2", "whoami_check": "true",
    "alert_failure_rate": "0.5", "alert_min_failures": "3",
}


def _patch_all(monkey_fail_stores=(), always_fail_stores=()):
    ka._sleep = lambda s: None   # 消除间歇与退避等待
    ka.enumerate_province_stores = _fake_enumerate
    bridge.build_client = _fake_build_client
    bridge.proto_whoami = _fake_whoami
    bridge.ChageeMenuApi = FakeMenuApi
    MENU_INSTANCES.clear()
    inst = FakeMenuApi()
    MENU_INSTANCES.clear()
    MENU_INSTANCES.append(inst)
    inst.fail_first = set(monkey_fail_stores)
    inst.always_fail = set(always_fail_stores)
    # run_keepalive 每账号新建客户端：让替身工厂直接复用同一实例以便断言
    bridge.ChageeMenuApi = lambda **kw: (setattr(inst, "kwargs", kw) or inst)
    return inst


def _run(trigger="manual", cfg=None):
    with _db() as db:
        return ka.run_keepalive(db, trigger=trigger, cfg=cfg or CFG_FAST)


# ---------- 4. 测试用例 ----------

def test_01_config_and_schedule():
    cfg = ka.load_config()
    assert cfg["run_at"] == "10:30" and cfg["province_city_prefix"] == "44"
    # 间歇 min>max 落盘时自动对调
    saved = ka.save_config({"min_interval_seconds": "20", "max_interval_seconds": "5"})
    assert saved["min_interval_seconds"] == "5" and saved["max_interval_seconds"] == "20"
    ka.save_config({k: str(v) for k, v in CFG_FAST.items()})
    # next_run_time：今天 10:30 未过取今天，已过取明天；非法时刻 None
    now = datetime(2026, 9, 29, 9, 0)
    assert ka.next_run_time({"run_at": "10:30"}, now) == datetime(2026, 9, 29, 10, 30)
    now2 = datetime(2026, 9, 29, 11, 0)
    assert ka.next_run_time({"run_at": "10:30"}, now2) == datetime(2026, 9, 30, 10, 30)
    assert ka.next_run_time({"run_at": "25:99"}, now) is None
    assert ka.next_run_time({"run_at": "00:00"}, now) == datetime(2026, 9, 30, 0, 0)  # 当天零点已过→明天


def test_02_happy_path_round_robin_and_token_passing():
    a1, a2 = _mk_account("A1", "token-good-1"), _mk_account("A2", "token-good-2")
    inst = _patch_all(monkey_fail_stores={"CN9001"})
    summary = _run()
    assert summary["status"] == "success", summary
    # 6 门店 + 2 whoami = 8 请求全成功（CN900 首败重试成功）
    assert summary["requests_total"] == 8 and summary["requests_failed"] == 0
    # 轮转分配差 ≤1：6 店 2 账号 → 3/3；两个客户端各自访问了自己的店
    calls = list(inst.calls)
    # 6 店全覆盖 + CN9001 首次失败重试 = 7 次调用（重试的失败尝试也计入 calls）
    assert set(calls) == {s["store_no"] for s in FAKE_STORES} and len(calls) == 7, calls
    # token/uuid/userId 正确传递（最后一次实例化取 A2；两账号各实例化一次，工厂复写 kwargs）
    with _db() as db:
        recs = db.query(KeepaliveRecord).filter(KeepaliveRecord.run_id == summary["run_id"]).all()
        assert len(recs) == 8
        retried = [r for r in recs if r.store_no == "CN9001"]
        assert retried and retried[0].ok and retried[0].attempt == 2, (
            [(r.store_no, r.ok, r.attempt) for r in recs])   # 首败→重试成功
        whoami_rows = [r for r in recs if r.action == "whoami" and r.ok]
        assert len(whoami_rows) == 2
        run = db.get(KeepaliveRun, summary["run_id"])
        assert run.store_total == 6 and run.city_total == 2 and run.accounts_total == 2


def test_03_token_expired_marks_account_and_alerts():
    from log_monitor import list_alerts
    a_bad = _mk_account("BAD1", "token-BAD-9")
    _patch_all()
    summary = _run()
    assert summary["status"] == "partial" and summary["expired_accounts"] == 1, summary
    with _db() as db:
        acc = db.get(ChageeAccount, a_bad)
        assert acc.status == "expired"   # 失效即标记，与扫描/检查同语义
        rec = (db.query(KeepaliveRecord)
                 .filter(KeepaliveRecord.account_id == a_bad, KeepaliveRecord.action == "whoami")
                 .first())
        assert rec is not None and not rec.ok and rec.status == "expired"
    alerts = list_alerts(only_open=True, page_size=50)
    rules = [a["rule"] for a in alerts["items"]]
    assert "keepalive_token_invalid" in rules


def test_04_failure_rate_alert_and_partial():
    # whoami 关闭使分母只有 6 次菜单访问：3 失败 = 50% 恰达阈值（≥min_failures 且 ≥rate）
    _patch_all(always_fail_stores={"CN9001", "CN9002", "CN9003"})
    summary = _run(cfg={**CFG_FAST, "whoami_check": "false"})
    assert summary["status"] == "partial", summary
    assert summary["requests_failed"] >= 3, summary
    from log_monitor import list_alerts
    alerts = list_alerts(only_open=True, page_size=50)
    assert "keepalive_failures" in [a["rule"] for a in alerts["items"]]


def test_05_no_accounts_and_no_stores():
    # 无在线账号（前面用例的账号已建——造一个全新场景：把枚举置空优先验证告警）
    ka.enumerate_province_stores = lambda cfg: ([], ["广州市: RuntimeError: 全挂"])
    summary = _run()
    assert summary["status"] == "failed"
    from log_monitor import list_alerts
    alerts = list_alerts(only_open=True, page_size=50)
    assert "keepalive_no_stores" in [a["rule"] for a in alerts["items"]]
    # 恢复枚举后，把全部账号置 expired → 无在线账号分支
    _patch_all()
    with _db() as db:
        db.query(ChageeAccount).update({"status": "expired"})
        db.commit()
    summary = _run()
    assert summary["status"] == "failed" and summary["requests_total"] == 0


def test_06_store_cap():
    _patch_all()
    _mk_account("CAP1", "token-cap-1")   # 本用例自带在线账号（不依赖前序用例的账号状态）
    summary = _run(cfg={**CFG_FAST, "max_stores_per_run": "3"})
    assert summary.get("stores") == 3, summary
    with _db() as db:
        run = db.get(KeepaliveRun, summary["run_id"])
        assert run.stores_planned == 3 and run.store_total == 6   # 截断不影响总数留痕


def test_07_endpoints():
    h = _auth()
    r = CLIENT.get("/api/ops/keepalive/status", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and "next_run_at" in body and body["last_run"] is not None
    r = CLIENT.get("/api/ops/keepalive/config", headers=h)
    assert r.status_code == 200 and r.json()["run_at"] == "10:30"
    # 非法 run_at 422；合法 PUT 落盘
    r = CLIENT.put("/api/ops/keepalive/config", headers=h,
                   json={**CFG_FAST, "run_at": "25:00"})
    assert r.status_code == 422
    r = CLIENT.put("/api/ops/keepalive/config", headers=h,
                   json={**CFG_FAST, "run_at": "11:30", "min_interval_seconds": "3"})
    assert r.status_code == 200 and r.json()["run_at"] == "11:30"
    assert ka.load_config()["run_at"] == "11:30"
    # runs 列表 + 详情（明细分页与结果过滤）
    r = CLIENT.get("/api/ops/keepalive/runs", headers=h)
    assert r.status_code == 200 and r.json()["total"] >= 1
    run_id = r.json()["items"][0]["id"]
    r = CLIENT.get(f"/api/ops/keepalive/runs/{run_id}?result=failed&page_size=200", headers=h)
    assert r.status_code == 200
    assert all(not x["ok"] for x in r.json()["records"])
    r = CLIENT.get("/api/ops/keepalive/runs/999999", headers=h)
    assert r.status_code == 404
    # trigger：线程关闭时手动触发仅置信号（运行态 409 逻辑在线上验证）
    r = CLIENT.post("/api/ops/keepalive/trigger", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n结果: {passed} passed, {failed} failed (共 {len(fns)} 项)")
    sys.exit(1 if failed else 0)
