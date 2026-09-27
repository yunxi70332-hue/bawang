"""系统用户管理路由（user:manage）。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import Role, SystemUser
from schemas import ResetPasswordRequest, UserCreate, UserOut, UserUpdate
from security import hash_password, require_perm

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: SystemUser = Depends(require_perm("user:manage"))):
    return db.query(SystemUser).order_by(SystemUser.id).all()


@router.post("", response_model=UserOut)
def create_user(body: UserCreate, request: Request,
                db: Session = Depends(get_db), _: SystemUser = Depends(require_perm("user:manage"))):
    if db.query(SystemUser).filter(SystemUser.username == body.username).first():
        raise HTTPException(400, "用户名已存在")
    role = db.get(Role, body.role_id)
    if not role:
        raise HTTPException(400, "角色不存在")
    user = SystemUser(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.username,
        role_id=role.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_audit(db, request, _, "user.create", body.username, {"role": role.name})
    return user


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdate, request: Request,
                db: Session = Depends(get_db), operator: SystemUser = Depends(require_perm("user:manage"))):
    user = db.get(SystemUser, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    role = db.get(Role, body.role_id)
    if not role:
        raise HTTPException(400, "角色不存在")
    if user.id == operator.id and (not body.is_active or role.name != "admin"):
        raise HTTPException(400, "不能降级或停用自己")
    user.display_name = body.display_name
    user.role_id = role.id
    user.is_active = body.is_active
    db.commit()
    db.refresh(user)
    log_audit(db, request, operator, "user.update", user.username, {"role": role.name, "active": body.is_active})
    return user


@router.delete("/{user_id}")
def delete_user(user_id: int, request: Request,
                db: Session = Depends(get_db), operator: SystemUser = Depends(require_perm("user:manage"))):
    user = db.get(SystemUser, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.username == "admin":
        raise HTTPException(400, "内置管理员不可删除")
    if user.id == operator.id:
        raise HTTPException(400, "不能删除自己")
    name = user.username
    db.delete(user)
    db.commit()
    log_audit(db, request, operator, "user.delete", name)
    return {"ok": True}


@router.post("/{user_id}/reset-password")
def reset_password(user_id: int, body: ResetPasswordRequest, request: Request,
                   db: Session = Depends(get_db), operator: SystemUser = Depends(require_perm("user:manage"))):
    user = db.get(SystemUser, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    log_audit(db, request, operator, "user.reset_password", user.username)
    return {"ok": True}
