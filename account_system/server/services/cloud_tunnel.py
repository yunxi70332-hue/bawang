"""云手机 WebView 代理隧道托管：作为主 API(8000) 的常驻子进程运行。

为什么放服务进程里：独立后台任务会随终端会话回收而消亡（多次实证）；隧道是云手机
收银台的出网生命线，必须与服务同寿命。方案 = DETACHED_PROCESS 分离子进程跑
scripts/restore_webview_proxy.py（自动探测设备地址），监督线程每 60s 检活、死亡即重启
（云手机换 IP / adb 抖动后脚本自身已能自适应重连）。

开关：CHAGEE_TUNNEL_ENABLED（默认 1；离线测试置 0）。
"""
import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))           # services/x.py 上四层 = 仓库根
_SCRIPT = os.path.join(_REPO, "scripts", "restore_webview_proxy.py")
_PYTHON = os.path.join(_REPO, ".venv_verify", "Scripts", "python.exe")  # venv 在仓库根内（不是上层）
_LOG = os.path.join(_REPO, "account_system", "data", "logs", "tunnel.log")
os.makedirs(os.path.dirname(_LOG), exist_ok=True)

_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def _spawn() -> subprocess.Popen:
    # DETACHED_PROCESS：脱离任何控制台/会话树，只认本服务进程为锚（服务退出时可选择回收）
    return subprocess.Popen([_PYTHON, "-u", _SCRIPT, "--quiet"],
                            stdout=open(_LOG, "ab"),
                            stderr=subprocess.STDOUT,
                            creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def start_tunnel() -> None:
    """幂等启动：起分离子进程 + 60s 监督线程（死亡自动重启，换设备自动重连由脚本自理）。"""
    global _proc
    if os.environ.get("CHAGEE_TUNNEL_ENABLED", "1") != "1":
        logger.info("云手机隧道未启用（CHAGEE_TUNNEL_ENABLED=0）")
        return
    with _lock:
        if _proc and _proc.poll() is None:
            return
        try:
            _proc = _spawn()
        except Exception:
            logger.warning("隧道子进程启动失败", exc_info=True)
            return

    def _supervise():
        while True:
            time.sleep(60)
            with _lock:
                if _proc and _proc.poll() is not None:
                    logger.info("隧道子进程退出(code=%s)，60s 后重启", _proc.returncode)
                    time.sleep(5)
                    try:
                        globals()["_proc"] = _spawn()
                        logger.info("隧道子进程已重启")
                    except Exception:
                        logger.warning("隧道子进程重启失败", exc_info=True)

    threading.Thread(target=_supervise, name="cloud-tunnel-supervisor", daemon=True).start()
    logger.info("云手机隧道子进程已启动（pid=%s，监督 60s 周期）", _proc.pid)
