"""多账号管理系统 — FastAPI 后端入口。

启动：cd account_system/server && python -m uvicorn app:app --host 127.0.0.1 --port 8000
生产托管前端：web/dist 构建产物由本服务静态托管（见末尾 StaticFiles 挂载）。
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from routers import accounts, audit, auth, logs, ops, orders, payportal, roles, users
from log_setup import setup_logging
from oplog import init_oplog, install_request_middleware, log_op
from log_monitor import start_log_monitor_thread
from seed import init_db
from services.order_reconcile import start_reconcile_thread
from services.menu_spec import start_menu_refresh_thread
from services.payment_events import start_pay_watcher

app = FastAPI(
    title="霸王茶姬多账号管理系统 API",
    version="1.0.0",
    description="基于六功能纯协议层（scripts/chagee_*.py）的多账号管理与 RBAC 系统",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 请求 trace + 访问日志中间件
install_request_middleware(app)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(accounts.router)
app.include_router(ops.router)
app.include_router(orders.router)   # F5 下单 + F6 取餐查询（/api/ops/accounts/{id}/orders...）
app.include_router(orders.global_router)   # 券使用记录 / 券档案 / 支付事件流（/api/ops/...）
app.include_router(audit.router)
app.include_router(logs.router)   # 全局日志/告警查询与处置（/api/ops/logs、/api/ops/alerts）
# H5 收银台公开路由（/pay/*，token 即凭证无 JWT）：主 API 本机也可访问，
# 局域网由独立进程 pay_portal:app 绑 0.0.0.0:8010 暴露同一组路由（管理 API 不进局域网）
app.include_router(payportal.pay_router)

# 收银台页面静态资源（qrcode.min.js 等；须在 SPA catch-all 之前挂载）
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/api/health", tags=["meta"])
def health():
    return {"ok": True, "service": "chagee-account-system", "version": "1.0.0"}


@app.on_event("startup")
def startup():
    setup_logging()   # 日志最先落位：其后 init_db / 后台线程的输出才有文件可查
    init_oplog(process="main-api")   # 全局操作日志就位（进程名随每条记录落库）
    log_op("system.startup", params={"pid": os.getpid(), "port": 8000})
    init_db()
    start_reconcile_thread()   # 后台订单校准（间隔 CHAGEE_RECONCILE_INTERVAL_SECONDS，默认 60s）
    start_menu_refresh_thread()   # 菜单规格库每日刷新（CHAGEE_MENU_REFRESH_INTERVAL_SECONDS，默认 86400）
    start_pay_watcher()   # 支付会话 watcher：自动发现已支付/已取消并收口 + 外部回调（CHAGEE_PAYWATCH_INTERVAL_SECONDS，默认 2s）
    start_log_monitor_thread()   # 日志监控告警线程：CHAGEE_LOG_MONITOR_INTERVAL_SECONDS，默认 30s


# 生产模式：托管前端构建产物（存在 web/dist 时生效；开发模式由 Vite 代理 /api）
# SPA history 路由需要 catch-all 兜底到 index.html（直接 StaticFiles(html=True) 刷新子路径会 404）
_WEB_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dist")
if os.path.isdir(_WEB_DIST):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith(("api/", "docs", "openapi")):
            raise HTTPException(404, "Not Found")
        candidate = os.path.normpath(os.path.join(_WEB_DIST, full_path))
        if full_path and candidate.startswith(_WEB_DIST) and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_WEB_DIST, "index.html"))
