# -*- coding: utf-8 -*-
"""cashier_mint 双 provider（protocol/frida/auto）分流离线测试。

不触网：_mint_via_protocol 通过注入 fake alipay_msp_client 模块驱动；
fill_back / mint_failed 事件落临时库断言。运行（须单独跑）：
  CHAGEE_MINT_ENABLED=0 python -m pytest test_mint_provider_offline.py -q
"""
import os
import sys
import types

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MINT_ENABLED"] = "0"
os.environ["CHAGEE_PAY_BROADCAST_ENABLED"] = "0"   # 离线禁发跨进程广播（mark_session 会触发）
os.environ["CHAGEE_OPLOG_DB"] = os.path.join(BASE, "..", "data", "test_mint_oplog.db")
os.environ["CHAGEE_LOG_DIR"] = os.path.join(BASE, "..", "data", "test_logs")

import database  # noqa: E402

database.DB_PATH = os.path.join(database.DATA_DIR, "test_mint_provider.db")
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

from models import ChageeAccount, PayEventLog, PaySession  # noqa: E402
import services.cashier_mint as cm  # noqa: E402


def _acc_id() -> int:
    with database.SessionLocal() as db:
        acc = db.query(ChageeAccount).filter(ChageeAccount.phone == "13800009999").first()
        if not acc:
            acc = ChageeAccount(label="铸造离线", phone="13800009999",
                                device_uuid="uuid-offline-mint-1", token="t", sk="s")
            db.add(acc)
            db.commit()
        return acc.id


def _mk_session(order_no: str, order_str: str = "alipay_sdk=x&sign=y") -> None:
    with database.SessionLocal() as db:
        sess = PaySession(account_id=_acc_id(), order_no=order_no,
                          pay_token="tok_" + order_no,
                          status="issued", order_str=order_str, total_amount="10")
        db.add(sess)
        db.commit()


def _events(order_no: str):
    with database.SessionLocal() as db:
        return db.query(PayEventLog).filter(PayEventLog.order_no == order_no).all()


def test_provider_env_default_protocol(monkeypatch):
    monkeypatch.delenv("CHAGEE_MINT_PROVIDER", raising=False)
    assert cm._provider() == "protocol"
    for val, expect in (("frida", "frida"), ("auto", "auto"), ("PROTOCOL", "protocol"),
                        ("bogus", "protocol"), ("", "protocol")):
        monkeypatch.setenv("CHAGEE_MINT_PROVIDER", val)
        assert cm._provider() == expect


def test_mint_via_protocol_success(monkeypatch):
    fake = types.ModuleType("alipay_msp_client")
    fake.mint_cashier_link = lambda order_str, probe=True: {
        "ok": True, "url": "https://mclient.alipay.com/cashierRoutePay.htm?session=S1&cc=y"}
    monkeypatch.setitem(sys.modules, "alipay_msp_client", fake)
    order_no = "TEST-MINT-OK-001"
    _mk_session(order_no)
    url = cm._mint_via_protocol(order_no, "alipay_sdk=x")
    assert url and url.startswith("https://mclient.alipay.com/cashierRoutePay.htm")
    # fill_back 回填 + 事件
    cm._fill_back(order_no, url, "protocol-mint")
    with database.SessionLocal() as db:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).one()
        assert sess.alipay_cashier_url == url
    evs = _events(order_no)
    assert any(e.event == "cashier_updated" and e.payload.get("source") == "protocol-mint"
               for e in evs)


def test_mint_via_protocol_failure_records_event(monkeypatch):
    fake = types.ModuleType("alipay_msp_client")
    fake.mint_cashier_link = lambda order_str, probe=True: {"ok": False, "error": "Result-Status 2000"}
    monkeypatch.setitem(sys.modules, "alipay_msp_client", fake)
    order_no = "TEST-MINT-FAIL-001"
    _mk_session(order_no)
    assert cm._mint_via_protocol(order_no, "alipay_sdk=x") is None
    evs = _events(order_no)
    assert any(e.event == "mint_failed" and e.payload.get("source") == "protocol-mint"
               for e in evs)


def test_fill_back_skips_non_issued():
    order_no = "TEST-MINT-SKIP-001"
    _mk_session(order_no)
    with database.SessionLocal() as db:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).one()
        sess.status = "paid"
        db.commit()
    cm._fill_back(order_no, "https://mclient.alipay.com/cashierRoutePay.htm?session=S2", "protocol-mint")
    with database.SessionLocal() as db:
        sess = db.query(PaySession).filter(PaySession.order_no == order_no).one()
        assert not sess.alipay_cashier_url


def test_trigger_disabled_short_circuit():
    assert cm.trigger_mint(None, "ANY") == {"skipped": "disabled"}
