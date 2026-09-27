"""统一日志装配：root logger 双输出（控制台 + data/logs/<log_name>.log 轮转文件）。

为什么独立模块：装配点不止一个（app.py startup、离线测试直调），幂等标志防重复挂
handler（重复挂载会让同一条日志按次数翻倍输出）。uvicorn.* 层级自带 handler 且
不向 root 传播，这里只配 root、完全不碰它们——访问日志仍走 uvicorn 自己的通道。

文件名按进程区分（log_name：主 API 默认 server，pay_portal 传 payportal）；
目录可被环境变量 CHAGEE_LOG_DIR 覆盖（调用时求值而非 import 时）——离线测试
借此把文本日志重定向到临时目录，不污染生产 data/logs/server.log。
"""

import logging
import os
from logging.handlers import RotatingFileHandler

# server/x.py 上两层 = account_system/（与 database.DATA_DIR 同级定位）；默认目录，
# 可被环境变量 CHAGEE_LOG_DIR 覆盖（在 setup_logging 内调用时读取）
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "logs")

_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
# 幂等标记挂在 handler 实例属性上：判断「root 是否已有我们装的 handler」
_HANDLER_FLAG = "_chagee_log_setup"


def setup_logging(log_name: str = "server") -> None:
    """幂等装配 root logger：INFO 级，控制台 + 5MB×3 轮转文件（含线程名——
    后台线程 order-reconcile / pay-watcher 的日志按 threadName 即可区分归属）。"""
    root = logging.getLogger()
    if any(getattr(h, _HANDLER_FLAG, False) for h in root.handlers):
        return   # 已装配过（startup 重入/测试多次直调），不重复挂载
    log_dir = os.environ.get("CHAGEE_LOG_DIR") or LOG_DIR   # 调用时求值：运行期设置也生效
    formatter = logging.Formatter(_FORMAT)
    os.makedirs(log_dir, exist_ok=True)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    setattr(console, _HANDLER_FLAG, True)
    file_handler = RotatingFileHandler(os.path.join(log_dir, f"{log_name}.log"),
                                       maxBytes=5 * 1024 * 1024,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_FLAG, True)
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)
