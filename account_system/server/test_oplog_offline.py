"""全局操作日志系统（oplog / log_monitor / routers/logs）离线测试（零网络、零生产文件写入）。

运行：
    cd C:\\baidunetdiskdownload\\霸王茶姬\\account_system\\server && ..\\..\\.venv_verify\\Scripts\\python.exe test_oplog_offline.py
    （或 pytest test_oplog_offline.py -q；须单独跑，不与其它套件合跑——engine 互踩是既定约定）

覆盖（编号即用例名前缀，main() 按字典序执行）：
  01 log_op 全字段写入 + query_logs 回读（level/process/actor/action/target/result/
     params/trace_id/duration_ms 全往返）
  02 params 自动脱敏：token/sk/password/嵌套 dict.token → ***；普通键原样保留
  03 error 异常对象格式化：ValueError("boom") → error 列含 "ValueError: boom"
  04 query_logs 组合过滤：level / action 前缀 / actor / target / 关键词 q / start/end 时间界
  05 trace 链路：同 trace_id 两条 → get_trace 按 id 正序返回 2 行
  06 heartbeat 限频（60 秒内同组件第二条不落库）+ last_heartbeat_age_seconds 新鲜度
  07 请求中间件：GET 200 不落库 / POST 200 落 INFO(method=POST) / GET 500 落 ERROR
     (result=http_500) / 响应头 X-Trace-Id / 路由内 log_op 自动携带同 trace_id
  08 error_burst 规则（窗口内 ERROR≥3 建告警、无通知配置 notif_status=skipped）+
     冷却去重（15 分钟内同规则同摘要不重复建）+ 冷却过期后可再建（回改 ts 验证）
  09 thread_dead 规则：INTERVAL>0 预期启动但线程不在 → order-reconcile 告警
  10 oplog_failure 规则：write_failures 计数 >0 → 告警且计数被 drain 清零
  11 /api/ops 端点（mini app 只挂 logs 路由）：alerts scope=all/open、ack（user:manage）、
     logs level 过滤、trace 链路、未知 trace 404、无 audit:read 403、无 token 401、手动 scan
  12 保留期清理：默认 30 天（回改历史 ts 的行被删）；CHAGEE_OPLOG_RETENTION_DAYS<=0 关闭清理
  13 惰性建表：未经 init_oplog 的进程首条 log_op 自动建表成功（空库首写不丢）

要点（与 test_reconcile_offline.py 同模式）：
  - 四个 CHAGEE_*_INTERVAL_SECONDS=0：禁三后台线程 + 日志监控线程（本套件直调 run_monitor_scan）
  - CHAGEE_OPLOG_DB / CHAGEE_LOG_DIR 指向测试路径：不污染生产 data/logs/oplog.db 与 server.log
  - 先把 database.DB_PATH 指向 data/test_oplog_app.db 并重建 engine，再 import seed/routers
  - mini app 不挂 /api/auth/login：鉴权用 security.create_token 直签 JWT
  - log_monitor.ALERT_CONFIG_PATH 指向不存在文件：_dispatch_notification 必返 skipped（零外发）
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))                 # .../account_system/server
ROOT = os.path.dirname(os.path.dirname(BASE))                     # 项目根
sys.path.insert(0, BASE)

# 禁三后台线程 + 监控线程（必须在 import app 系模块之前；本套件直调 run_monitor_scan）
os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_MENU_REFRESH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_PAYWATCH_INTERVAL_SECONDS"] = "0"
os.environ["CHAGEE_LOG_MONITOR_INTERVAL_SECONDS"] = "0"

# ---------- 1. 日志库隔离 + 先改库路径，再 import 任何 server 模块 ----------
import database  # noqa: E402

OPLOG_TEST_DB = os.path.join(database.DATA_DIR, "test_oplog.db")
for _suffix in ("", "-journal", "-wal", "-shm"):
    _p = OPLOG_TEST_DB + _suffix
    if os.path.exists(_p):
        os.remove(_p)
os.environ["CHAGEE_OPLOG_DB"] = OPLOG_TEST_DB
os.environ["CHAGEE_LOG_DIR"] = os.path.join(database.DATA_DIR, "test_logs")

database.DB_PATH = os.path.join(database.DATA_DIR, "test_oplog_app.db")
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
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import oplog  # noqa: E402
import log_monitor  # noqa: E402
from models import Role, SystemUser  # noqa: E402
from routers import logs as logs_router  # noqa: E402
from security import create_token, hash_password  # noqa: E402

# 通知配置指向不存在的文件：告警外发必然 skipped（且杜绝真实 webhook/smtp 网络调用）
log_monitor.ALERT_CONFIG_PATH = os.path.join(database.DATA_DIR, "test_oplog_alert_cfg_absent.json")
if os.path.exists(log_monitor.ALERT_CONFIG_PATH):
    os.remove(log_monitor.ALERT_CONFIG_PATH)

oplog.init_oplog(process="oplog-offline-test")

# ---------- 2. 测试数据（RBAC：admin 全权限 / viewer_op 无 audit:read） ----------
seed.init_db()
with database.SessionLocal() as db:
    if not db.query(SystemUser).filter(SystemUser.username == "viewer_op").first():
        viewer_role = db.query(Role).filter(Role.name == "viewer").one()
        db.add(SystemUser(username="viewer_op", display_name="日志只读",
                          password_hash=hash_password("Viewer@123"), role_id=viewer_role.id))
        db.commit()

# mini app 只挂 /api/ops 日志路由（不 import 整个 app：无需 FakeClient，零网络面）
MINI = FastAPI()
MINI.include_router(logs_router.router)
CLIENT = TestClient(MINI)


def _token(username: str) -> str:
    """直签 JWT（mini app 未挂 /api/auth/login，与登录等价：同一 JWT_SECRET/载荷）。"""
    with database.SessionLocal() as db:
        user = db.query(SystemUser).filter(SystemUser.username == username).one()
        return create_token(user)


def _auth(username: str) -> dict:
    return {"Authorization": f"Bearer {_token(username)}"}


# ---------- 3. 直连日志库的小工具（绕过 API 层核对/造数） ----------

def _exec(sql: str, args=()) -> None:
    conn = sqlite3.connect(oplog.OPLOG_DB_PATH, timeout=5)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _fetch_alerts(rule: str = "") -> list[dict]:
    sql = ("SELECT id, ts, rule, level, summary, notif_status, acked, acked_by"
           " FROM op_alert" + (" WHERE rule=?" if rule else ""))
    conn = sqlite3.connect(oplog.OPLOG_DB_PATH, timeout=5)
    try:
        rows = conn.execute(sql, (rule,) if rule else ()).fetchall()
    finally:
        conn.close()
    cols = ("id", "ts", "rule", "level", "summary", "notif_status", "acked", "acked_by")
    return [dict(zip(cols, r)) for r in rows]


# ---------- 4. 测试用例 ----------

def test_01_log_op_roundtrip():
    """全字段写入 + 回读：level/process/actor/action/target/result/params/trace/duration。"""
    ok = oplog.log_op("unit.case1", level="WARN", actor="alice", target="T#1",
                      result="failed", params={"k": "v", "n": 1, "ok": True},
                      trace_id="tr-case-1", duration_ms=123, process="op-test")
    assert ok is True, "log_op 应返回 True（落库成功）"
    d = oplog.query_logs(action="unit.case1")
    assert d["total"] == 1, f"应查到 1 条，实得 {d['total']}"
    it = d["items"][0]
    assert it["level"] == "WARN", it
    assert it["process"] == "op-test", it
    assert it["actor"] == "alice" and it["action"] == "unit.case1"
    assert it["target"] == "T#1" and it["result"] == "failed"
    assert json.loads(it["params"]) == {"k": "v", "n": 1, "ok": True}, it["params"]
    assert it["trace_id"] == "tr-case-1", it
    assert it["duration_ms"] == 123, it
    assert it["error"] == ""


def test_02_params_redaction():
    """敏感键打码：token/sk/password（含嵌套 dict）→ ***；普通键原样保留。"""
    oplog.log_op("unit.redact", params={
        "token": "tok-secret", "sk": "sk-secret", "password": "pw-secret",
        "keep": "plain-value", "nested": {"token": "inner-secret", "note": "ok"}})
    it = oplog.query_logs(action="unit.redact")["items"][0]
    p = json.loads(it["params"])
    assert p["token"] == "***" and p["sk"] == "***" and p["password"] == "***", p
    assert p["keep"] == "plain-value", p
    assert p["nested"]["token"] == "***" and p["nested"]["note"] == "ok", p


def test_03_error_format():
    """异常对象格式化：error 列以 "ValueError: boom" 开头（类型名: 消息）。"""
    try:
        raise ValueError("boom")
    except ValueError as e:
        oplog.log_op("unit.error", level="ERROR", error=e)
    it = oplog.query_logs(action="unit.error")["items"][0]
    assert it["error"].startswith("ValueError: boom"), it["error"]


def test_04_query_filters():
    """组合过滤：level / action 前缀 / actor / target / 关键词 q / start/end 时间界。"""
    oplog.log_op("probe.alpha", level="INFO", actor="alice", params={"note": "zebra-unique"})
    oplog.log_op("probe.beta", level="WARN", actor="bob", params={"note": "plain"})
    oplog.log_op("probe.gamma", level="ERROR", actor="alice", target="GT-1")

    d = oplog.query_logs(action="probe.", level="ERROR")
    assert d["total"] == 1 and d["items"][0]["action"] == "probe.gamma", d
    d = oplog.query_logs(action="probe.", actor="bob")
    assert d["total"] == 1 and d["items"][0]["action"] == "probe.beta", d
    d = oplog.query_logs(action="probe.", target="GT-1")
    assert d["total"] == 1 and d["items"][0]["action"] == "probe.gamma", d
    d = oplog.query_logs(action="probe.", q="zebra-unique")
    assert d["total"] == 1 and d["items"][0]["action"] == "probe.alpha", d
    # ts 为 ISO 字符串可直接字典序比较：start/end 支持日期前缀
    assert oplog.query_logs(action="probe.", start="2999-01-01")["total"] == 0, "未来起点应查不到"
    assert oplog.query_logs(action="probe.", end="2000-01-01")["total"] == 0, "过去终点应查不到"
    assert oplog.query_logs(action="probe.", start="2000-01-01")["total"] == 3, "全时间界应 3 条"


def test_05_trace_chain():
    """trace 链路：同 trace_id 两条日志 → get_trace 按 id 正序返回 2 行。"""
    tid = "chain-42"
    assert oplog.log_op("unit.chain.a", trace_id=tid) is True
    assert oplog.log_op("unit.chain.b", level="WARN", trace_id=tid) is True
    rows = oplog.get_trace(tid)
    assert len(rows) == 2, f"链路应 2 行，实得 {len(rows)}"
    assert [r["action"] for r in rows] == ["unit.chain.a", "unit.chain.b"], rows
    assert rows[0]["id"] < rows[1]["id"], "应按 id 正序"
    assert all(r["trace_id"] == tid for r in rows), rows


def test_06_heartbeat_ratelimit():
    """心跳限频：首跳落库，60 秒内第二跳不落库；心跳年龄新鲜（<5s）；从未心跳 → None。"""
    oplog.heartbeat("unit-test-comp")
    d1 = oplog.query_logs(action="system.heartbeat", actor="unit-test-comp")
    assert d1["total"] == 1, f"首跳应落库 1 条，实得 {d1['total']}"
    oplog.heartbeat("unit-test-comp")            # 立即第二跳：限频拦截
    d2 = oplog.query_logs(action="system.heartbeat", actor="unit-test-comp")
    assert d2["total"] == 1, f"限频失败：60 秒内重复落库（{d2['total']} 条）"
    age = oplog.last_heartbeat_age_seconds("unit-test-comp")
    assert age is not None and age < 5, f"心跳年龄应 <5s，实得 {age}"
    assert oplog.last_heartbeat_age_seconds("never-heartbeat-comp") is None, "从未心跳应返回 None"


def test_07_request_middleware():
    """中间件：GET 200 不落库；POST 200 落 INFO(method=POST)；GET 5xx 落 ERROR(http_500)；
    响应头 X-Trace-Id；路由内 log_op 自动携带同 trace_id（get_trace 串联验证）。"""
    mw = FastAPI()
    oplog.install_request_middleware(mw)

    @mw.get("/ok")
    def _ok():
        return {"ok": True}

    @mw.post("/do")
    def _do():
        oplog.log_op("unit.inner", params={"m": "POST"})
        return {"ok": True}

    @mw.get("/boom")
    def _boom():
        raise HTTPException(status_code=500, detail="boom")

    with TestClient(mw) as c:
        # ① GET 200：静音（不落库），但响应头必须带 trace
        r = c.get("/ok")
        assert r.status_code == 200
        t_ok = r.headers.get("x-trace-id")
        assert t_ok, "响应头缺 X-Trace-Id"
        assert oplog.query_logs(action="http.request", trace_id=t_ok)["total"] == 0, \
            "GET 200 不应落 http.request"

        # ② POST 200：INFO 一条；路由内业务日志携带同 trace
        r = c.post("/do")
        assert r.status_code == 200
        t_post = r.headers.get("x-trace-id")
        assert t_post and t_post != t_ok, "每请求独立 trace_id"
        rows = oplog.get_trace(t_post)
        assert [x["action"] for x in rows] == ["unit.inner", "http.request"], rows
        assert rows[0]["trace_id"] == t_post and rows[1]["trace_id"] == t_post, rows
        assert rows[1]["level"] == "INFO", rows
        assert json.loads(rows[1]["params"])["method"] == "POST", rows[1]["params"]

        # ③ GET 500：ERROR 一条 result=http_500
        r = c.get("/boom")
        assert r.status_code == 500
        t_boom = r.headers.get("x-trace-id")
        rows = oplog.get_trace(t_boom)
        assert len(rows) == 1, rows
        assert rows[0]["level"] == "ERROR" and rows[0]["result"] == "http_500", rows[0]


def test_08_error_burst_and_cooldown():
    """error_burst：窗口内 ≥3 条 ERROR 建告警（无通知配置 → skipped）；冷却期内二扫不重复建；
    回改告警 ts 至 16 分钟前 → 冷却过期可再建（验证 ALERT_COOLDOWN_SECONDS 真实生效）。"""
    for i in range(3):
        oplog.log_op("x.burst", level="ERROR", actor="burst", error=f"burst-{i}")
    r1 = log_monitor.run_monitor_scan()
    assert r1["scanned"]["errors_in_window"] >= 3, r1
    rows = _fetch_alerts(rule="error_burst")
    assert len(rows) == 1, f"应建 1 条 error_burst 告警，实得 {len(rows)}"
    assert rows[0]["notif_status"] == "skipped", rows[0]      # 无 alert_config.json → 不外发
    assert rows[0]["level"] == "ERROR", rows[0]

    log_monitor.run_monitor_scan()                             # 立即二扫：冷却期内去重
    assert len(_fetch_alerts(rule="error_burst")) == 1, "冷却期内重复建告警（去重失效）"

    old_ts = (datetime.now() - timedelta(seconds=log_monitor.ALERT_COOLDOWN_SECONDS + 60)) \
        .isoformat(sep=" ", timespec="seconds")
    _exec("UPDATE op_alert SET ts=? WHERE id=?", (old_ts, rows[0]["id"]))
    log_monitor.run_monitor_scan()                             # 冷却已过 → 允许再建
    assert len(_fetch_alerts(rule="error_burst")) == 2, "冷却过期后应允许再建告警"


def test_09_thread_dead():
    """thread_dead：RECONCILE INTERVAL>0（预期启动）但线程不在 → order-reconcile 告警。"""
    os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "60"
    try:
        r = log_monitor.run_monitor_scan()
        th = r["scanned"]["threads"].get("order-reconcile")
        assert th == {"expected": True, "alive": False}, r["scanned"]["threads"]
        rows = _fetch_alerts(rule="thread_dead")
        assert len(rows) == 1, f"应建 1 条 thread_dead 告警，实得 {len(rows)}"
        assert "order-reconcile" in rows[0]["summary"], rows[0]
    finally:
        os.environ["CHAGEE_RECONCILE_INTERVAL_SECONDS"] = "0"
    # 复位后再扫：预期关闭 → 不再新增
    log_monitor.run_monitor_scan()
    assert len(_fetch_alerts(rule="thread_dead")) == 1, "复位 env 后不应重复建 thread_dead"


def test_10_oplog_failure():
    """oplog_failure：write_failures=2 → ERROR 告警且计数被 drain 清零。"""
    oplog.write_failures = 2                    # 模拟两次写入失败（模块级计数器）
    r = log_monitor.run_monitor_scan()
    assert r["scanned"]["oplog_write_failures"] == 2, r
    assert oplog.write_failures == 0, "drain 后计数应清零"
    rows = _fetch_alerts(rule="oplog_failure")
    assert len(rows) == 1, f"应建 1 条 oplog_failure 告警，实得 {len(rows)}"
    assert rows[0]["level"] == "ERROR" and "2 次" in rows[0]["summary"], rows[0]


def test_11_ops_api():
    """/api/ops 端点：alerts 列表/ack/scope 过滤 + logs 过滤查询 + trace 链路/404 + 权限门控。"""
    h_admin = _auth("admin")
    # ① scope=all：本套件此前用例产生的告警全部可见
    r = CLIENT.get("/api/ops/alerts", params={"scope": "all"}, headers=h_admin)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    rules = {a["rule"] for a in items}
    assert {"error_burst", "thread_dead", "oplog_failure"} <= rules, rules
    # ② ack（user:manage）：确认最新一条 error_burst → acked=1 / acked_by=admin
    target = next(a for a in items if a["rule"] == "error_burst")
    r = CLIENT.post(f"/api/ops/alerts/{target['id']}/ack", headers=h_admin)
    assert r.status_code == 200 and r.json()["acked_by"] == "admin", r.text
    hit = [x for x in _fetch_alerts(rule="error_burst") if x["id"] == target["id"]][0]
    assert hit["acked"] == 1 and hit["acked_by"] == "admin", hit
    # ③ scope=open 不再包含已确认项
    r = CLIENT.get("/api/ops/alerts", params={"scope": "open"}, headers=h_admin)
    assert r.status_code == 200, r.text
    open_ids = [a["id"] for a in r.json()["items"]]
    assert target["id"] not in open_ids, "已确认告警不应出现在 scope=open"
    # ④ logs 过滤查询：level=ERROR + 级别统计
    r = CLIENT.get("/api/ops/logs", params={"level": "ERROR", "page": 1}, headers=h_admin)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] >= 1, d
    assert d["stats"]["ERROR"] == d["total"], d
    assert all(it["level"] == "ERROR" for it in d["items"]), d["items"]
    # ⑤ trace 端点：链路 2 行；未知 trace 404
    r = CLIENT.get("/api/ops/logs/trace/chain-42", headers=h_admin)
    assert r.status_code == 200 and len(r.json()["items"]) == 2, r.text
    r = CLIENT.get("/api/ops/logs/trace/no-such-trace", headers=h_admin)
    assert r.status_code == 404, r.text
    # ⑥ 权限门控：viewer（无 audit:read）403；无 token 401；viewer 也不能 ack（user:manage）
    h_viewer = _auth("viewer_op")
    r = CLIENT.get("/api/ops/logs", headers=h_viewer)
    assert r.status_code == 403, f"viewer 应 403，实得 {r.status_code}"
    assert "audit:read" in r.json()["detail"], r.text
    assert CLIENT.get("/api/ops/logs").status_code == 401, "无 token 应 401"
    r = CLIENT.post(f"/api/ops/alerts/{target['id']}/ack", headers=h_viewer)
    assert r.status_code == 403, f"viewer ack 应 403，实得 {r.status_code}"
    # ⑦ 手动触发扫描（user:manage）
    r = CLIENT.post("/api/ops/logs/scan", headers=h_admin)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text


def test_12_retention_prune():
    """保留期清理：默认 30 天，超期 op_log 被删（回改历史 ts 验证）；RETENTION<=0 关闭。"""
    os.environ.pop("CHAGEE_OPLOG_RETENTION_DAYS", None)        # 用默认 30 天
    assert oplog.log_op("unit.oldrow") is True
    row = oplog.query_logs(action="unit.oldrow")["items"][0]
    _exec("UPDATE op_log SET ts='2020-01-01 00:00:00' WHERE id=?", (row["id"],))
    r = log_monitor.run_monitor_scan()
    assert r["pruned"] >= 1, f"超期日志应被清理，实得 pruned={r['pruned']}"
    assert oplog.query_logs(action="unit.oldrow")["total"] == 0, "超期行应已删除"
    # days<=0：关闭清理（返回 0，即使有超期行）
    _exec("UPDATE op_log SET ts='2020-01-01 00:00:00'"
          " WHERE action='unit.case1'")
    os.environ["CHAGEE_OPLOG_RETENTION_DAYS"] = "0"
    try:
        assert log_monitor._prune_retention() == 0, "days<=0 应关闭清理"
        assert oplog.query_logs(action="unit.case1")["total"] == 1, "关闭清理时不应删行"
    finally:
        os.environ.pop("CHAGEE_OPLOG_RETENTION_DAYS", None)


def test_13_log_op_lazy_schema():
    """惰性建表：未经 init_oplog 的进程，首条 log_op 也应成功（文档承诺「首条写入也会惰性建表」）。"""
    fresh = os.path.join(database.DATA_DIR, "test_oplog_fresh.db")
    for _suffix in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(fresh + _suffix):
            os.remove(fresh + _suffix)
    old_path, old_ready = oplog.OPLOG_DB_PATH, oplog._schema_ready
    oplog.OPLOG_DB_PATH = fresh            # 指向全新空库：模拟未 init 的进程
    oplog._schema_ready = False
    try:
        assert oplog.log_op("unit.lazy") is True, "首条写入应惰性建表并成功"
        conn = sqlite3.connect(fresh, timeout=5)
        try:
            n = conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
        finally:
            conn.close()
        assert n == 1, f"惰性建表后应有 1 行，实得 {n}"
    finally:
        oplog.OPLOG_DB_PATH = old_path
        oplog._schema_ready = True         # 主测试库已 init_oplog 过
        for _suffix in ("", "-journal", "-wal", "-shm"):
            if os.path.exists(fresh + _suffix):
                os.remove(fresh + _suffix)


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
    # 清理测试库（Windows 下先释放连接池；oplog 库走独立 sqlite 连接，逐一删除）
    database.engine.dispose()
    for path in (database.DB_PATH, OPLOG_TEST_DB):
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
