# -*- coding: utf-8 -*-
"""支付窗 10 分钟钳制（clamp_pay_deadline）离线测试。

背景：官方待支付单 paymentExpiryTimestamp=下单+10min、autoCancel、不随 continuePay
重置；支付宝 time_expire=下单+30min 且续付顺延。pay_deadline 必须钳制到官方窗，
否则 10-30 分钟间支付会「支付宝扣款成功、茶姬订单已取消」，支付无回传。
运行（须单独跑）：python -m pytest test_pay_window_offline.py -q
"""
import os
import sys
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MINT_ENABLED"] = "0"
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"   # 离线禁发跨进程广播（mark_session 会触发）
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_paywin_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_pay_window.db")
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

seed.init_db()

from models import ChageeAccount, OrderRecord, PaySession  # noqa: E402
from services import chagee_bridge as bridge  # noqa: E402
from services.pay_session import (  # noqa: E402
    PAY_WINDOW_SECONDS, clamp_pay_deadline, ensure_pay_session, upsert_order_record,
)

WINDOW = timedelta(seconds=PAY_WINDOW_SECONDS)


# ---------- 纯函数 ----------

def test_clamp_30min_deadline_to_official_window():
    now = datetime.now()
    clamped = clamp_pay_deadline(now + timedelta(minutes=30), now)
    assert abs((clamped - (now + WINDOW)).total_seconds()) < 1


def test_clamp_keeps_shorter_deadline():
    now = datetime.now()
    dl = now + timedelta(minutes=5)
    assert clamp_pay_deadline(dl, now) == dl


def test_clamp_none_returns_official_window():
    now = datetime.now()
    clamped = clamp_pay_deadline(None, now)
    assert abs((clamped - (now + WINDOW)).total_seconds()) < 1


def test_clamp_accepts_text_expire():
    now = datetime.now()
    far = (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    clamped = clamp_pay_deadline(far, now)
    assert abs((clamped - (now + WINDOW)).total_seconds()) < 1


def test_clamp_default_anchor_is_now():
    far = datetime.now() + timedelta(minutes=30)
    clamped = clamp_pay_deadline(far)
    assert datetime.now() + WINDOW - timedelta(seconds=5) <= clamped <= datetime.now() + WINDOW


def test_clamp_idempotent():
    now = datetime.now()
    dl = clamp_pay_deadline(now + timedelta(minutes=30), now)
    assert clamp_pay_deadline(dl, now) == dl


def test_clamp_remint_keeps_original_anchor():
    """续付场景：continuePay 顺延的 time_expire 不重置官方倒计时（锚仍为下单时刻）。"""
    created = datetime.now() - timedelta(minutes=8)
    renewed = created + timedelta(minutes=38)          # 续付顺延后距下单 38 分钟
    clamped = clamp_pay_deadline(renewed, created)
    assert clamped == created + WINDOW                 # = 下单+10min（已过期 2 分钟）


# ---------- upsert 层 ----------

def _acc_id() -> int:
    with database.SessionLocal() as db:
        acc = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800007777").first()
        if not acc:
            acc = ChageeAccount(label="窗口离线", phone="13800007777",
                                device_uuid="uuid-offline-paywin-1", token="t", sk="s")
            db.add(acc)
            db.commit()
        return acc.id


def test_upsert_clamps_new_order_deadline():
    acc_id = _acc_id()
    order_no = "TEST-PAYWIN-NEW-001"
    far = datetime.now() + timedelta(minutes=30)
    rec = upsert_order_record(database.SessionLocal(), acc_id, order_no,
                              pay_deadline=far)
    assert rec.pay_deadline <= datetime.now() + WINDOW + timedelta(seconds=5)
    assert rec.pay_deadline >= datetime.now() + WINDOW - timedelta(seconds=5)


def test_upsert_clamps_renewed_deadline_to_created_at():
    acc_id = _acc_id()
    order_no = "TEST-PAYWIN-RENEW-001"
    with database.SessionLocal() as db:
        db.add(OrderRecord(account_id=acc_id, order_no=order_no, status=1,
                           created_at=datetime.now() - timedelta(minutes=8)))
        db.commit()
    renewed = datetime.now() + timedelta(minutes=30)   # continuePay 顺延值
    rec = upsert_order_record(database.SessionLocal(), acc_id, order_no,
                              pay_deadline=renewed)
    base = rec.created_at + WINDOW
    assert abs((rec.pay_deadline - base).total_seconds()) < 1


# ---------- ensure_pay_session 层 ----------

def _link(order_no: str, expire_in: float) -> bridge.PayLink:
    expire_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + expire_in))
    return bridge.PayLink(order_no=order_no, pay_no="PN-W", order_str="alipay_sdk=x&sign=y",
                          out_trade_no="OT-W", total_amount="10.00", expire_at=expire_at)


def test_ensure_pay_session_clamps_deadline():
    acc_id = _acc_id()
    order_no = "TEST-PAYWIN-SESS-001"
    with database.SessionLocal() as db:
        db.add(OrderRecord(account_id=acc_id, order_no=order_no, status=1,
                           total_amount="10", created_at=datetime.now()))
        db.commit()
    with database.SessionLocal() as db:
        sess = ensure_pay_session(db, acc_id, order_no, _link(order_no, expire_in=1800))
        assert sess.pay_deadline is not None
        remaining = (sess.pay_deadline - datetime.now()).total_seconds()
        # 30 分钟支付串被钳到官方窗：剩余 ≤ 10 分钟（允许 1s 抖动）
        assert remaining <= PAY_WINDOW_SECONDS + 1
        assert remaining > PAY_WINDOW_SECONDS - 30
