"""SQLite 一致性快照备份线程（2026-09-29，异步订单中枢交付项）。

现状（此前）：无任何自动备份——官方口径「备份即复制 app.db 文件」，全仓仅一份手工
备份 app.db.bak_rate_coupon_20260928。本模块补齐：
  - sqlite3 backup API 做在线一致性快照（WAL 模式下直接复制文件会拿到不含 -wal 的
    陈旧视图，backup API 走页级拷贝才是正确姿势）；
  - 覆盖 data/app.db（业务）与 data/logs/oplog.db（操作日志/告警）；
  - 保留期自动清理（CHAGEE_BACKUP_RETENTION_DAYS，默认 14 天）。

线程惯例：daemon + heartbeat("backup") + 单轮异常不退出（log_monitor heartbeat_stale
据此监控）。开关：CHAGEE_BACKUP_INTERVAL_SECONDS（默认 86400 每日；<=0 不启动——
离线测试约定置 0）。手动触发：run_backup_once()（测试/运维直调）。
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from oplog import heartbeat, log_op

logger = logging.getLogger(__name__)

# services/x.py 上三层 = account_system/
_ACCOUNT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(_ACCOUNT_ROOT, "data")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

# 备份对象：业务库 + 操作日志库（相对 DATA_DIR 的路径）
BACKUP_TARGETS = ("app.db", os.path.join("logs", "oplog.db"))

INTERVAL_SECONDS = float(os.environ.get("CHAGEE_BACKUP_INTERVAL_SECONDS", "86400") or 0)
RETENTION_DAYS = int(os.environ.get("CHAGEE_BACKUP_RETENTION_DAYS", "14") or 14)


def _backup_one(src_path: str, dst_path: str) -> None:
    """单库在线一致性快照：sqlite3 backup API（源库繁忙时逐页拷贝仍一致）。"""
    src = sqlite3.connect(src_path, timeout=30)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _prune_expired(now: datetime) -> int:
    """删除超保留期的备份文件（按文件名里的日期判定，回退 mtime）。"""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    removed = 0
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(path):
            continue
        expired = False
        try:
            # 命名约定：<base>-YYYYMMDD-HHMM.db / <base>-YYYYMMDD.db
            stamp = name.rsplit("-", 1)[-1].split(".")[0]
            if len(stamp) == 8 and stamp.isdigit():
                expired = datetime.strptime(stamp, "%Y%m%d") < cutoff
            else:
                expired = datetime.fromtimestamp(os.path.getmtime(path)) < cutoff
        except ValueError:
            expired = datetime.fromtimestamp(os.path.getmtime(path)) < cutoff
        if expired:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def run_backup_once(reason: str = "scheduled") -> dict:
    """执行一轮备份（全部目标库 + 保留期清理）。幂等安全，可并发调用（文件名含时分）。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    now = datetime.now()
    done: list[str] = []
    for rel in BACKUP_TARGETS:
        src = os.path.join(DATA_DIR, rel)
        if not os.path.isfile(src):
            continue
        base = os.path.basename(rel)
        dst = os.path.join(BACKUP_DIR, f"{base[:-3]}-{now.strftime('%Y%m%d-%H%M')}.db")
        try:
            _backup_one(src, dst)
            done.append(os.path.relpath(dst, DATA_DIR))
        except Exception as e:
            logger.error("备份失败 %s: %s", rel, e, exc_info=True)
            log_op(level="ERROR", action="system.backup", target=rel, result="failed",
                   error=e, params={"reason": reason})
    removed = _prune_expired(now)
    if done:
        log_op(action="system.backup", target=",".join(done),
               params={"reason": reason, "pruned": removed,
                       "retention_days": RETENTION_DAYS})
    return {"backed_up": done, "pruned": removed}


_started = False


def _backup_loop(interval: float) -> None:
    while True:
        time.sleep(interval)
        heartbeat("backup")
        try:
            run_backup_once()
        except Exception as e:
            logger.exception("备份轮次异常，继续下一轮")
            log_op("system.thread_error", level="ERROR",
                   params={"thread": "backup"}, error=e)


def start_backup_thread() -> None:
    """幂等启动 daemon 备份线程（间隔 CHAGEE_BACKUP_INTERVAL_SECONDS，默认 86400s；
    <=0 不启动，供离线测试彻底关闭）。"""
    global _started
    if _started:
        return
    if INTERVAL_SECONDS <= 0:
        logger.info("备份线程未启用（CHAGEE_BACKUP_INTERVAL_SECONDS<=0）")
        return
    _started = True
    threading.Thread(target=_backup_loop, args=(INTERVAL_SECONDS,),
                     daemon=True, name="backup").start()
    logger.info("SQLite 备份线程已启动（%.0fs 间隔，保留 %d 天，目录 data/backups/）",
                INTERVAL_SECONDS, RETENTION_DAYS)
