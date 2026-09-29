"""账号保活跃任务路由（2026-09-29，services/keepalive.py 的 HTTP 面）。

  GET  /api/ops/keepalive/status          状态快照（运行中/下次定时/最近一轮摘要）
  GET  /api/ops/keepalive/config          data/keepalive_config.json 原文
  PUT  /api/ops/keepalive/config          改配置（run_at/间歇/重试/告警阈值；30s 内热生效）
  GET  /api/ops/keepalive/runs            运行头分页
  GET  /api/ops/keepalive/runs/{id}       运行详情 + 逐请求明细分页
  POST /api/ops/keepalive/trigger         手动触发一轮（运行中 409；不受 enabled 限制）

权限：读 = account:read；配置/手动触发 = account:login（保活跃是生产外向动作，
与协议登录同级别）。运行日志另见 oplog action=keepalive.* 与告警 /api/ops/alerts。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import KeepaliveRecord, KeepaliveRun, SystemUser
from oplog import log_op
from schemas import KeepaliveConfigRequest
from security import require_perm
from services import keepalive as keepalive_svc

router = APIRouter(prefix="/api/ops/keepalive", tags=["keepalive"])


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _run_row(r: KeepaliveRun) -> dict:
    return {
        "id": r.id, "trigger": r.trigger, "status": r.status, "province": r.province,
        "city_total": r.city_total, "store_total": r.store_total,
        "stores_planned": r.stores_planned, "accounts_total": r.accounts_total,
        "accounts_expired": r.accounts_expired, "requests_total": r.requests_total,
        "requests_ok": r.requests_ok, "requests_failed": r.requests_failed,
        "avg_ms": r.avg_ms, "note": r.note,
        "started_at": _fmt(r.started_at), "finished_at": _fmt(r.finished_at),
    }


def _record_row(x: KeepaliveRecord) -> dict:
    return {
        "id": x.id, "run_id": x.run_id, "account_id": x.account_id,
        "account_label": x.account_label, "store_no": x.store_no,
        "store_name": x.store_name, "action": x.action, "attempt": x.attempt,
        "ok": bool(x.ok), "status": x.status, "ms": x.ms, "error": x.error,
        "created_at": _fmt(x.created_at),
    }


@router.get("/status")
def status(db: Session = Depends(get_db),
           _: SystemUser = Depends(require_perm("account:read"))):
    return keepalive_svc.snapshot_status(db)


@router.get("/config")
def config_get(_: SystemUser = Depends(require_perm("account:read"))):
    return keepalive_svc.load_config()


@router.put("/config")
def config_put(body: KeepaliveConfigRequest, request: Request,
               db: Session = Depends(get_db),
               user: SystemUser = Depends(require_perm("account:login"))):
    saved = keepalive_svc.save_config(body.model_dump())
    log_audit(db, request, user, "keepalive.config_put", "保活跃配置", dict(saved))
    log_op(action="keepalive.config_put", actor=user.username, target="保活跃配置",
           params=dict(saved))
    return saved


@router.get("/runs")
def runs_list(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
              db: Session = Depends(get_db),
              _: SystemUser = Depends(require_perm("account:read"))):
    q = db.query(KeepaliveRun)
    total = q.count()
    rows = (q.order_by(KeepaliveRun.id.desc())
              .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "items": [_run_row(r) for r in rows]}


@router.get("/runs/{run_id}")
def run_detail(run_id: int,
               records_page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200),
               result: str = Query("", pattern="^(ok|failed)?$"),
               db: Session = Depends(get_db),
               _: SystemUser = Depends(require_perm("account:read"))):
    run = db.get(KeepaliveRun, run_id)
    if run is None:
        raise HTTPException(404, "运行记录不存在")
    q = db.query(KeepaliveRecord).filter(KeepaliveRecord.run_id == run_id)
    if result == "ok":
        q = q.filter(KeepaliveRecord.ok == True)   # noqa: E712
    elif result == "failed":
        q = q.filter(KeepaliveRecord.ok == False)   # noqa: E712
    records_total = q.count()
    records = (q.order_by(KeepaliveRecord.id)
                 .offset((records_page - 1) * page_size).limit(page_size).all())
    return {"run": _run_row(run), "records_total": records_total,
            "records": [_record_row(x) for x in records]}


@router.post("/trigger")
def trigger(request: Request, db: Session = Depends(get_db),
            user: SystemUser = Depends(require_perm("account:login"))):
    ok, msg = keepalive_svc.trigger_manual()
    if not ok:
        raise HTTPException(409, msg)
    log_audit(db, request, user, "keepalive.trigger", "保活跃任务", {"trigger": "manual"})
    log_op(action="keepalive.trigger", actor=user.username, target="保活跃任务",
           params={"trigger": "manual"})
    return {"ok": True, "message": "已触发手动运行（后台执行，稍后查看运行记录）"}
