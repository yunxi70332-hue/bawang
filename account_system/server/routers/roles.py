"""角色与权限矩阵管理路由（role:manage）。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import Role, SystemUser
from permissions import ADMIN_ALL, PERMISSION_CODES, permission_tree
from schemas import RoleCreate, RoleUpdate
from security import require_perm

router = APIRouter(prefix="/api/roles", tags=["roles"])


@router.get("")
def list_roles(db: Session = Depends(get_db), _: SystemUser = Depends(require_perm("role:manage"))):
    roles = db.query(Role).order_by(Role.id).all()
    return {
        "roles": [
            {
                "id": r.id, "name": r.name, "description": r.description,
                "permissions": r.permissions, "is_builtin": r.is_builtin,
                "user_count": len(r.users),
                "created_at": r.created_at,
            }
            for r in roles
        ],
        "permission_tree": permission_tree(),
    }


def _validate_permissions(perms: list[str]):
    unknown = [p for p in perms if p not in PERMISSION_CODES]
    if unknown:
        raise HTTPException(400, f"未知权限点: {unknown}")


@router.post("")
def create_role(body: RoleCreate, request: Request,
                db: Session = Depends(get_db), operator: SystemUser = Depends(require_perm("role:manage"))):
    if body.name in ("admin",):
        raise HTTPException(400, "admin 为内置角色名")
    if db.query(Role).filter(Role.name == body.name).first():
        raise HTTPException(400, "角色名已存在")
    _validate_permissions(body.permissions)
    role = Role(name=body.name, description=body.description, permissions=body.permissions)
    db.add(role)
    db.commit()
    db.refresh(role)
    log_audit(db, request, operator, "role.create", role.name, {"permissions": body.permissions})
    return {"id": role.id}


@router.put("/{role_id}")
def update_role(role_id: int, body: RoleUpdate, request: Request,
                db: Session = Depends(get_db), operator: SystemUser = Depends(require_perm("role:manage"))):
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(404, "角色不存在")
    if role.name == "admin":
        # admin 权限全量锁定，仅允许改描述
        perms = list(ADMIN_ALL)
    else:
        perms = body.permissions
        _validate_permissions(perms)
    role.description = body.description
    role.permissions = perms
    db.commit()
    log_audit(db, request, operator, "role.update", role.name, {"permissions": perms})
    return {"ok": True}


@router.delete("/{role_id}")
def delete_role(role_id: int, request: Request,
                db: Session = Depends(get_db), operator: SystemUser = Depends(require_perm("role:manage"))):
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(404, "角色不存在")
    if role.is_builtin:
        raise HTTPException(400, "内置角色（admin/operator/viewer）不可删除")
    if role.users:
        raise HTTPException(400, f"仍有 {len(role.users)} 个用户使用该角色，请先调整用户角色")
    name = role.name
    db.delete(role)
    db.commit()
    log_audit(db, request, operator, "role.delete", name)
    return {"ok": True}
