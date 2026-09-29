"""异步订单中枢压力测试（接收吞吐 + 队列消费吞吐，离线零真实网络）。

Phase 1 接收压测：8 线程并发提交 320 单（内部 JWT 路径）——断言零丢失/零重复/全 202，
输出接收吞吐（单/秒）。接收路径 = Pydantic 校验 + 菜单库预检 + SQLite 毫秒级短事务，
吞吐即 SQLite WAL 单写者上限的直接量化（上线后可重复运行对比）。

Phase 2 消费吞吐：将 worker 执行链桩化为纯本地操作（settle/create 返回最小结果），
排空 320 条消息——量化「SQLite 队列 + 状态机推进 + SSE 推送 + oplog 打标」的
消费管线本身的上限（全自动真实链路的耗时由茶姬远程接口主导，与队列无关，
全链语义正确性已在 test_intake_offline.py 覆盖）。

运行：pytest test_intake_stress.py -q -s（-s 看吞吐报告）
"""

import json
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
sys.path.insert(0, BASE)

os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MENU_REFRESH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MINT_ENABLED"] = "0"
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"
os.environ["CHAGEE_TUNNEL_ENABLED"] = "0"
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_stress_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_intake_stress.db")
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
from models import ChageeAccount, CustomerOrder, MenuGoodsCache, OrderMessage  # noqa: E402
from services import order_worker  # noqa: E402

TEST_SKU = "653632618000097282"
TEST_SPU = "625339451983278080"
STORE_NO = "CN03324"

seed.init_db()
with database.SessionLocal() as db:
    if not db.query(ChageeAccount).filter(ChageeAccount.phone == "13800003333").first():
        db.add(ChageeAccount(label="压测账号", phone="13800003333", device_uuid="uuid-offline-stress-1",
                             token="fake.token.stress", sk="fakesk", customer_id="1190018250",
                             status="online", group="默认"))
        db.commit()
    if not db.query(MenuGoodsCache).filter_by(store_no=STORE_NO, spu_id=TEST_SPU).first():
        db.add(MenuGoodsCache(
            store_no=STORE_NO, spu_id=TEST_SPU, spu_name="伯牙绝弦-压测", spu_type="stand",
            status=1, default_price="20",
            sku_index={TEST_SKU: {"price": "20.00", "stock": 999,
                                  "itemSkuId": "653632618000097282",
                                  "specs": [{"specId": "653599312273510400",
                                             "specOptionId": "653599312273510401",
                                             "specOptionName": "大杯"}]}},
            spec_groups=[], attribute_groups=[], extra_groups=[], raw={},
            fetched_at=__import__("datetime").datetime.now()))
        db.commit()
    db.query(CustomerOrder).delete()
    db.query(OrderMessage).delete()
    db.commit()

CLIENT = TestClient(app_module.app)
_r = CLIENT.post("/api/auth/login", json={"username": "admin", "password": "Admin@123"})
assert _r.status_code == 200, _r.text
TOKEN = _r.json()["token"]

TOTAL = 320
THREADS = 8


def test_01_intake_concurrent_throughput():
    """8 线程并发 320 单：零丢失/零重复/全 202，输出接收吞吐。"""
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def _submitter(tid: int):
        client = TestClient(app_module.app)   # 线程独立客户端（并发安全）
        headers = {"Authorization": f"Bearer {TOKEN}"}
        local = []
        for i in range(TOTAL // THREADS):
            no = f"STRESS-{tid:02d}-{i:04d}"
            r = client.post("/api/intake/orders", headers=headers, json={
                "customer_order_no": no, "store_no": STORE_NO, "sku_id": TEST_SKU,
                "quantity": 1, "spec_texts": [], "customer_price": "22"})
            local.append((r.status_code, r.json().get("customer_order_no", "")))
        with lock:
            results.extend(local)

    started = time.perf_counter()
    threads = [threading.Thread(target=_submitter, args=(t,)) for t in range(THREADS)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    elapsed = time.perf_counter() - started

    assert len(results) == TOTAL
    assert all(sc == 202 for sc, _ in results), [x for x in results if x[0] != 202][:3]
    nos = [no for _, no in results]
    assert len(set(nos)) == TOTAL, "存在重复登记（幂等键碰撞）"
    with database.SessionLocal() as db:
        assert db.query(CustomerOrder).count() == TOTAL, "登记单丢失"
        assert db.query(OrderMessage).filter(OrderMessage.status == "pending").count() == TOTAL
    print(f"\n[压测] 接收：{TOTAL} 单 / {elapsed:.2f}s = {TOTAL / elapsed:.1f} 单/秒"
          f"（{THREADS} 线程并发，含菜单预检+登记+入队事务）")


def test_02_worker_pipeline_throughput():
    """消费管线吞吐：执行链桩化后排空 320 条——量化队列+状态机+SSE+oplog 本身上限。"""
    real_settle, real_create = order_worker.settle_core, order_worker.create_core
    real_decide = order_worker.decide_core
    # 桩化：decide 全额通过（account 固定压测账号）、settle/create 即返——纯本地管线
    with database.SessionLocal() as db:
        acc_id = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800003333").one().id

    def _stub_decide(db, req, **kw):
        return {"verdict": "pass", "account_id": acc_id, "coupon": None,
                "decision_log_id": 0,
                "settle_prefill": {"spu_id": TEST_SPU, "spu_name": "压测", "sku_id": req.sku_id,
                                   "sku_name": "压测", "item_sku_id": "", "quantity": req.quantity,
                                   "spec_list": [], "attribute_list": [], "extra_list": [],
                                   "sale_price": 20.0, "spu_type": "stand"}}

    def _stub_settle(db, account, body, **kw):
        return {"draft_id": "d-stress", "preview": {}}

    def _stub_create(db, account, body, **kw):
        return {"result": "zero", "order_no": f"SO-{os.urandom(4).hex()}",
                "status": 3, "status_label": "制作中", "pickup_no": "SP01",
                "pay_amount": "0", "coupon_code": None}

    order_worker.decide_core = _stub_decide
    order_worker.settle_core = _stub_settle
    order_worker.create_core = _stub_create
    try:
        started = time.perf_counter()
        consumed = 0
        while True:
            outcome = order_worker.process_once("w-stress")
            if outcome == "idle":
                break
            consumed += 1
            assert outcome == "done", f"桩化管线不应失败：{outcome}"
        elapsed = time.perf_counter() - started
        assert consumed == TOTAL, f"消费 {consumed} != {TOTAL}"
        with database.SessionLocal() as db:
            done = db.query(OrderMessage).filter_by(status="done").count()
            completed = db.query(CustomerOrder).filter_by(status="completed").count()
            assert done == TOTAL and completed == TOTAL
        print(f"[压测] 消费管线：{consumed} 条 / {elapsed:.2f}s = {consumed / elapsed:.1f} 条/秒"
              f"（单 worker，含状态机+SSE 推送+oplog 三次打标）")
    finally:
        order_worker.decide_core = real_decide
        order_worker.settle_core = real_settle
        order_worker.create_core = real_create


if __name__ == "__main__":
    test_01_intake_concurrent_throughput()
    test_02_worker_pipeline_throughput()
    print("ALL STRESS TESTS PASSED")
