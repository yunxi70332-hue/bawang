"""多账号管理系统 — FastAPI 后端入口。

启动：cd account_system/server && python -m uvicorn app:app --host 127.0.0.1 --port 8000
生产托管前端：web/dist 构建产物由本服务静态托管（见末尾 StaticFiles 挂载）。
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from routers import (accounts, audit, auth, decision, events, intake, keepalive, logs, ops,
                     orders, payportal, roles, settings, users)
from log_setup import setup_logging
from oplog import init_oplog, install_request_middleware, log_op
from log_monitor import start_log_monitor_thread
from seed import init_db
from services.order_reconcile import start_reconcile_thread
from services.menu_spec import start_menu_refresh_thread
from services.payment_events import start_pay_watcher
from services.keepalive import start_keepalive_thread

app = FastAPI(
    title="霸王茶姬多账号管理系统 API",
    version="1.0",
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
app.include_router(decision.router)   # 下单决策：套餐/券成本/配置/扫描库存/报表/流水（/api/ops/decision/...）
app.include_router(decision.global_router)   # decide 决策评估（POST /api/ops/orders/decide，权限 feature:order）
app.include_router(intake.router)   # 异步订单中枢：内部登记/队列/死信/密钥管理（/api/intake/...）
app.include_router(intake.v1_router)   # 外部 KFC 系适配（/api/intake/v1/...，X-Api-Key 鉴权）
app.include_router(audit.router)
app.include_router(events.router)   # SSE 实时事件流（/api/events：仪表盘统计推送 + 全量取餐码扫描进度）
app.include_router(logs.router)   # 全局日志/告警查询与处置（/api/ops/logs、/api/ops/alerts）
app.include_router(keepalive.router)   # 账号保活跃任务：状态/配置/运行记录/手动触发（/api/ops/keepalive/*）
app.include_router(settings.router)   # 系统设置：代理出口配置/状态/诊断/切换日志（/api/ops/settings/proxy*）
# H5 收银台公开路由（/pay/*，token 即凭证无 JWT）：主 API 本机也可访问，
# 局域网由独立进程 pay_portal:app 绑 0.0.0.0:8010 暴露同一组路由（管理 API 不进局域网）
app.include_router(payportal.pay_router)

# 收银台页面静态资源（qrcode.min.js 等；须在 SPA catch-all 之前挂载）
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/api/health", tags=["meta"])
def health():
    return {"ok": True, "service": "chagee-account-system", "version": app.version}


@app.on_event("startup")
def startup():
    setup_logging()   # 日志最先落位：其后 init_db / 后台线程的输出才有文件可查
    init_oplog(process="main-api")   # 全局操作日志就位（进程名随每条记录落库）
    log_op("system.startup", params={"pid": os.getpid(), "port": 8000})
    init_db()
    start_reconcile_thread()   # 后台订单校准（间隔 CHAGEE_RECONCILE_INTERVAL_SECONDS，默认 60s）
    start_menu_refresh_thread()   # 菜单规格库每日刷新（CHAGEE_MENU_REFRESH_INTERVAL_SECONDS，默认 86400）
    start_keepalive_thread()   # 账号保活跃：每天 10:30 间歇访问广东省门店菜单（CHAGEE_KEEPALIVE_THREAD=0 停用）
    start_pay_watcher()   # 支付会话 watcher：自动发现已支付/已取消并收口 + 外部回调（CHAGEE_PAYWATCH_INTERVAL_SECONDS，默认 2s）
    start_log_monitor_thread()   # 日志监控告警线程：CHAGEE_LOG_MONITOR_INTERVAL_SECONDS，默认 30s
    from services.dashboard_push import start_dashboard_push_thread
    start_dashboard_push_thread()   # 仪表盘统计 SSE 推送（CHAGEE_DASHBOARD_PUSH_INTERVAL_SECONDS，默认 5s，指纹变化才推）
    from services.order_worker import start_order_workers
    start_order_workers()   # 异步订单中枢 worker 池（CHAGEE_ORDER_WORKERS，默认 2；<=0 不启动）
    from services.backup import start_backup_thread
    start_backup_thread()   # SQLite 一致性快照备份（CHAGEE_BACKUP_INTERVAL_SECONDS，默认每日；<=0 不启动）
    from services.cloud_tunnel import start_tunnel
    start_tunnel()   # 云手机 WebView 隧道托管子进程（与主服务同寿命；CHAGEE_TUNNEL_ENABLED=0 停用）
    from services.net_proxy import start_net_proxy
    start_net_proxy()  # 网络出口管理：urlopen 代理补丁 + 健康监控线程（CHAGEE_PROXY_CHECK_INTERVAL_SECONDS=0 停用）


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
