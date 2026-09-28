"""认证与授权：bcrypt 密码哈希、JWT 签发校验、RBAC 权限依赖。

JWT 密钥首次启动自动生成并持久化到 data/secret.key；令牌有效期 12 小时。
"""

import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database import DATA_DIR, get_db
from models import SystemUser

SECRET_FILE = os.path.join(DATA_DIR, "secret.key")
TOKEN_TTL_HOURS = 12
_bearer = HTTPBearer(auto_error=False)


def _load_secret() -> str:
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
            if s:
                return s
    s = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(s)
    return s


JWT_SECRET = _load_secret()
JWT_ALGO = "HS256"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


def create_token(user: SystemUser) -> str:
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role.name if user.role else "",
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> SystemUser:
    if cred is None:
        raise HTTPException(401, "未登录或令牌缺失")
    try:
        payload = jwt.decode(cred.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "无效令牌")
    user = db.get(SystemUser, int(payload.get("sub", 0)))
    if not user or not user.is_active:
        raise HTTPException(401, "用户不存在或已停用")
    return user


def user_from_token_str(token: str, db: Session) -> SystemUser | None:
    """从裸 JWT 字符串解析用户（SSE 端点用：EventSource 无法携带 Authorization 头，
    鉴权改走 query token）；无效/过期/用户不存在或停用一律返回 None，由调用方决定 401。"""
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.InvalidTokenError:
        return None
    user = db.get(SystemUser, int(payload.get("sub", 0) or 0))
    if not user or not user.is_active:
        return None
    return user


def require_perm(*perms: str):
    """RBAC 依赖工厂：当前用户角色须持有任意一个给定权限点。"""

    def checker(user: SystemUser = Depends(get_current_user)) -> SystemUser:
        owned = set(user.role.permissions or []) if user.role else set()
        missing = [p for p in perms if p not in owned]
        if missing:
            raise HTTPException(403, f"权限不足：需要 {', '.join(missing)}")
        return user

    return checker


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "")
