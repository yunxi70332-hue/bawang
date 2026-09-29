"""异步订单中枢队列语义离线测试（services/order_queue，零 HTTP / 零网络）。

覆盖：入队-登记同事务原子性 / 原子认领（多线程不重不漏）/ ack 幂等 /
fail 退避递增与可见性 / attempts 耗尽 → 死信 / kill 直达死信 / 孤儿回收（崩溃恢复）/
死信重放 / 队列统计。运行：pytest test_queue_offline.py -q
"""

import os
import sys
import threading
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# 离线开关（conftest 已置 worker/backup 线程 0；此处显式声明便于单文件直跑）
os.environ["CHAGEE_ORDER_WORKERS"] = "0"
os.environ["CHAGEE_BACKUP_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MENU_REFRESH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_queue_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

# 先改库路径再 import 任何 server 模块（同 test_orders_offline 手法）
import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_queue.db")
if os.path.exists(database.DB_PATH):
    os.remove(database.DB_PATH)
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

database.engine = create_engine(
    f"sqlite:///{database.DB_PATH}", connect_args={"check_same_thread": False}, pool_pre_ping=True)
database.SessionLocal = sessionmaker(bind=database.engine, autoflush=False,
                                     autocommit=False, expire_on_commit=False)

import seed  # noqa: E402
from models import CustomerOrder, OrderMessage  # noqa: E402
from services import order_queue  # noqa: E402

seed.init_db()


def _mk_co(db, no: str) -> CustomerOrder:
    co = CustomerOrder(customer_order_no=no, source="internal", status="enqueued",
                       payload={"customer_order_no": no})
    db.add(co)
    db.flush()
    return co


def _clean_messages() -> None:
    """清空队列（claim_one 是全局 FIFO，用例间必须清场防跨用例拾取残留消息）。"""
    with database.SessionLocal() as db:
        db.query(OrderMessage).delete()
        db.commit()


# ---------------- 基础语义 ----------------

def test_01_enqueue_with_order_atomic():
    _clean_messages()
    """登记+入队同事务：commit 前队列查不到，commit 后一并可见（原子性）。"""
    with database.SessionLocal() as db:
        co = _mk_co(db, "Q-ATOMIC-1")
        order_queue.enqueue(db, co.id, payload={"k": 1})
        with database.SessionLocal() as other:   # 另一连接：commit 前不可见
            assert other.query(OrderMessage).filter_by(customer_order_id=co.id).count() == 0
        db.commit()
    with database.SessionLocal() as db:
        msg = db.query(OrderMessage).filter_by(customer_order_id=co.id).one()
        assert msg.status == "pending" and msg.attempts == 0 and msg.max_attempts == 3


def test_02_claim_atomic_and_ack():
    _clean_messages()
    """认领：pending→processing、attempts+1；ack 后 done；重复 ack 幂等。"""
    with database.SessionLocal() as db:
        co = _mk_co(db, "Q-CLAIM-1")
        order_queue.enqueue(db, co.id)
        db.commit()
        cid = co.id
    claimed = order_queue.claim_one("w-test")
    assert claimed is not None and claimed["customer_order_id"] == cid
    assert claimed["attempts"] == 1
    with database.SessionLocal() as db:
        msg = db.query(OrderMessage).filter_by(customer_order_id=cid).one()
        assert msg.status == "processing" and msg.locked_by == "w-test"
    order_queue.ack(claimed["id"])
    order_queue.ack(claimed["id"])   # 幂等：done 态再 ack 不变
    with database.SessionLocal() as db:
        assert db.query(OrderMessage).filter_by(customer_order_id=cid).one().status == "done"
    assert order_queue.claim_one("w-test") is None   # 队列已空


def test_03_fail_backoff_and_dead():
    _clean_messages()
    """失败退避：attempts<max → pending + next_visible_at 推后；耗尽 → dead（死信）。"""
    with database.SessionLocal() as db:
        co = _mk_co(db, "Q-FAIL-1")
        order_queue.enqueue(db, co.id, max_attempts=3)
        db.commit()
    now = datetime.now()
    # 第 1 次失败：认领时 attempts=1 → 30s 退避
    m1 = order_queue.claim_one("w1")
    assert order_queue.fail(m1["id"], "boom-1", m1["attempts"], m1["max_attempts"]) == "pending"
    with database.SessionLocal() as db:
        msg = db.query(OrderMessage).get(m1["id"])
        assert msg.status == "pending" and msg.last_error == "boom-1"
        assert msg.next_visible_at > now + timedelta(seconds=25)   # 30s 阶梯
        assert msg.next_visible_at < now + timedelta(seconds=60)
    # 可见性窗口：退避未到不可认领
    assert order_queue.claim_one("w1") is None
    # 手动把可见时间拉回当下 → 可再认领（第 2 次）
    with database.SessionLocal() as db:
        db.execute(text("UPDATE order_messages SET next_visible_at=:n WHERE id=:i"),
                   {"n": datetime.now(), "i": m1["id"]})
        db.commit()
    m2 = order_queue.claim_one("w2")
    assert m2["attempts"] == 2
    assert order_queue.fail(m2["id"], "boom-2", m2["attempts"], m2["max_attempts"]) == "pending"
    with database.SessionLocal() as db:
        db.execute(text("UPDATE order_messages SET next_visible_at=:n WHERE id=:i"),
                   {"n": datetime.now(), "i": m1["id"]})
        db.commit()
    # 第 3 次（= max_attempts）：耗尽 → dead
    m3 = order_queue.claim_one("w3")
    assert m3["attempts"] == 3
    assert order_queue.fail(m3["id"], "boom-3", m3["attempts"], m3["max_attempts"]) == "dead"
    with database.SessionLocal() as db:
        assert db.query(OrderMessage).get(m1["id"]).status == "dead"


def test_04_kill_straight_to_dead():
    _clean_messages()
    """kill：致命失败不消耗退避，直接死信（last_error 带 fatal 语义）。"""
    with database.SessionLocal() as db:
        co = _mk_co(db, "Q-KILL-1")
        order_queue.enqueue(db, co.id)
        db.commit()
    m = order_queue.claim_one("w1")
    order_queue.kill(m["id"], "决策拦截：利润不足")
    with database.SessionLocal() as db:
        msg = db.query(OrderMessage).get(m["id"])
        assert msg.status == "dead" and "利润不足" in msg.last_error


def test_05_reclaim_orphans():
    _clean_messages()
    """孤儿回收（崩溃恢复）：processing 超 visibility timeout → 复位 pending 且立即可见。"""
    with database.SessionLocal() as db:
        co = _mk_co(db, "Q-ORPHAN-1")
        order_queue.enqueue(db, co.id)
        db.commit()
    m = order_queue.claim_one("w-crashed")
    assert m is not None
    assert order_queue.reclaim_orphans(timeout_seconds=600) == 0   # 未超时不动
    with database.SessionLocal() as db:   # 把 locked_at 拨回 1 小时前（模拟 worker 崩溃）
        db.execute(text("UPDATE order_messages SET locked_at=:past WHERE id=:i"),
                   {"past": datetime.now() - timedelta(hours=1), "i": m["id"]})
        db.commit()
    assert order_queue.reclaim_orphans(timeout_seconds=600) == 1
    m2 = order_queue.claim_one("w2")   # 复位后立即可再认领（attempts 继续累计）
    assert m2 is not None and m2["attempts"] == 2


def test_06_requeue_dead_resets_attempts():
    _clean_messages()
    """死信重放：dead → pending，attempts/last_error 归零。"""
    with database.SessionLocal() as db:
        co = _mk_co(db, "Q-RQ-1")
        order_queue.enqueue(db, co.id)
        db.commit()
    m = order_queue.claim_one("w1")
    order_queue.kill(m["id"], "fatal-x")
    assert order_queue.requeue_dead(m["id"]) is True
    m2 = order_queue.claim_one("w2")
    assert m2["attempts"] == 1   # 重放后额度重置（认领 +1 从 0 起）


def test_07_concurrent_claim_no_dup_no_loss():
    _clean_messages()
    """并发认领：4 线程抢 50 条消息——恰好 50 次认领、零重复、零遗漏。"""
    total = 50
    with database.SessionLocal() as db:
        for i in range(total):
            co = _mk_co(db, f"Q-CONC-{i}")
            order_queue.enqueue(db, co.id)
        db.commit()
    claimed: list[int] = []
    lock = threading.Lock()

    def _worker(wid: str):
        for _ in range(total):
            m = order_queue.claim_one(wid)
            if m is None:
                break
            with lock:
                claimed.append(m["id"])

    threads = [threading.Thread(target=_worker, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == total, f"认领数 {len(claimed)} != {total}"
    assert len(set(claimed)) == total, "存在重复认领"


def test_08_queue_stats():
    """统计：分组计数正确（fail-soft 永不抛）。自建 pending/processing/done/dead 各一。"""
    _clean_messages()
    with database.SessionLocal() as db:
        for no in ("Q-STAT-P", "Q-STAT-W", "Q-STAT-D1", "Q-STAT-X"):
            co = _mk_co(db, no)
            order_queue.enqueue(db, co.id)
        db.commit()
    m = order_queue.claim_one("w-stat")          # → processing
    order_queue.ack(m["id"])                     # → done
    order_queue.claim_one("w-stat")              # → 留在 processing 态
    m3 = order_queue.claim_one("w-stat")
    order_queue.kill(m3["id"], "fatal")          # → dead
    stats = order_queue.queue_stats()
    assert set(stats) >= {"pending", "processing", "done", "dead"}
    assert stats["pending"] == 1 and stats["processing"] == 1
    assert stats["done"] == 1 and stats["dead"] == 1


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        fn()
        print(f"[ok] {name}")
    print("ALL QUEUE TESTS PASSED")
