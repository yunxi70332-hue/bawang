"""协议层桥接：把 scripts/ 下已验证的纯协议模块接入管理系统。

关键约束（来自 docs/protocol_six_features_20260923.md）：
  - 数据库是账号凭证（token/sk/uuid）的唯一事实源；每账号在 data/accounts/<id>/
    下维护隔离的 device.json / session.json，供 ChageeClient 文件式构造
  - 单会话语义：协议登录会使该账号旧 token 立即失效（无 refresh），失效码 401/12320120400401
  - 生产外向动作（发短信/登录/登出）由路由层审计 + 前端二次确认，本层只负责执行
  - getsk 与业务域名固定 release 环境（test 已弃用）
"""

import json
import os
import shutil
import sys
import time
import uuid as uuidlib

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from chagee_client import ChageeClient, ChageeError, RateLimitError, SessionExpiredError  # noqa: E402
from chagee_coupon_api import ChageeCouponApi  # noqa: E402
from chagee_login import login_sms, send_sms  # noqa: E402
from chagee_menu_api import ChageeMenuApi  # noqa: E402
from chagee_trade_api import (  # noqa: E402,F401
    ChageeTradeApi, ConsistencyError, OrderHangError, OrderOutcome, PayLink, TradeError,
)

ACCOUNTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "accounts")
os.makedirs(ACCOUNTS_DIR, exist_ok=True)

__all__ = [
    "ChageeError", "RateLimitError", "SessionExpiredError",
    "TradeError", "ConsistencyError", "OrderHangError",
    "ChageeTradeApi", "OrderOutcome", "PayLink",
    "ChageeBridgeError", "build_client", "trade_api", "new_device_uuid", "account_workdir",
    "remove_workdir", "proto_send_sms", "proto_login_sms", "proto_logout",
    "proto_whoami", "proto_coupons", "menu_api", "read_session_file",
]

# 供路由层映射 HTTP 状态码（子类异常须排在父类 TradeError 之前，按序 isinstance 匹配）
ERROR_STATUS = {
    SessionExpiredError: 409, RateLimitError: 429, ChageeError: 502,
    ConsistencyError: 409, OrderHangError: 409, TradeError: 502,
}


class ChageeBridgeError(Exception):
    """桥接层本地错误（如账号未登录/已停用），HTTP 400。"""


def account_workdir(account_id: int) -> str:
    return os.path.join(ACCOUNTS_DIR, str(account_id))


def new_device_uuid() -> str:
    return str(uuidlib.uuid4())


def _ensure_device_file(workdir: str, device_uuid: str):
    os.makedirs(workdir, exist_ok=True)
    path = os.path.join(workdir, "device.json")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "uuid": device_uuid,
                "persistent_device_id": "device_" + os.urandom(8).hex(),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "managed",
            }, f, ensure_ascii=False, indent=2)


def build_client(account) -> ChageeClient:
    """按账号构造隔离客户端：seed 文件指向不存在路径避免污染 scripts 种子，构造后以 DB 值覆盖。"""
    if account.status == "disabled":
        raise ChageeBridgeError("账号已停用，无法执行协议操作")
    workdir = account_workdir(account.id)
    _ensure_device_file(workdir, account.device_uuid)
    client = ChageeClient(
        env="release",
        device_file=os.path.join(workdir, "device.json"),
        session_file=os.path.join(workdir, "session.json"),
        seed_file=os.path.join(workdir, "seed_unused.json"),
    )
    client.uuid = account.device_uuid
    client.token = account.token or ""
    client.proto.token = client.token
    client.proto.sk = account.sk or ""
    return client


def remove_workdir(account_id: int):
    shutil.rmtree(account_workdir(account_id), ignore_errors=True)


def read_session_file(account_id: int) -> dict:
    path = os.path.join(account_workdir(account_id), "session.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ---------------- 功能1：设备注册 + 短信登录（生产闭环已验证） ----------------

def proto_send_sms(client: ChageeClient, phone: str) -> dict:
    """POST /user-client/message/send —— 真实投递，60s 限流由 ChageeClient 分级。"""
    return send_sms(client, phone)


def proto_login_sms(client: ChageeClient, phone: str, code: str) -> str:
    """登录成功后 token/sk 落在账号隔离的 session.json，由调用方读回并写库。"""
    return login_sms(client, phone, code)


def proto_logout(client: ChageeClient) -> dict:
    from chagee_login import logout as _logout
    return _logout(client)


# ---------------- 会话自检（whoami） ----------------

def proto_whoami(client: ChageeClient) -> dict:
    if not client.token:
        raise ChageeBridgeError("账号尚未登录（无 token），请先完成短信登录")
    return client.whoami()


# ---------------- 功能4：优惠券三列表分类查询 ----------------

def proto_coupons(client: ChageeClient) -> dict:
    if not client.token:
        raise ChageeBridgeError("账号尚未登录（无 token），请先完成短信登录")
    if not client.proto.sk:
        client.ensure_sk()
    return ChageeCouponApi(client).classify()


# ---------------- 功能5/6：订单域（F5 下单 + F6 取餐查询） ----------------

def trade_api(account) -> ChageeTradeApi:
    """按账号构造下单引擎（内部走 build_client 隔离构造，凭证/设备与其它功能一致）。"""
    return ChageeTradeApi(build_client(account))


# ---------------- 功能3：游客模式城市/门店/菜单（无 token） ----------------

_menu_api = None


def menu_api() -> ChageeMenuApi:
    global _menu_api
    if _menu_api is None:
        _menu_api = ChageeMenuApi()
    return _menu_api
