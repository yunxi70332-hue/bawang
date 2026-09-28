"""仪表盘统计 SSE 推送线程（2026-09-28）：周期重算 stats，指纹变化才推。

与 GET /api/dashboard/stats 同源（services/dashboard_stats.collect_dashboard_stats）：
REST 供首次拉取与 SSE 不可用时的兜底轮询，本线程保证「数据一变、订阅端即收」。
指纹剔除 generated_at（时间戳恒变），其余字段 canonical JSON 比对。
连上即推：latest_stats() 缓存最近一次结果（无论是否推送过），供 /api/events
对 dashboard 新订阅者立即补发一帧（对齐收银台 SSE「连上即推 sync」契约）。

线程开关：CHAGEE_DASHBOARD_PUSH_INTERVAL_SECONDS（默认 5；0 停用——离线测试约定）。
"""

import json
import logging
import os
import threading
import time

from database import SessionLocal
from services import events_bus
from services.dashboard_stats import collect_dashboard_stats

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = float(os.environ.get("CHAGEE_DASHBOARD_PUSH_INTERVAL_SECONDS", "5") or 0)

_latest_lock = threading.Lock()
_latest: dict | None = None   # 最近一次重算的 stats（供 SSE 订阅连上即推）


def latest_stats() -> dict | None:
    with _latest_lock:
        return _latest


def _fingerprint(stats: dict) -> str:
    """载荷指纹：剔除 generated_at（恒变的时间戳）后规范化序列化。"""
    payload = {k: v for k, v in stats.items() if k != "generated_at"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def push_once() -> bool:
    """重算一次并按需推送；返回是否发生推送（离线测试直接调用本函数断言）。"""
    global _latest
    with SessionLocal() as db:
        stats = collect_dashboard_stats(db)
    fp = _fingerprint(stats)
    with _latest_lock:
        prev = _latest
        if prev is not None and fp == _fingerprint(prev):
            _latest = stats   # 缓存刷新（generated_at 更新），但不推送
            return False
        _latest = stats
    events_bus.publish("dashboard", "stats", stats)
    return True


def start_dashboard_push_thread() -> None:
    if INTERVAL_SECONDS <= 0:
        logger.info("仪表盘统计推送线程未启用（CHAGEE_DASHBOARD_PUSH_INTERVAL_SECONDS=0）")
        return

    def _loop() -> None:
        while True:
            time.sleep(INTERVAL_SECONDS)
            try:
                push_once()
            except Exception:
                logger.warning("仪表盘统计重算失败（下轮重试）", exc_info=True)

    threading.Thread(target=_loop, daemon=True, name="dashboard-push").start()
    logger.info("仪表盘统计推送线程已启动（%.0fs 指纹比对，变化才 SSE 推送）", INTERVAL_SECONDS)
