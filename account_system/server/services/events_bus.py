"""进程内 SSE 事件总线：主 API 的实时推送基建（2026-09-28）。

消费方：/api/events 端点（routers/events.py）——每个连接一个订阅队列，阻塞取帧；
发布方：后台线程与请求线程（全量取餐码扫描进度、仪表盘统计指纹推送）。

帧格式与 H5 收银台 SSE（routers/payportal.py）同构：event: <type>\ndata: <单行JSON>\n\n。
背压策略：订阅队列满（消费过慢）时丢弃最旧帧保最新——订阅方始终可回落 REST 端点拉全量，
慢消费者不能拖垮发布方；本总线只做单进程内闭环，跨进程联动沿用 pay_broadcast 机制。
"""

import json
import logging
import queue
import threading

logger = logging.getLogger(__name__)

_QUEUE_MAX = 200   # 每订阅者待发帧上限（约容纳一次全量扫描的全部逐账号事件）

_lock = threading.Lock()
_subscribers: dict[str, list[queue.Queue]] = {}   # topic -> 订阅队列列表


def sse_frame(event: str, payload: dict) -> str:
    """构造一帧 SSE 文本（data 序列化为单行 JSON；datetime 等类型经 default=str 兜底）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def subscribe(topics) -> "queue.Queue[str]":
    """订阅一组 topic，返回本订阅者的帧队列（同一队列可收多 topic 的帧）。"""
    q: "queue.Queue[str]" = queue.Queue(maxsize=_QUEUE_MAX)
    with _lock:
        for t in topics:
            _subscribers.setdefault(t, []).append(q)
    return q


def unsubscribe(q: "queue.Queue[str]", topics) -> None:
    """连接断开时注销（幂等：未注册的队列忽略）。"""
    with _lock:
        for t in topics:
            qs = _subscribers.get(t)
            if qs and q in qs:
                qs.remove(q)
                if not qs:
                    _subscribers.pop(t, None)


def publish(topic: str, event: str, payload: dict) -> int:
    """向 topic 的全部订阅者投递一帧；返回成功投递数（无人订阅返回 0）。"""
    frame = sse_frame(event, payload)
    with _lock:
        queues = list(_subscribers.get(topic, ()))
    delivered = 0
    for q in queues:
        try:
            q.put_nowait(frame)
            delivered += 1
        except queue.Full:
            try:
                q.get_nowait()   # 丢最旧保最新
                q.put_nowait(frame)
                delivered += 1
            except (queue.Empty, queue.Full):
                logger.warning("events_bus: 订阅队列满且置换失败，丢弃 topic=%s event=%s", topic, event)
    return delivered
