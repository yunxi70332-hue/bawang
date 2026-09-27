"""全局操作日志（oplog）：结构化业务操作记录，双进程共享，落独立 SQLite 库。

定位与分工（与既有三套留痕机制的边界）：
  - audit.AuditLog（app.db）   管理面「谁做了什么」的审计台账（RBAC 视角，只记管理动作）
  - CouponUsageLog/PayEventLog 券与支付会话的领域明细
  - 本模块 oplog               全局统一操作日志：覆盖订单全流程 + HTTP 请求 + 后台线程
                              心跳 + 系统启停，含级别(INFO/WARN/ERROR)、trace_id 链路
                              关联、异常细节；是排障与监控告警(log_monitor)的数据源

为什么独立库（data/logs/oplog.db）而非入 app.db：主 API(8000) 与收银台(8010) 双进程
+ 三个后台线程本就并发写 app.db（WAL 缓解），日志高频写入单独落库，与业务事务彻底
隔离；日志库损坏/膨胀不影响业务，保留期清理也只动日志库。

存储安全：WAL + busy_timeout=5000 + synchronous=NORMAL（短事务，进程并发安全）；
每条写入独立连接（量级为每秒个位数，无连接池必要）；写失败绝不抛出——降级为
标准 logger 输出并累加失败计数（log_monitor 据此告警「日志存储不可靠」）。

多进程标识：process 列区分来源（main-api / pay-portal / cli / test）。进程在启动时
调 init_oplog(process=...) 显式注册；未注册时按 CHAGEE_PROCESS_NAME 环境变量兜底。

测试隔离：CHAGEE_OPLOG_DB 环境变量可重定向日志库路径（离线测试指向 data/test_*.db，
避免污染生产日志库——与 database.DB_PATH 的重定向约定一致）。
"""

import contextvars
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime

logger = logging.getLogger("oplog")

# server/oplog.py 上两层 = account_system/（与 database.DATA_DIR 同级定位）
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_BASE, "data", "logs", "oplog.db")
OPLOG_DB_PATH = os.environ.get("CHAGEE_OPLOG_DB") or DEFAULT_DB_PATH

LEVELS = ("INFO", "WARN", "ERROR")

# 当前进程标识（init_oplog 注册；读时惰性兜底）
_process_name: str | None = None
_process_lock = threading.Lock()

# trace 链路上下文（请求中间件设置；后台线程无请求上下文时为 None）
_trace_id: contextvars.ContextVar = contextvars.ContextVar("oplog_trace_id", default=None)

# 写入失败计数（自进程上次读取后的增量；log_monitor 周期读取并告警）
write_failures = 0
_fail_lock = threading.Lock()

# 心跳限频（component → 上次落库时间戳）：watcher 2 秒一轮，不能轮轮落库
_heartbeat_last: dict[str, float] = {}
_HEARTBEAT_MIN_INTERVAL = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS op_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL,
    process     TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT '',
    params      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    trace_id    TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_op_log_ts ON op_log(ts);
CREATE INDEX IF NOT EXISTS idx_op_log_action ON op_log(action);
CREATE INDEX IF NOT EXISTS idx_op_log_level ON op_log(level);
CREATE INDEX IF NOT EXISTS idx_op_log_trace ON op_log(trace_id);
CREATE TABLE IF NOT EXISTS op_alert (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    rule         TEXT NOT NULL,
    level        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    notif_status TEXT NOT NULL DEFAULT 'pending',
    notif_error  TEXT NOT NULL DEFAULT '',
    acked        INTEGER NOT NULL DEFAULT 0,
    acked_by     TEXT NOT NULL DEFAULT '',
    acked_at     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_op_alert_rule ON op_alert(rule, acked, ts);
"""


# ---------------- 进程标识 / 初始化 ----------------

def set_process_name(name: str) -> None:
    global _process_name
    with _process_lock:
        _process_name = str(name)[:32]


def get_process_name() -> str:
    with _process_lock:
        if _process_name:
            return _process_name
    env = os.environ.get("CHAGEE_PROCESS_NAME", "")
    if env:
        return env[:32]
    return "main-api"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(OPLOG_DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_oplog(process: str | None = None) -> None:
    """建表（幂等）+ 注册进程名。startup 事件调用；首条写入也会惰性建表。"""
    global _process_name, _schema_ready
    if process:
        set_process_name(process)
    os.makedirs(os.path.dirname(OPLOG_DB_PATH), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _schema_ready = True


# 惰性建表标志：True 后 log_op 不再重复 executescript（CREATE IF NOT EXISTS 幂等，
# 但每条日志一次建表脚本仍是浪费；进程级缓存即可，多进程各建一次无冲突）
_schema_ready = False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _schema_ready
    conn.executescript(_SCHEMA)
    conn.commit()
    _schema_ready = True


# ---------------- 脱敏 ----------------

# 键名命中即整值替换为 ***（子串匹配，键统一小写、去下划线后比对）
_EXACT_SENSITIVE = {"sk", "session", "password", "passwd", "secret", "authorization",
                    "cookie", "paytoken", "accesskey", "sessionkey"}
_SUBSTR_SENSITIVE = ("token", "password", "secret", "authorization", "cookie")

_PARAMS_MAX_CHARS = 4000


def _key_is_sensitive(key: str) -> bool:
    k = str(key).lower().replace("_", "").replace("-", "")
    if k in _EXACT_SENSITIVE:
        return True
    return any(s in k for s in _SUBSTR_SENSITIVE)


def redact(value, depth: int = 0):
    """递归脱敏：敏感键的值打码；超长字符串截断。永不抛出（解析失败原样字符串化）。-
    depth 防御性上限：嵌套 >6 层直接字符串化（业务参数不会更深）。"""
    try:
        if depth > 6:
            return str(value)[:200]
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if _key_is_sensitive(k):
                    out[str(k)] = "***"
                else:
                    out[str(k)] = redact(v, depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            return [redact(v, depth + 1) for v in value[:50]]
        if isinstance(value, str):
            return value if len(value) <= 512 else value[:512] + "...(截断)"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:200]
    except Exception:
        return str(value)[:200]


def _dumps_params(params) -> str:
    if not params:
        return ""
    try:
        s = json.dumps(redact(params), ensure_ascii=False, default=str)
        return s[:_PARAMS_MAX_CHARS]
    except Exception:
        return str(params)[:_PARAMS_MAX_CHARS]


def _fmt_error(error) -> str:
    if error is None:
        return ""
    if isinstance(error, BaseException):
        msg = f"{type(error).__name__}: {error}"
        tail = ""
        tb = getattr(error, "__traceback__", None)
        if tb is not None:
            import traceback
            lines = traceback.format_exception(type(error), error, tb)
            tail = "".join(lines[-3:]).strip()[-800:]
            if tail:
                msg += "\n" + tail
        return msg[:1200]
    return str(error)[:1200]


# ---------------- trace 链路 ----------------

def new_trace_id() -> str:
    return datetime.now().strftime("%m%d%H%M%S") + "-" + secrets.token_hex(4)


def current_trace_id() -> str | None:
    return _trace_id.get()


def set_trace_id(trace_id: str):
    """设置当前上下文 trace_id（请求中间件调用），返回 reset token。"""
    return _trace_id.set(trace_id)


def reset_trace_id(token) -> None:
    _trace_id.reset(token)


# ---------------- 核心：写一条操作日志 ----------------

def log_op(action: str, *, level: str = "INFO", actor: str = "", target: str = "",
           result: str = "success", params=None, error=None, trace_id: str | None = None,
           duration_ms: int | None = None, process: str | None = None) -> bool:
    """写一条结构化操作日志。永不抛出：库写入失败降级为标准 logger 输出 + 失败计数。

    字段：action 操作类型（沿用审计 action 词表，如 feature.order_create /
    coupon.rollback / http.request / system.heartbeat）；actor 操作用户（登录名或
    伪操作者 system/pay-watcher/pay-portal）；target 操作对象（label#id / order_no）；
    result success/failed/rejected/pending...；params 关键参数（自动脱敏）；
    error 异常对象或字符串（自动带尾部栈）；trace_id 缺省取当前上下文。
    同时镜像一行到标准 logger（进入 server.log/payportal.log 文本日志）。
    """
    global write_failures
    level = level if level in LEVELS else "INFO"
    ts = datetime.now().isoformat(sep=" ", timespec="milliseconds")
    trace = trace_id or current_trace_id() or ""
    proc = process or get_process_name()
    params_json = _dumps_params(params)
    error_text = _fmt_error(error)
    ok = False
    try:
        if not os.path.exists(os.path.dirname(OPLOG_DB_PATH)):
            os.makedirs(os.path.dirname(OPLOG_DB_PATH), exist_ok=True)
        conn = _connect()
        try:
            if not _schema_ready:
                _ensure_schema(conn)   # 惰性建表：未走 startup init_oplog 的进程首写自愈
            conn.execute(
                "INSERT INTO op_log(ts, level, process, actor, action, target, result,"
                " params, error, trace_id, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ts, level, proc, str(actor or "")[:64], str(action or "")[:96],
                 str(target or "")[:128], str(result or "")[:32], params_json,
                 error_text, str(trace)[:32],
                 int(duration_ms) if duration_ms is not None else None),
            )
            conn.commit()
            ok = True
        finally:
            conn.close()
    except Exception as e:
        with _fail_lock:
            write_failures += 1
        logger.error("oplog 写入失败（降级为文本日志）action=%s: %s: %s",
                     action, type(e).__name__, e)
    # 镜像到标准文本日志（server.log / payportal.log）；ERROR 带异常栈便于全文检索
    mirror = logging.ERROR if level == "ERROR" else (
        logging.WARNING if level == "WARN" else logging.INFO)
    logger.log(mirror, "OP[%s] %s actor=%s target=%s result=%s %s%s",
               proc, action, actor or "-", target or "-", result or "-",
               params_json[:600],
               ("\nERROR-DETAIL: " + error_text.replace("\n", " | ")) if error_text else "")
    return ok


def drain_write_failures() -> int:
    """取走自上次以来的写入失败计数（log_monitor 周期调用，>0 即告警）。"""
    global write_failures
    with _fail_lock:
        n = write_failures
        write_failures = 0
    return n


# ---------------- 心跳（后台线程存活/进度信号） ----------------

def heartbeat(component: str, *, min_interval: float = _HEARTBEAT_MIN_INTERVAL,
              extra=None) -> None:
    """后台线程每轮调用；限频落库（默认每组件每 60 秒至多一条 system.heartbeat）。
    log_monitor 以心跳新鲜度判「线程活着但循环卡死」（线程死亡由线程名直接检测）。"""
    now = time.time()
    last = _heartbeat_last.get(component, 0.0)
    if now - last < min_interval:
        return
    _heartbeat_last[component] = now
    log_op("system.heartbeat", actor=component,
           params={"component": component, "pid": os.getpid(), **(extra or {})})


def last_heartbeat_age_seconds(component: str) -> float | None:
    """该组件最近一次心跳距现在的秒数；从未心跳返回 None。含跨进程可见性说明：
    读的是共享日志库中该组件最新一条 system.heartbeat（不限本进程）。"""
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT ts FROM op_log WHERE action='system.heartbeat'"
                " AND actor=? ORDER BY id DESC LIMIT 1", (component,)).fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        t = datetime.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S")
        return max(0.0, (datetime.now() - t).total_seconds())
    except Exception:
        return None


# ---------------- 查询（routers/logs.py 使用） ----------------

def query_logs(*, level: str = "", action: str = "", actor: str = "", target: str = "",
               trace_id: str = "", q: str = "", start: str = "", end: str = "",
               page: int = 1, page_size: int = 20) -> dict:
    """多条件组合查询 + 分页 + 级别统计。ts 为 ISO 字符串，可直接字典序比较
    （start/end 支持 '2026-09-27' / '2026-09-27 15:00' 等前缀形式）。"""
    where, args = [], []
    if level:
        where.append("level=?"); args.append(level)
    if action:
        where.append("action LIKE ?"); args.append(action + "%")
    if actor:
        where.append("actor=?"); args.append(actor)
    if target:
        where.append("target=?"); args.append(target)
    if trace_id:
        where.append("trace_id=?"); args.append(trace_id)
    if start:
        where.append("ts>=?"); args.append(start)
    if end:
        where.append("ts<=?"); args.append(end)
    if q:
        like = f"%{q}%"
        where.append("(params LIKE ? OR error LIKE ? OR target LIKE ? OR actor LIKE ?"
                     " OR action LIKE ?)")
        args.extend([like, like, like, like, like])
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM op_log" + cond, args).fetchone()[0]
        stats_rows = conn.execute(
            "SELECT level, COUNT(*) FROM op_log" + cond + " GROUP BY level", args).fetchall()
        rows = conn.execute(
            "SELECT id, ts, level, process, actor, action, target, result, params,"
            " error, trace_id, duration_ms FROM op_log" + cond +
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [page_size, (page - 1) * page_size]).fetchall()
    finally:
        conn.close()
    cols = ("id", "ts", "level", "process", "actor", "action", "target", "result",
            "params", "error", "trace_id", "duration_ms")
    return {
        "total": total,
        "stats": {lv: n for lv, n in stats_rows},
        "items": [dict(zip(cols, r)) for r in rows],
    }


def get_trace(trace_id: str) -> list[dict]:
    """取一条 trace 链路的全部日志（正序）：一次手动下单从请求进入到引擎调用的全链。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, ts, level, process, actor, action, target, result, params,"
            " error, trace_id, duration_ms FROM op_log WHERE trace_id=? ORDER BY id",
            (trace_id,)).fetchall()
    finally:
        conn.close()
    cols = ("id", "ts", "level", "process", "actor", "action", "target", "result",
            "params", "error", "trace_id", "duration_ms")
    return [dict(zip(cols, r)) for r in rows]


# ---------------- HTTP 请求链路中间件（双入口共用） ----------------

# 噪声路径前缀：静态资源/健康检查/文档不记录
_QUIET_PREFIXES = ("/static/", "/api/health", "/docs", "/openapi", "/redoc")


def install_request_middleware(app) -> None:
    """给 FastAPI 应用挂请求 trace + 访问日志中间件。

    记录策略（防刷屏）：GET 且 2xx 的查询（含 H5 收银台秒级 /status 轮询）不落库；
    非幂等请求（POST/PUT/DELETE）、一切 4xx/5xx 必落库（WARN/ERROR 级）。
    每个请求生成 trace_id 并回写 X-Trace-Id 响应头，业务日志自动携带同一 trace_id。"""

    @app.middleware("http")
    async def _trace_and_log(request, call_next):
        trace = new_trace_id()
        token = set_trace_id(trace)
        started = time.time()
        try:
            response = await call_next(request)
        except Exception:
            # 未被路由层捕获的异常（中间件/框架层）：必须留痕后原样上抛
            log_op("http.request", level="ERROR", actor="-",
                   target=str(request.url.path)[:128], result="exception",
                   params={"method": request.method},
                   error="unhandled middleware exception",
                   trace_id=trace, duration_ms=int((time.time() - started) * 1000))
            reset_trace_id(token)
            raise
        response.headers["X-Trace-Id"] = trace
        path = request.url.path
        quiet = any(path.startswith(p) for p in _QUIET_PREFIXES)
        status = response.status_code
        if not quiet and (request.method != "GET" or status >= 400):
            level = "ERROR" if status >= 500 else ("WARN" if status >= 400 else "INFO")
            log_op("http.request", level=level, actor="-",
                   target=path[:128],
                   result="ok" if status < 400 else f"http_{status}",
                   params={"method": request.method, "status": status},
                   trace_id=trace, duration_ms=int((time.time() - started) * 1000))
        reset_trace_id(token)
        return response
