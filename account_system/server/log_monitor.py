"""日志监控告警：周期扫描 oplog + 线程存活 + 心跳新鲜度，异常即落告警表并外发通知。

四条规则（触发即创建 op_alert 告警，去重靠「同规则未确认且在冷却期内则跳过」）：
  1. error_burst   最近窗口内 ERROR 级操作日志 ≥ 阈值（默认 5 分钟 3 条）——业务异常洪峰
  2. thread_dead   预期应存活的后台线程（order-reconcile / menu-refresh / pay-watcher，
                   以各自 INTERVAL 环境变量 >0 判定「预期启动」）不在本进程线程列表——关键流程中断
  3. heartbeat_stale 组件心跳超时（线程活着但循环停滞，如卡死在无超时的外呼上）——
                   距最近一条 system.heartbeat 超过 staleness 阈值
  4. oplog_failure oplog 自身写入失败计数 >0——日志存储不可靠（监控自身的基础设施告警）

通知渠道（data/alert_config.json，全部可选、零新增依赖；未配置时告警仅落库+控制台
ERROR 输出，仍可在 /api/ops/alerts 查询处理）：
  {"webhook": {"url": "http://...", "secret": "可选-HMAC签名密钥"},
   "smtp":    {"host": "...", "port": 465, "ssl": true, "user": "", "password": "",
               "from": "", "to": ["a@b.c"]}}

启动：app.py startup 调 start_log_monitor_thread()（daemon，间隔
CHAGEE_LOG_MONITOR_INTERVAL_SECONDS 默认 30 秒，<=0 不启动）。只在主 API 进程运行
（8010 收银台进程不起，避免双进程重复告警）。保留期清理：每轮顺带删除超期
op_log（CHAGEE_OPLOG_RETENTION_DAYS 默认 30 天）与 op_alert（默认 90 天）。
"""

import hashlib
import hmac
import json
import logging
import os
import smtplib
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from oplog import (
    OPLOG_DB_PATH, _connect, drain_write_failures, get_process_name,
    init_oplog, last_heartbeat_age_seconds, log_op,
)

logger = logging.getLogger("log_monitor")

# 服务自检根（config 与 oplog.DATA_DIR 同级定位）
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_CONFIG_PATH = os.path.join(_BASE, "data", "alert_config.json")

# 直连 opener：回调目标多为内网地址，绕过系统代理（与 payment_events 同策略）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 规则参数（小流量系统取保守默认；需要调优时改这里的常量即可）
ERROR_WINDOW_SECONDS = 300     # error_burst 统计窗口
ERROR_THRESHOLD = 3            # 窗口内 ERROR 条数阈值
ALERT_COOLDOWN_SECONDS = 900   # 同规则告警冷却（15 分钟内不重复创建）
HEARTBEAT_STALE_SECONDS = 600  # 心跳超时阈值（心跳写入本身限频 60s，阈值给足裕量）

# 关键后台线程：名称 → 间隔环境变量（变量 >0 才「预期启动」；读env在扫描时进行，
# 测试进程置 0 后 monitor 不会对缺失线程误报）
WATCHED_THREADS = {
    "order-reconcile": "CHAGEE_RECONCILE_INTERVAL_SECONDS",
    "menu-refresh": "CHAGEE_MENU_REFRESH_INTERVAL_SECONDS",
    "pay-watcher": "CHAGEE_PAYWATCH_INTERVAL_SECONDS",
}

_thread_started = False


# ---------------- 告警表读写 ----------------

def _create_alert(rule: str, level: str, summary: str, detail) -> bool:
    """插入告警（含冷却去重）并尝试外发通知；返回是否新建。

    去重口径：同 rule+summary 的最近一条未确认告警仍在冷却期（ALERT_COOLDOWN_SECONDS）
    内 → 跳过；已确认、或冷却已过（持续异常应能再次提醒值守者）→ 允许新建。"""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    detail_json = json.dumps(detail, ensure_ascii=False, default=str)[:4000]
    dedupe_key = hashlib.sha1((rule + "|" + summary).encode("utf-8")).hexdigest()[:16]
    cooldown_cutoff = (datetime.now() - timedelta(seconds=ALERT_COOLDOWN_SECONDS)).isoformat(
        sep=" ", timespec="seconds")
    try:
        conn = _connect()
        try:
            dup = conn.execute(
                "SELECT id FROM op_alert WHERE rule=? AND acked=0 AND ts>=?"
                " AND id IN (SELECT id FROM op_alert WHERE rule=? AND acked=0"
                "             ORDER BY id DESC LIMIT 1)"
                " AND detail LIKE ?",
                (rule, cooldown_cutoff, rule, f"%{dedupe_key}%")).fetchone()
            if dup:
                return False
            cur = conn.execute(
                "INSERT INTO op_alert(ts, rule, level, summary, detail, notif_status)"
                " VALUES(?,?,?,?,?,'pending')",
                (now, rule, level, summary, detail_json + f'|"dedupe_key={dedupe_key}"'))
            alert_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("告警落库失败 rule=%s: %s: %s", rule, type(e).__name__, e)
        return False
    # 告警必须让值守者在控制台立刻看到（无论外发渠道是否配置）
    logger.error("【告警】[%s] %s", rule, summary)
    status, err = _dispatch_notification(rule, level, summary, detail)
    try:
        conn = _connect()
        try:
            conn.execute("UPDATE op_alert SET notif_status=?, notif_error=? WHERE id=?",
                         (status, err[:500], alert_id))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return True


def _load_alert_config() -> dict:
    try:
        with open(ALERT_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _dispatch_notification(rule: str, level: str, summary: str, detail) -> tuple[str, str]:
    """按配置外发；返回 (notif_status, notif_error)。任一渠道成功即 sent。"""
    cfg = _load_alert_config()
    body = json.dumps({
        "type": "chagee_op_alert", "rule": rule, "level": level,
        "summary": summary, "detail": detail,
        "ts": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "process": get_process_name(),
    }, ensure_ascii=False, default=str)
    sent, last_err = False, ""
    wh = cfg.get("webhook") or {}
    if isinstance(wh, dict) and wh.get("url"):
        try:
            req = urllib.request.Request(
                wh["url"], data=body.encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
            secret = str(wh.get("secret") or "")
            if secret:
                sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"),
                               hashlib.sha256).hexdigest()
                req.add_header("X-Signature", sig)
            with _OPENER.open(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    sent = True
        except Exception as e:
            last_err = f"webhook: {type(e).__name__}: {e}"
    smtp = cfg.get("smtp") or {}
    if isinstance(smtp, dict) and smtp.get("host") and smtp.get("to"):
        try:
            msg = MIMEText(f"[霸王茶姬系统告警][{level}] {rule}\n\n{summary}\n\n{body}",
                           "plain", "utf-8")
            msg["Subject"] = f"[茶姬系统告警][{level}] {summary[:60]}"
            msg["From"] = smtp.get("from") or smtp.get("user") or ""
            msg["To"] = ", ".join(smtp["to"])
            if smtp.get("ssl", True):
                srv = smtplib.SMTP_SSL(smtp["host"], int(smtp.get("port", 465)), timeout=10)
            else:
                srv = smtplib.SMTP(smtp["host"], int(smtp.get("port", 25)), timeout=10)
            try:
                if smtp.get("user"):
                    srv.login(smtp["user"], smtp.get("password") or "")
                srv.sendmail(msg["From"], smtp["to"], msg.as_string())
            finally:
                srv.quit()
            sent = True
        except Exception as e:
            last_err = (last_err + "; " if last_err else "") + f"smtp: {type(e).__name__}: {e}"
    if sent:
        return "sent", ""
    if not (cfg.get("webhook") or cfg.get("smtp")):
        return "skipped", "未配置通知渠道（data/alert_config.json）"
    return "failed", last_err


def ack_alert(alert_id: int, username: str) -> bool:
    """确认告警（管理端 API 调用）。"""
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE op_alert SET acked=1, acked_by=?, acked_at=? WHERE id=? AND acked=0",
                (username, datetime.now().isoformat(sep=" ", timespec="seconds"), alert_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


def list_alerts(*, only_open: bool = False, page: int = 1, page_size: int = 20) -> dict:
    conn = _connect()
    try:
        cond = " WHERE acked=0" if only_open else ""
        total = conn.execute("SELECT COUNT(*) FROM op_alert" + cond).fetchone()[0]
        open_cnt = conn.execute("SELECT COUNT(*) FROM op_alert WHERE acked=0").fetchone()[0]
        rows = conn.execute(
            "SELECT id, ts, rule, level, summary, detail, notif_status, notif_error,"
            " acked, acked_by, acked_at FROM op_alert" + cond +
            " ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, (page - 1) * page_size)).fetchall()
    finally:
        conn.close()
    cols = ("id", "ts", "rule", "level", "summary", "detail", "notif_status",
            "notif_error", "acked", "acked_by", "acked_at")
    return {"total": total, "open": open_cnt,
            "items": [dict(zip(cols, r)) for r in rows]}


# ---------------- 扫描（一轮完整规则评估；测试与手动触发直调） ----------------

def _count_recent_errors(window_seconds: int) -> int:
    cutoff = (datetime.now() - timedelta(seconds=window_seconds)).isoformat(
        sep=" ", timespec="seconds")
    try:
        conn = _connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM op_log WHERE level='ERROR' AND ts>=?",
                (cutoff,)).fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return 0


def _expected_threads() -> dict[str, bool]:
    """名称 → 本进程是否预期存在该线程（间隔 env >0）。"""
    out = {}
    for name, env in WATCHED_THREADS.items():
        try:
            interval = float(os.environ.get(env, "1"))
        except ValueError:
            interval = 1.0
        out[name] = interval > 0
    return out


def run_monitor_scan() -> dict:
    """执行一轮完整扫描（幂等，可直接调用）。返回扫描摘要（触发了几条告警等）。"""
    init_oplog()
    created: list[str] = []
    scanned = {"errors_in_window": 0, "threads": {}, "heartbeats": {}}

    # 规则1：ERROR 洪峰
    errs = _count_recent_errors(ERROR_WINDOW_SECONDS)
    scanned["errors_in_window"] = errs
    if errs >= ERROR_THRESHOLD:
        _create_alert(
            "error_burst", "ERROR",
            f"最近 {ERROR_WINDOW_SECONDS // 60} 分钟内出现 {errs} 条 ERROR 级操作日志（≥{ERROR_THRESHOLD}）",
            {"count": errs, "window_seconds": ERROR_WINDOW_SECONDS})

    # 规则2：关键线程死亡（关键流程中断）
    alive = {t.name for t in threading.enumerate()}
    for name, expected in _expected_threads().items():
        scanned["threads"][name] = {"expected": expected, "alive": name in alive}
        if expected and name not in alive:
            _create_alert("thread_dead", "ERROR", f"关键后台线程 {name} 不在运行（关键流程中断）",
                          {"thread": name})

    # 规则3：心跳停滞（线程活着但循环无进展）
    for name, expected in _expected_threads().items():
        if not expected or name not in alive:
            continue   # 线程已死由规则2覆盖；未预期启动的组件不检查
        age = last_heartbeat_age_seconds(name)
        scanned["heartbeats"][name] = age
        if age is not None and age > HEARTBEAT_STALE_SECONDS:
            _create_alert("heartbeat_stale", "WARN",
                          f"组件 {name} 心跳已停滞 {int(age)} 秒（线程存活但循环疑似卡死）",
                          {"component": name, "age_seconds": int(age)})

    # 规则4：oplog 自身写入失败（日志存储可靠性）
    fails = drain_write_failures()
    scanned["oplog_write_failures"] = fails
    if fails > 0:
        _create_alert("oplog_failure", "ERROR",
                      f"操作日志库写入失败 {fails} 次（日志存储不可靠，检查磁盘/权限: {OPLOG_DB_PATH}）",
                      {"failures": fails, "db": OPLOG_DB_PATH})

    pruned = _prune_retention()
    return {"created_alerts": created, "pruned": pruned, "scanned": scanned}


def _prune_retention() -> int:
    """按保留期清理超期日志（op_log 默认 30 天、op_alert 默认 90 天，环境变量可调）。"""
    try:
        days = int(os.environ.get("CHAGEE_OPLOG_RETENTION_DAYS", "30"))
    except ValueError:
        days = 30
    if days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
    try:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM op_log WHERE ts<?", (cutoff,))
            conn.execute("DELETE FROM op_alert WHERE ts<? AND acked=1",
                         ((datetime.now() - timedelta(days=90)).isoformat(
                             sep=" ", timespec="seconds"),))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception:
        return 0


# ---------------- 后台监控线程（仅主 API 进程） ----------------

def _monitor_loop(interval: float) -> None:
    while True:
        time.sleep(interval)
        try:
            run_monitor_scan()
        except Exception:
            logger.exception("日志监控轮次异常，继续下一轮")


def start_log_monitor_thread() -> None:
    """幂等启动 daemon 监控线程（name="log-monitor"）。间隔环境变量
    CHAGEE_LOG_MONITOR_INTERVAL_SECONDS（默认 30 秒；<=0 不启动——离线测试用）。"""
    global _thread_started
    if _thread_started:
        return
    try:
        interval = float(os.environ.get("CHAGEE_LOG_MONITOR_INTERVAL_SECONDS", "30"))
    except ValueError:
        interval = 30.0
    if interval <= 0:
        return
    _thread_started = True
    threading.Thread(target=_monitor_loop, args=(interval,),
                     daemon=True, name="log-monitor").start()
    log_op("system.monitor_start", params={"interval_seconds": interval})
