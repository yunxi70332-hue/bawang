"""H5 收银台独立轻量入口（0.0.0.0:8010，只暴露 /pay/* 公开路由 + /static）。

为什么独立进程而非挂在主 API 上：主 API 绑 127.0.0.1:8000（管理面不进局域网）；
收银台链接要让手机扫码/直开，必须绑 0.0.0.0——两者安全边界不同，分进程隔离
（pay_token 即凭证，无需 JWT；管理 API 因此不暴露给局域网）。

启动：account_system\\start_payportal.bat
（cd server && ..\\..\\.venv_verify\\Scripts\\python.exe -m uvicorn pay_portal:app --host 0.0.0.0 --port 8010）
"""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routers.payportal import internal_router, pay_router
from log_setup import setup_logging
from oplog import init_oplog, install_request_middleware, log_op
from seed import init_db

app = FastAPI(title="霸王茶姬 H5 收银台", version="1.0.0",
              docs_url=None, redoc_url=None, openapi_url=None)   # 公开端点：关文档面

app.include_router(pay_router)
# 主 API → 本进程的内部通知端点（/internal/broadcast，X-Internal-Token 鉴权）：
# 收口状态变更的 SSE 实时分发入口，仅 127.0.0.1 进程间调用，不属于公开收银台面
app.include_router(internal_router)
# H5 侧请求同样进全局日志，process=pay-portal
install_request_middleware(app)

# 收银台页面引用的静态资源（qrcode.min.js 等）
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def root():
    return {"service": "chagee-pay-portal", "ok": True}


@app.on_event("startup")
def startup():
    # 本进程原先只输出 stderr：装配文件日志（data/logs/payportal.log）补齐
    setup_logging(log_name="payportal")
    init_oplog(process="pay-portal")
    log_op("system.startup", params={"pid": os.getpid(), "port": 8010})
    from services.net_proxy import start_net_proxy
    start_net_proxy()  # 网络出口管理：收银台进程同样接管茶姬域出站（配置与主 API 共享 proxy_config.json）
    # 只建表/迁移，不启动任何后台线程（校准/事件链 watcher/日志监控属主 API 进程
    # 职责——日志监控只跑主 API 一份，双进程同跑会重复告警）
    init_db()
