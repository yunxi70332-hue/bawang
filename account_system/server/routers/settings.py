"""系统设置路由（2026-09-29）：代理出口配置 / 状态 / 诊断 / 切换日志。

权限：settings:manage（系统管理组；admin 种子自动获得，seed 每次启动重置内置角色）。
PUT 保存后立即执行一个检测周期（拿最新状态回显）；POST test 为干跑诊断——
对草稿配置全链路实测（提取→CONNECT 隧道→经代理访问茶姬网关→直连基线），不改不落状态。
"""

from fastapi import APIRouter, Body, Depends, Request
from fastapi import HTTPException
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import SystemUser
from oplog import query_logs
from security import require_perm
from services import net_proxy

router = APIRouter(prefix="/api/ops/settings", tags=["settings"])


def _merge_saved_password(cfg_in: dict) -> dict:
    """前端回显的 password 为 *** 时表示未修改：保留原值，避免把掩码写库。"""
    if cfg_in.get("password") == "***":
        cfg_in = {**cfg_in, "password": net_proxy._cfg.get("password") or ""}
    return cfg_in


@router.get("/proxy")
def get_proxy(_: SystemUser = Depends(require_perm("settings:manage"))):
    return {"config": net_proxy.masked_config(), "status": net_proxy.status_snapshot()}


@router.put("/proxy")
def save_proxy(payload: dict = Body(...), request: Request = None,
               db: Session = Depends(get_db),
               user: SystemUser = Depends(require_perm("settings:manage"))):
    try:
        cfg = net_proxy.save_config(_merge_saved_password(payload), actor=user.username)
    except ValueError as e:
        raise HTTPException(422, str(e))
    log_audit(db, request, user, "settings.proxy_save",
              detail={"enabled": cfg["enabled"], "mode": cfg["mode"], "protocol": cfg["protocol"]})
    status = net_proxy.refresh_now()   # 保存即生效：立即检测一轮并回显最新状态
    return {"config": net_proxy.masked_config(), "status": status}


@router.post("/proxy/test")
def test_proxy(payload: dict = Body(default=None),
               _: SystemUser = Depends(require_perm("settings:manage"))):
    return net_proxy.run_dry_test(_merge_saved_password(payload or {}))


@router.post("/proxy/refresh")
def refresh_proxy(_: SystemUser = Depends(require_perm("settings:manage"))):
    return net_proxy.refresh_now()


@router.get("/proxy/logs")
def proxy_logs(page: int = 1, page_size: int = 20,
               _: SystemUser = Depends(require_perm("settings:manage"))):
    return query_logs(action="proxy.", page=page, page_size=min(page_size, 100))
