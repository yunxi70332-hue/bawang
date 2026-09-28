"""SSE 实时事件流端点（2026-09-28）：GET /api/events?topics=...&token=<JWT>。

EventSource 无法携带 Authorization 头，鉴权走 query token（security.user_from_token_str）。
topic → 权限点映射与同域 REST 端点同口径：
  - dashboard  ← GET /api/dashboard/stats 的 account:read
  - pickup_scan ← POST /api/ops/pickup/scan-all 的 feature:pickup

帧格式与 payportal 一致（event: <type>\ndata: <json>\n\n）；15s 无事件发 ": ping" 心跳保连。
dashboard 订阅连上即推一帧缓存 stats（services/dashboard_push.latest_stats），
其后仅在指纹变化时收到新帧——订阅端无需再发首次 REST 拉取。
"""

import queue

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from security import user_from_token_str
from services import dashboard_push, events_bus

router = APIRouter(prefix="/api", tags=["events"])

_TOPIC_PERMS = {
    "dashboard": "account:read",        # 仪表盘统计实时推送
    "pickup_scan": "feature:pickup",    # 全量取餐码扫描进度推流
}

_HEARTBEAT_SECONDS = 15.0


@router.get("/events")
def events_stream(topics: str = Query(..., description="逗号分隔的订阅主题"),
                  token: str = Query("", description="JWT（EventSource 无法带 Authorization 头）"),
                  db: Session = Depends(get_db)):
    user = user_from_token_str(token, db)
    if not user:
        raise HTTPException(401, "未登录或令牌无效")
    owned = set(user.role.permissions or []) if user.role else set()
    requested = [t.strip() for t in topics.split(",") if t.strip()]
    topic_list = [t for t in requested if t in _TOPIC_PERMS]
    if not topic_list:
        raise HTTPException(400, f"无有效 topic（支持：{', '.join(_TOPIC_PERMS)}）")
    denied = [t for t in topic_list if _TOPIC_PERMS[t] not in owned]
    if denied:
        raise HTTPException(403, "权限不足：" + ", ".join(f"{t} 需 {_TOPIC_PERMS[t]}" for t in denied))

    q = events_bus.subscribe(topic_list)

    def stream():
        try:
            if "dashboard" in topic_list:
                cached = dashboard_push.latest_stats()
                if cached:
                    yield events_bus.sse_frame("stats", cached)
            while True:
                try:
                    yield q.get(timeout=_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            events_bus.unsubscribe(q, topic_list)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
