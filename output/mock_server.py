# 临时 mock 后端：托管 web/dist 前端 + 假账号 API，用于安全验证「新建账号内完成登录」流程
# （绝不触达真实茶姬协议层，验证完即关闭）
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

DIST = Path(__file__).resolve().parent.parent / "account_system" / "web" / "dist"

app = FastAPI()

PERMS = ["account:read", "account:create", "account:update", "account:delete", "account:login"]
ROLE = {"id": 1, "name": "admin", "description": "管理员", "is_builtin": True}
USER = {
    "id": 1, "username": "admin", "display_name": "管理员", "is_active": True,
    "last_login_at": None, "created_at": "2026-01-01T00:00:00", "role": ROLE,
}

now = lambda: time.strftime("%Y-%m-%dT%H:%M:%S")


def acc(aid, label, phone, status="pending", nickname=None, customer_id=None):
    return {
        "id": aid, "label": label, "phone_masked": phone[:3] + "****" + phone[-4:],
        "phone_full": phone, "device_uuid": f"mock-uuid-{aid}", "customer_id": customer_id,
        "nickname": nickname, "status": status,
        "status_label": {"pending": "待登录", "online": "在线", "expired": "凭证失效",
                         "disabled": "已停用"}.get(status, status),
        "group": "默认", "note": "", "token_fingerprint": "", "has_token": False,
        "last_login_at": None, "last_check_at": None, "created_by": "admin",
        "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-01T00:00:00",
    }


DB = {"next_id": 100, "items": [acc(1, "种子账号-甲", "13800000001"), acc(2, "种子账号-乙", "13800000002")]}


@app.post("/api/auth/login")
def login(body: dict):
    return {"token": "mock-token", "user": USER, "permissions": PERMS}


@app.get("/api/auth/me")
def me():
    return USER


@app.get("/api/accounts")
def list_accounts():
    return {"total": len(DB["items"]), "items": DB["items"], "groups": ["默认"]}


@app.post("/api/accounts")
async def create_account(body: dict):
    row = acc(DB["next_id"], body.get("label", ""), body.get("phone", ""))
    row["group"] = body.get("group") or "默认"
    row["note"] = body.get("note") or ""
    DB["next_id"] += 1
    DB["items"].insert(0, row)
    print(f"[mock] create_account -> {row['id']} {row['label']} {row['phone_full']}")
    return row


@app.post("/api/accounts/{aid}/send-sms")
async def send_sms(aid: int):
    row = next((r for r in DB["items"] if r["id"] == aid), None)
    print(f"[mock] send_sms -> {aid} ({row['phone_full'] if row else '?'})")
    time.sleep(0.6)  # 模拟协议层耗时，便于观察 loading
    return {"ticket_id": f"mock-ticket-{aid}", "expires_at": now(), "message": "mock 已发送"}


@app.post("/api/accounts/{aid}/login")
async def login_sms(aid: int, body: dict):
    row = next((r for r in DB["items"] if r["id"] == aid), None)
    assert row is not None
    row["status"], row["status_label"] = "online", "在线"
    row["nickname"], row["customer_id"] = "模拟茶友", f"C{aid:06d}"
    row["token_fingerprint"], row["has_token"] = "mock-token-head...len=608", True
    row["last_login_at"] = now()
    print(f"[mock] login -> {aid} code={body.get('code')}")
    return {"ok": True, "status": "online", "nickname": row["nickname"],
            "customer_id": row["customer_id"], "token_fingerprint": row["token_fingerprint"]}


@app.get("/api/dashboard/stats")
def stats():
    return {}


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        return JSONResponse({"detail": "mock 未实现"}, status_code=404)
    from fastapi.responses import FileResponse
    f = DIST / path
    return FileResponse(f if f.is_file() else DIST / "index.html")
