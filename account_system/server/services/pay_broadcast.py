"""跨进程支付会话状态通知（主 API 8000 → 收银台 8010）。

为什么需要：mark_session 收口（paid/cancelled/expired）发生在主 API 进程（watcher /
reconcile / 管理端手动取消），而 H5 收银台的 SSE 订阅者（/pay/{token}/events）挂在
收银台独立进程——进程间无共享内存，需要一个通知通道让订阅者「秒级」收到终态事件，
而不是只靠收银台进程自己的 1s 兜底轮询。

设计约束：
  - **不得 import services.pay_session**（pay_session 反向 import 本模块做 mark_session
    联动，反向依赖会成环）
  - fire-and-forget：通知失败只 logger.debug——收银台 SSE 有 1s 兜底轮询比对
    (status, pay_deadline_ts)，丢一条通知最多晚 1 秒补推，绝不影响状态收口主流程
  - 开关 CHAGEE_PAY_BROADCAST_ENABLED（默认 "1"；置 "0" 完全不发网络——离线测试必置 0）
  - 鉴权：X-Internal-Token = data/internal_broadcast.secret（secrets.token_hex(16)，
    双进程同机同文件天然共享；首读时惰性创建，"x" 独占创建避免双进程并发写互相覆盖）

requests 缺失时整体静默 no-op（环境没装 requests 时退化为纯兜底轮询模式）。
"""

import logging
import os
import secrets as _secrets

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:   # requests 非硬依赖：缺失时通知整体 no-op（SSE 兜底轮询仍工作）
    requests = None

# 收银台进程的内部广播端点（与 pay_portal.py 固定端口 8010 对齐）
PAYPORTAL_INTERNAL_URL = "http://127.0.0.1:8010/internal/broadcast"

# services/x.py 上三层 = account_system/（与 payment_events.py 的 secret.key 定位同法）
_ACCOUNT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SECRET_PATH = os.path.join(_ACCOUNT_ROOT, "data", "internal_broadcast.secret")

_secret_cache: str | None = None   # 模块级缓存：secret 文件只在首读时触盘一次


def broadcast_enabled() -> bool:
    """环境开关：CHAGEE_PAY_BROADCAST_ENABLED，默认开（"0" 完全不发网络）。"""
    return os.environ.get("CHAGEE_PAY_BROADCAST_ENABLED", "1") == "1"


def load_internal_secret() -> str:
    """读/建内部广播密钥（模块级缓存；读不到返回空串——校验双方都空时视为失配 401）。"""
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    try:
        os.makedirs(os.path.dirname(_SECRET_PATH), exist_ok=True)
        if not os.path.exists(_SECRET_PATH):
            try:
                # "x" 独占创建：双进程同时首启时只有一方写入，另一方走读路径拿到同一密钥
                with open(_SECRET_PATH, "x", encoding="utf-8") as f:
                    f.write(_secrets.token_hex(16))
            except FileExistsError:
                pass
        with open(_SECRET_PATH, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            _secret_cache = value
    except Exception:
        logger.debug("内部广播密钥读取失败（广播将退化为纯兜底轮询）", exc_info=True)
    return _secret_cache or ""


def notify_session_change(order_no: str, status: str) -> None:
    """会话状态变更通知：POST 收银台 /internal/broadcast（超短超时，任何异常只 debug）。

    timeout=0.3：通知是「加速器」而非关键路径，宁可丢通知走兜底轮询，也不能让
    mark_session 的调用方（探针/校准线程）被网络等待拖住。proxies 显式置 None：
    继承系统代理会把 127.0.0.1 的本机回环请求劫持到代理服务器（payment_events 回调
    同坑，见其 _OPENER 注释）。
    """
    if not broadcast_enabled():
        return
    if requests is None:
        return
    try:
        requests.post(PAYPORTAL_INTERNAL_URL,
                      json={"order_no": str(order_no or ""), "status": str(status or "")},
                      headers={"X-Internal-Token": load_internal_secret()},
                      timeout=0.3,
                      proxies={"http": None, "https": None})
    except Exception:
        # 收银台进程未启动 / 端口未监听 / 网络抖动：全部静默，SSE 兜底轮询 1s 内补推
        logger.debug("支付会话状态广播失败（忽略，兜底轮询会补）order_no=%s status=%s",
                     order_no, status, exc_info=True)
