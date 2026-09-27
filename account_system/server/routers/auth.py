"""认证路由：系统用户登录 / 当前信息 / 修改密码。"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import SystemUser
from schemas import ChangePasswordRequest, LoginRequest, LoginResponse, UserOut
from security import create_token, get_current_user, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(SystemUser).filter(SystemUser.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        log_audit(db, request, user, "auth.login_failed", body.username)
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已停用，请联系管理员")
    user.last_login_at = datetime.now()
    db.commit()
    token = create_token(user)
    perms = list(user.role.permissions or []) if user.role else []
    log_audit(db, request, user, "auth.login", "", {"role": user.role.name if user.role else ""})
    return LoginResponse(token=token, user=UserOut.model_validate(user), permissions=perms)


@router.get("/me", response_model=UserOut)
def me(user: SystemUser = Depends(get_current_user)):
    return user


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: SystemUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from security import hash_password
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    log_audit(db, request, user, "auth.change_password")
    return {"ok": True}
