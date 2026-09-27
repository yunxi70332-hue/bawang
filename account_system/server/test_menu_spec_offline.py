"""本地菜单规格库离线测试（wire 回放，零真实网络请求）。

运行：
    cd account_system\\server && ..\\..\\.venv_verify\\Scripts\\python.exe test_menu_spec_offline.py

覆盖：
  - refresh_store_menu 三表落库 + 首刷 diff / 二刷幂等
  - get_goods_cached 本地优先（TTL 内零线上调用；陈旧回源 write-through）
  - /api/ops/goods 端点结构不变（前端零改动）
  - 兜底命中链：skuId 直查（L1/L2）、文案解析（精确/别名/包含）、歧义拒猜
  - 端点：POST /menu/spec/refresh、GET /menu/spec/status、GET /menu/spec/resolve

夹具：output/menu-CN00529（杭州店 82 SPU 真实快照）。测试样本即客户平台真实单：
青青糯山（spuId 640215811876831233 / 大杯 skuId 655441178728091650 = 客户 linkId），
文案 "大杯/少冰/半糖"（半糖 = attributeOptionId 623882672850116613，仅部分 SPU 可选）。
"""

import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根
sys.path.insert(0, BASE)

# 禁用两个后台线程（校准线程 + 菜单刷新线程）：避免测试进程残留副作用
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MENU_REFRESH_INTERVAL_SECONDS"] = "0"
# 日志隔离：oplog 日志库与文本日志均指向测试路径，避免污染生产 data/logs/
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_menu_spec_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# ---------- 1. 先改库路径再 import 任何 server 模块 ----------
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_menu_spec.db")
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
from models import MenuGoodsCache, MenuRefreshLog, MenuSpecOption  # noqa: E402
from security import hash_password  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services import menu_spec as ms  # noqa: E402

# ---------- 2. 快照回放假菜单客户端（替换 bridge.menu_api，杜绝真实网络） ----------

with open(os.path.join(ROOT, "output", "menu-CN00529", "03_storeGoodsMenu.json"),
          encoding="utf-8") as f:
    MENU = json.load(f)
with open(os.path.join(ROOT, "output", "menu-CN00529", "04_goods_details.json"),
          encoding="utf-8") as f:
    DETAILS = json.load(f)
DETAIL_MAP = {str(d.get("spuId")): d for d in DETAILS}

STORE = "CN00529"
QNS_SPU = "640215811876831233"      # 青青糯山
QNS_SKU_BIG = "655441178728091650"  # 大杯 = 客户平台 linkId
BYJX_SPU = "625339451983278080"     # 伯牙绝弦（甜度无半糖，商品级选项集差异样本）
HALF_SUGAR_ID = "623882672850116613"
NO_SUGAR_ID = "623882672850116610"
CUP_SPEC = ("653599312273510400", "653599312273510402")   # 杯型组/大杯
TEMP_GROUP = "745317722624679942"


class FakeMenuApi:
    """回放 CN00529 快照；detail_calls 统计线上调用次数（本地优先断言依据）。"""

    detail_calls = 0
    menu_calls = 0

    def store_goods_menu(self, store_no, sale_type="1", sale_channel="2"):
        FakeMenuApi.menu_calls += 1
        return MENU

    def goods_detail(self, spu_id, store_no, sale_type="1", sale_channel="2"):
        FakeMenuApi.detail_calls += 1
        if spu_id not in DETAIL_MAP:
            raise KeyError(f"快照无此 SPU: {spu_id}")
        return DETAIL_MAP[spu_id]


bridge.menu_api = lambda: FakeMenuApi()   # monkeypatch（ops 路由与 menu_spec 服务共用 bridge）

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


# ---------- 4. 测试用例（按序号保证执行顺序） ----------

def test_01_refresh_persist_and_diff():
    with _db() as db:
        summary = ms.refresh_store_menu(db, STORE, trigger="manual")
    spu_rows = [spu for cat in MENU for spu in (cat.get("spuList") or [])]
    unique_spus = len({str(s.get("spuId")) for s in spu_rows if s.get("spuId")})
    assert summary["spu_total"] == len(spu_rows), f"菜单行数不符: {summary}"
    # 多分类展示致同 SPU 重复出现（82 行 62 个唯一 SPU）：落库按 store×spu 去重
    assert summary["spu_new"] == unique_spus and summary["failed"] == []
    with _db() as db:
        assert db.query(MenuGoodsCache).filter(MenuGoodsCache.store_no == STORE).count() == unique_spus
        assert db.query(MenuSpecOption).count() > 0
        log = db.query(MenuRefreshLog).order_by(MenuRefreshLog.id.desc()).first()
        assert log and log.trigger == "manual" and log.spu_new == unique_spus
        # 新品名进了 diff 明细（地区限定/新品追踪入口）
        names = {n["spuName"] for n in log.detail.get("new_spus", [])}
        assert "青青糯山" in names


def test_02_refresh_idempotent():
    with _db() as db:
        before = db.query(MenuGoodsCache).count()
        summary = ms.refresh_store_menu(db, STORE, trigger="scheduled")
    assert summary["spu_new"] == 0 and summary["spu_changed"] == 0
    with _db() as db:
        assert db.query(MenuGoodsCache).count() == before   # 无重复行


def test_03_goods_cached_local_first():
    with _db() as db:
        before = FakeMenuApi.detail_calls
        detail = ms.get_goods_cached(db, QNS_SPU, STORE)
        assert detail.get("name") == "青青糯山"
        assert FakeMenuApi.detail_calls == before, "TTL 内不应回源"
        # 陈旧（fetched_at 拨回 25h 前）→ 回源一次并 write-through
        row = (db.query(MenuGoodsCache)
                 .filter(MenuGoodsCache.store_no == STORE, MenuGoodsCache.spu_id == QNS_SPU).one())
        import datetime as _dt
        row.fetched_at = _dt.datetime.now() - _dt.timedelta(seconds=ms.MENU_TTL_SECONDS + 3600)
        db.commit()
        ms.get_goods_cached(db, QNS_SPU, STORE)
        assert FakeMenuApi.detail_calls == before + 1, "陈旧快照应回源一次"
        row = (db.query(MenuGoodsCache)
                 .filter(MenuGoodsCache.store_no == STORE, MenuGoodsCache.spu_id == QNS_SPU).one())
        assert row.raw.get("name") == "青青糯山"


def test_04_goods_endpoint_shape():
    r = CLIENT.get("/api/ops/goods", params={"spu_id": QNS_SPU, "store": STORE}, headers=_auth())
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("specGroups", "attributes", "extras", "skus", "spuName"):
        assert key in d, f"缺少键 {key}"
    assert d["spuName"] == "青青糯山"
    assert any(o.get("specOptionName") == "大杯" for g in d["specGroups"] for o in g.get("specOptions", []))
    assert any(o.get("name") == "半糖" for g in d["attributes"] for o in g.get("attrOptions", []))
    sku = next(s for s in d["skus"] if s["skuId"] == QNS_SKU_BIG)
    assert sku["price"] == "18.00" and "大杯" in sku["specDesc"]


def test_05_resolve_by_sku_l1():
    with _db() as db:
        before = FakeMenuApi.menu_calls
        hit = ms.resolve_by_sku(db, STORE, QNS_SKU_BIG)
    assert hit["spu_id"] == QNS_SPU and hit["spu_name"] == "青青糯山"
    assert hit["price"] == "18.00"
    specs = {s["specOptionId"] for s in hit["specs"]}
    assert CUP_SPEC[1] in specs
    assert FakeMenuApi.menu_calls == before, "本地命中不应触发全店回源"


def test_06_resolve_texts_customer_scenario():
    """客户平台真实场景：文案 大杯/少冰/半糖 → ID 组合（半糖=613）。"""
    with _db() as db:
        out = ms.resolve_spec_texts(db, STORE, QNS_SPU, ["大杯", "少冰", "半糖"])
    spec = out["spec_list"]
    assert len(spec) == 1 and spec[0]["specId"] == CUP_SPEC[0] and spec[0]["specOptionId"] == CUP_SPEC[1]
    attrs = {a["attributeId"]: a for a in out["attribute_list"]}
    assert attrs[TEMP_GROUP]["attributeOptionName"] == "少冰"
    assert attrs["623882672850116609"]["attributeOptionId"] == HALF_SUGAR_ID
    assert out["resolved"].get("甜度") == "半糖"
    # 未指定的必选组（若有）回落 defaulted 默认项——App 同款行为
    for g, name in out["resolved"].items():
        assert name


def test_07_alias_and_contains():
    with _db() as db:
        out = ms.resolve_spec_texts(db, STORE, QNS_SPU, ["大杯", "少冰", "无糖"])
    attrs = {a["attributeId"]: a for a in out["attribute_list"]}
    assert attrs["623882672850116609"]["attributeOptionId"] == NO_SUGAR_ID, "别名 无糖→不另外加糖"
    assert out["resolved"]["甜度"] == "不另外加糖"


def test_08_ambiguity_rejected():
    """零候选必须拒绝并给出候选，不许瞎猜（宁停不错）。"""
    with _db() as db:
        try:
            ms.resolve_spec_texts(db, STORE, QNS_SPU, ["大杯", "加十颗珍珠"])
        except ms.SpecResolveError as e:
            assert "加十颗珍珠" in str(e)
            assert e.candidates, "歧义错误应携带候选列表"
        else:
            raise AssertionError("零候选文案不应解析成功")


def test_09_half_sugar_is_spu_scoped():
    """商品级选项集差异：伯牙绝弦甜度无半糖 → 半糖文案在其上应报错并列出真实候选。"""
    with _db() as db:
        try:
            ms.resolve_spec_texts(db, STORE, BYJX_SPU, ["大杯", "少冰", "半糖"])
        except ms.SpecResolveError as e:
            assert "半糖" in str(e)
            assert "标准糖" in (e.candidates or []) or "标准糖" in str(e)
        else:
            raise AssertionError("伯牙绝弦无半糖选项，应拒绝而非猜")


def test_10_l2_refetch_on_cold_cache():
    """冷库（清空缓存表）→ resolve_by_sku 触发全店回源（L2）后命中。"""
    with _db() as db:
        db.query(MenuGoodsCache).delete()
        db.commit()
        before = FakeMenuApi.menu_calls
        hit = ms.resolve_by_sku(db, STORE, QNS_SKU_BIG)
    assert hit["spu_id"] == QNS_SPU
    assert FakeMenuApi.menu_calls == before + 1, "冷库应触发一次全店回源"
    with _db() as db:
        assert db.query(MenuGoodsCache).filter(MenuGoodsCache.store_no == STORE).count() > 0
        log = db.query(MenuRefreshLog).order_by(MenuRefreshLog.id.desc()).first()
        assert log.trigger == "ttl_miss"


def test_11_endpoints_refresh_status_resolve():
    h = _auth()
    r = CLIENT.post("/api/ops/menu/spec/refresh", json={"store_no": STORE}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["refreshed"] == 1
    r = CLIENT.get("/api/ops/menu/spec/status", headers=h)
    assert r.status_code == 200
    st = r.json()
    assert st["spu_cached"] > 0 and st["spec_options"] > 0 and STORE in st["stores"]
    assert st["recent_refreshes"][0]["trigger"] == "manual"
    # 客户场景端到端：linkId → 文案 → ID 组合
    r = CLIENT.get("/api/ops/menu/spec/resolve",
                   params={"store": STORE, "sku_id": QNS_SKU_BIG, "spec": "大杯/少冰/半糖"},
                   headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["spu_id"] == QNS_SPU
    assert any(a["attributeOptionId"] == HALF_SUGAR_ID for a in d["attribute_list"])
    # 歧义 → 422 + 候选
    r = CLIENT.get("/api/ops/menu/spec/resolve",
                   params={"store": STORE, "sku_id": QNS_SKU_BIG, "spec": "爆爆珠"},
                   headers=h)
    assert r.status_code == 422


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
