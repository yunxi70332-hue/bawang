"""审计日志辅助：所有变更/登录/协议外向动作统一落库。"""

import json

from fastapi import Request
from sqlalchemy.orm import Session

from models import AuditLog
from security import client_ip


def log_audit(db: Session, request: Request | None, user, action: str, target: str = "", detail=None):
    entry = AuditLog(
        user_id=user.id if user else 0,
        username=user.username if user else "-",
        action=action,
        target=target[:128],
        detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else "",
        ip=client_ip(request) if request else "",
    )
    db.add(entry)
    db.commit()
