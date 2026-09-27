"""操作日志与告警查询路由（/api/ops/logs、/api/ops/alerts）。

权限沿用既有目录：读取（日志/告警/trace）用 audit:read（与审计日志同一查看权限，
operator 角色已含）；告警确认/手动触发扫描用 user:manage（管理动作）。
数据源是 oplog 独立日志库（data/logs/oplog.db），与业务库无关。
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from log_monitor import ack_alert, list_alerts, run_monitor_scan
from models import SystemUser
from oplog import LEVELS, get_trace, init_oplog, query_logs
from security import require_perm

router = APIRouter(prefix="/api/ops", tags=["logs"])


@router.get("/logs")
def op_logs(
    level: str = Query("", pattern="^(INFO|WARN|ERROR)?$"),
    action: str = Query("", max_length=96, description="操作类型前缀（如 order / coupon / http）"),
    actor: str = Query("", max_length=64, description="操作用户（含伪操作者 system/pay-watcher/pay-portal）"),
    target: str = Query("", max_length=128),
    trace_id: str = Query("", max_length=32),
    q: str = Query("", max_length=64, description="关键词（参数/异常/对象模糊匹配）"),
    start: str = Query("", max_length=32, description="起始时间（2026-09-27 或 2026-09-27 15:00）"),
    end: str = Query("", max_length=32),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    user: SystemUser = Depends(require_perm("audit:read")),
):
    """全局操作日志分页查询：级别/操作类型/操作人/对象/trace/关键词/时间窗组合过滤，
    附当前筛选下的级别统计。字段说明见 docs/日志系统说明.md。"""
    init_oplog()
    if level and level not in LEVELS:
        raise HTTPException(400, f"level 仅支持 {'/'.join(LEVELS)}")
    return query_logs(level=level, action=action, actor=actor, target=target,
                      trace_id=trace_id, q=q, start=start, end=end,
                      page=page, page_size=page_size)


@router.get("/logs/trace/{trace_id}")
def op_trace(trace_id: str, user: SystemUser = Depends(require_perm("audit:read"))):
    """单条链路全量视图：按 trace_id 取该请求/操作从进入到结束的全部日志（正序）。"""
    init_oplog()
    items = get_trace(trace_id.strip()[:32])
    if not items:
        raise HTTPException(404, "该 trace_id 无日志记录")
    return {"trace_id": trace_id, "items": items}


@router.get("/alerts")
def op_alerts(
    scope: str = Query("open", pattern="^(open|all)$"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    user: SystemUser = Depends(require_perm("audit:read")),
):
    """告警列表（默认只看未确认 open）：含触发规则/级别/摘要/通知状态/确认信息。"""
    init_oplog()
    return list_alerts(only_open=(scope == "open"), page=page, page_size=page_size)


@router.post("/alerts/{alert_id}/ack")
def op_alert_ack(alert_id: int, user: SystemUser = Depends(require_perm("user:manage"))):
    """确认（处理完毕）一条告警：记录确认人与时间。"""
    init_oplog()
    if not ack_alert(alert_id, user.username):
        raise HTTPException(404, "告警不存在或已确认")
    return {"ok": True, "alert_id": alert_id, "acked_by": user.username}


@router.post("/logs/scan")
def op_logs_scan(user: SystemUser = Depends(require_perm("user:manage"))):
    """手动触发一轮日志监控扫描（不等 30 秒周期）：返回四条规则的评估摘要。"""
    result = run_monitor_scan()
    return {"ok": True, "scan": result}
