"""审计日志与仪表盘统计路由。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from models import AuditLog, SystemUser
from security import require_perm
from services.dashboard_stats import collect_dashboard_stats

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit")
def list_audit(
    keyword: str = Query("", max_length=64),
    action: str = Query("", max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: SystemUser = Depends(require_perm("audit:read")),
):
    q = db.query(AuditLog)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(AuditLog.username.like(like) | AuditLog.target.like(like) | AuditLog.action.like(like))
    if action:
        q = q.filter(AuditLog.action.like(f"{action}%"))
    total = q.count()
    rows = (q.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total,
        "items": [
            {"id": r.id, "username": r.username, "action": r.action, "target": r.target,
             "detail": r.detail, "ip": r.ip, "created_at": r.created_at}
            for r in rows
        ],
    }


@router.get("/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("account:read"))):
    """仪表盘统一数据源：账号域 / 券域 / 订单域 / 审计域实时聚合。
    聚合逻辑在 services/dashboard_stats.collect_dashboard_stats，与 SSE 推送线程
    （services/dashboard_push，变化才推）同源——本端点供首次拉取与兜底轮询。"""
    return collect_dashboard_stats(db)
