"""茶姬账号管理路由：CRUD + 协议登录流程（功能1）+ 会话检查。

协议登录流程（对应文档功能1「设备注册流程」）：
  1) create   —— 生成设备身份 uuid（协议层设备注册等价物）
  2) send-sms —— 生产真实投递短信验证码（60s 服务端限流；前端有二次确认）
  3) login    —— 提交验证码换取 608B token，DB 持久化，状态 → online
  4) check    —— whoami 自检；单会话语义下失效则状态 → expired
"""

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import ACCOUNT_STATUS, ChageeAccount, LoginTicket, STATUS_LABELS, SystemUser
from oplog import log_op
from schemas import AccountCreate, AccountOut, AccountUpdate, SmsCodeRequest, mask_phone
from security import require_perm
from services import chagee_bridge as bridge

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

TICKET_TTL_MINUTES = 10
SMS_INTERVAL_SECONDS = 65  # 服务端 60s 限流 + 5s 缓冲


def _serialize(account: ChageeAccount) -> AccountOut:
    token = account.token or ""
    return AccountOut(
        id=account.id,
        label=account.label,
        phone_masked=mask_phone(account.phone),
        phone_full=account.phone,
        device_uuid=account.device_uuid,
        customer_id=account.customer_id,
        nickname=account.nickname,
        status=account.status,
        status_label=STATUS_LABELS.get(account.status, account.status),
        group=account.group,
        note=account.note,
        token_fingerprint=(token[:16] + f"...len={len(token)}") if token else "",
        has_token=bool(token),
        last_login_at=account.last_login_at,
        last_check_at=account.last_check_at,
        created_by=account.created_by,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _get_account(db: Session, account_id: int) -> ChageeAccount:
    account = db.get(ChageeAccount, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    return account


@router.get("")
def list_accounts(
    keyword: str = Query("", max_length=64),
    status: str = Query(""),
    group: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: SystemUser = Depends(require_perm("account:read")),
):
    q = db.query(ChageeAccount)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(ChageeAccount.label.like(like) | ChageeAccount.phone.like(like)
                     | ChageeAccount.nickname.like(like) | ChageeAccount.note.like(like))
    if status:
        if status not in ACCOUNT_STATUS:
            raise HTTPException(400, f"非法状态筛选: {status}")
        q = q.filter(ChageeAccount.status == status)
    if group:
        q = q.filter(ChageeAccount.group == group)
    total = q.count()
    rows = (q.order_by(ChageeAccount.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total,
        "items": [_serialize(a) for a in rows],
        "groups": sorted({g for (g,) in db.query(ChageeAccount.group).distinct() if g}),
    }


@router.post("", response_model=AccountOut)
def create_account(body: AccountCreate, request: Request,
                   db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("account:create"))):
    if db.query(ChageeAccount).filter(ChageeAccount.phone == body.phone).first():
        raise HTTPException(400, "该手机号已存在账号记录")
    account = ChageeAccount(
        label=body.label, phone=body.phone, group=body.group or "默认", note=body.note,
        device_uuid=bridge.new_device_uuid(), status="pending", created_by=user.username,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    bridge._ensure_device_file(bridge.account_workdir(account.id), account.device_uuid)
    log_audit(db, request, user, "account.create", f"{account.label}#{account.id}",
              {"phone": mask_phone(account.phone), "uuid": account.device_uuid})
    return _serialize(account)


@router.get("/{account_id}", response_model=AccountOut)
def get_account(account_id: int, db: Session = Depends(get_db),
                user: SystemUser = Depends(require_perm("account:read"))):
    return _serialize(_get_account(db, account_id))


@router.get("/{account_id}/token")
def get_account_token(account_id: int, request: Request,
                      db: Session = Depends(get_db),
                      user: SystemUser = Depends(require_perm("account:read"))):
    """完整 token（列表页双击「Token 指纹」复制用）；复制行为写入审计日志。"""
    account = _get_account(db, account_id)
    if not account.token:
        raise HTTPException(404, "该账号当前没有 token（未登录或已失效）")
    fingerprint = account.token[:16] + f"...len={len(account.token)}"
    log_audit(db, request, user, "account.token_copy", f"{account.label}#{account.id}",
              {"fingerprint": fingerprint})
    return {"token": account.token, "fingerprint": fingerprint}


@router.put("/{account_id}", response_model=AccountOut)
def update_account(account_id: int, body: AccountUpdate, request: Request,
                   db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("account:update"))):
    account = _get_account(db, account_id)
    dup = db.query(ChageeAccount).filter(ChageeAccount.phone == body.phone, ChageeAccount.id != account_id).first()
    if dup:
        raise HTTPException(400, "该手机号已被其他账号使用")
    phone_changed = body.phone != account.phone
    account.label = body.label
    account.group = body.group or "默认"
    account.note = body.note
    account.phone = body.phone
    if body.status and body.status != account.status and account.status != "online":
        account.status = body.status
    if phone_changed and account.status == "online":
        # 换绑手机号意味着旧 token 归属存疑，强制回到待登录
        account.status, account.token = "pending", ""
    db.commit()
    db.refresh(account)
    log_audit(db, request, user, "account.update", f"{account.label}#{account.id}",
              {"phone_changed": phone_changed})
    return _serialize(account)


@router.delete("/{account_id}")
def delete_account(account_id: int, request: Request,
                   db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("account:delete"))):
    account = _get_account(db, account_id)
    label = f"{account.label}#{account.id}"
    if account.status == "online":
        raise HTTPException(400, "账号在线中，请先登出或停用后再删除")
    db.delete(account)
    db.query(LoginTicket).filter(LoginTicket.account_id == account_id).delete()
    db.commit()
    bridge.remove_workdir(account_id)
    log_audit(db, request, user, "account.delete", label)
    return {"ok": True}


# ---------------- 功能1：协议登录流程 ----------------

def _active_ticket(db: Session, account_id: int) -> LoginTicket | None:
    return (db.query(LoginTicket)
              .filter(LoginTicket.account_id == account_id,
                      LoginTicket.stage == "wait_code",
                      LoginTicket.expires_at > datetime.now())
              .order_by(LoginTicket.id.desc()).first())


@router.post("/{account_id}/send-sms")
def send_sms(account_id: int, request: Request,
             db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("account:login"))):
    account = _get_account(db, account_id)
    if account.status == "disabled":
        raise HTTPException(400, "账号已停用")
    recent = (db.query(LoginTicket)
                .filter(LoginTicket.account_id == account_id)
                .order_by(LoginTicket.id.desc()).first())
    if recent and recent.stage == "wait_code" and (datetime.now() - recent.created_at).total_seconds() < SMS_INTERVAL_SECONDS:
        wait = int(SMS_INTERVAL_SECONDS - (datetime.now() - recent.created_at).total_seconds())
        raise HTTPException(429, f"短信发送间隔限制，请 {wait}s 后重试（服务端 60s 限流）")
    try:
        client = bridge.build_client(account)
        client.ensure_sk()
        bridge.proto_send_sms(client, account.phone)
    except bridge.RateLimitError as e:
        raise HTTPException(429, f"茶姬服务端限流: {e}")
    except bridge.ChageeBridgeError as e:
        raise HTTPException(400, str(e))
    except bridge.ChageeError as e:
        raise HTTPException(502, f"协议错误: {e}")
    except Exception as e:  # 网络层等
        raise HTTPException(502, f"请求失败: {type(e).__name__}: {e}")
    ticket = LoginTicket(
        account_id=account.id, phone=account.phone, stage="wait_code",
        expires_at=datetime.now() + timedelta(minutes=TICKET_TTL_MINUTES),
    )
    db.add(ticket)
    if account.status == "expired":
        account.status = "pending"
    db.commit()
    log_audit(db, request, user, "account.send_sms", f"{account.label}#{account.id}",
              {"phone": mask_phone(account.phone), "ticket": ticket.id})
    return {"ticket_id": ticket.id, "expires_at": ticket.expires_at,
            "message": "验证码已发送（10 分钟内有效），请在手机上查收后填入"}


@router.post("/{account_id}/login")
def login_with_code(account_id: int, body: SmsCodeRequest, request: Request,
                    db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("account:login"))):
    account = _get_account(db, account_id)
    ticket = _active_ticket(db, account_id)
    if not ticket:
        raise HTTPException(400, "没有待验证的登录工单，请先发送短信验证码")
    try:
        client = bridge.build_client(account)
        client.ensure_sk()
        bridge.proto_login_sms(client, account.phone, body.code)
    except bridge.ChageeBridgeError as e:
        raise HTTPException(400, str(e))
    except bridge.ChageeError as e:
        ticket.stage, ticket.last_error = "failed", str(e)[:250]
        db.commit()
        raise HTTPException(502, f"登录失败（验证码或工单可能已过期）: {e}")
    except Exception as e:
        raise HTTPException(502, f"请求失败: {type(e).__name__}: {e}")

    # 从隔离 session 文件读回 token/sk（DB 为事实源）
    sess = bridge.read_session_file(account.id)
    token = sess.get("token", "")
    if not token:
        raise HTTPException(502, "登录成功但未取回 token，请检查账号会话文件")
    account.token, account.sk = token, sess.get("sk", "")
    account.status = "online"
    account.last_login_at = datetime.now()
    ticket.stage = "succeeded"
    db.commit()

    # whoami 回填昵称/customerId（失败不影响登录结果）
    who = {}
    try:
        client.token = token
        client.proto.token = token
        who = client.whoami().get("data", {}) or {}
        account.nickname = who.get("nickName") or account.nickname
        account.customer_id = str(who.get("customerId") or account.customer_id)
        account.last_check_at = datetime.now()
        db.commit()
    except Exception:
        pass
    # 登录即同步券档案：F4 拉取可用/历史两列表全量入库（操作员免手动查询）。
    # best-effort：失败仅 WARN 留痕，绝不影响登录结果；CHAGEE_LOGIN_COUPON_SYNC=0 可关闭
    coupons_synced = None
    if os.environ.get("CHAGEE_LOGIN_COUPON_SYNC", "1") != "0":
        try:
            client.token = token
            client.proto.token = token
            result = bridge.proto_coupons(client)
            from routers.ops import _persist_coupon_records   # 惰性导入防环
            summary = result.get("summary", {}) or {}
            coupons_synced = {
                "total": _persist_coupon_records(db, account, result),
                "effective": summary.get("effective_total", 0),
                "historical": summary.get("historical_total", 0),
            }
        except Exception as e:
            coupons_synced = None
            log_op(level="WARN", action="coupon.login_sync", actor=user.username,
                   target=f"{account.label}#{account.id}", result="failed", error=e)
    log_audit(db, request, user, "account.login", f"{account.label}#{account.id}",
              {"customerId": account.customer_id, "nick": account.nickname,
               "coupons_synced": coupons_synced["total"] if coupons_synced else None})
    return {"ok": True, "status": "online", "nickname": account.nickname,
            "customer_id": account.customer_id, "coupons_synced": coupons_synced,
            "token_fingerprint": token[:16] + f"...len={len(token)}"}


@router.post("/{account_id}/logout")
def logout_account(account_id: int, request: Request,
                   db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("account:login"))):
    account = _get_account(db, account_id)
    try:
        client = bridge.build_client(account)
        bridge.proto_logout(client)
    except bridge.ChageeBridgeError as e:
        raise HTTPException(400, str(e))
    except bridge.ChageeError as e:
        raise HTTPException(502, f"协议错误: {e}")
    except Exception as e:
        raise HTTPException(502, f"请求失败: {type(e).__name__}: {e}")
    finally:
        account.token, account.status = "", "pending"
        db.commit()
    log_audit(db, request, user, "account.logout", f"{account.label}#{account.id}")
    return {"ok": True, "status": "pending"}


@router.post("/{account_id}/check")
def check_account(account_id: int, request: Request,
                  db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("account:login"))):
    account = _get_account(db, account_id)
    try:
        client = bridge.build_client(account)
        info = bridge.proto_whoami(client)
    except bridge.SessionExpiredError as e:
        account.status = "expired"
        account.last_check_at = datetime.now()
        db.commit()
        log_audit(db, request, user, "account.check", f"{account.label}#{account.id}",
                  {"result": "expired", "error": str(e)[:200]})
        return {"status": "expired", "status_label": STATUS_LABELS["expired"],
                "message": "凭证已失效（单会话语义：可能在 App 端重新登录被踢），需重新短信登录"}
    except bridge.ChageeBridgeError as e:
        raise HTTPException(400, str(e))
    except bridge.ChageeError as e:
        raise HTTPException(502, f"协议错误: {e}")
    except Exception as e:
        raise HTTPException(502, f"请求失败: {type(e).__name__}: {e}")

    data = info.get("data", {}) or {}
    account.status = "online"
    account.nickname = data.get("nickName") or account.nickname
    account.customer_id = str(data.get("customerId") or account.customer_id)
    account.last_check_at = datetime.now()
    db.commit()
    log_audit(db, request, user, "account.check", f"{account.label}#{account.id}",
              {"result": "online", "customerId": account.customer_id})
    return {"status": "online", "status_label": STATUS_LABELS["online"],
            "nickname": account.nickname, "customer_id": account.customer_id,
            "last_check_at": account.last_check_at}
