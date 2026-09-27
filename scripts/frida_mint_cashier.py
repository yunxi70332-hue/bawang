#!/usr/bin/env python3
"""纯协议复现 · frida 铸造收银台链接：把指定订单的 orderStr 喂给云手机上的支付宝 SDK，
当场铸造 mobilecashier 会话并打开 H5PayActivity——从而为「系统创建的订单」产出可打开其
支付页的 H5 链接（链接捕获由 DevTools 哨兵完成）。

原理（docs/cashier_link_assembly_20260927.md）：cashierRoutePay 链接的 session 在
「SDK 拿 orderStr 调 PayTask.pay → mobilegw 铸造」时产生并与该订单绑定。本脚本经 frida
在茶姬 App 进程内直接调用 PayTask(orderStr)，把铸造动作从"用户在 App 里点支付"变成
"系统按需触发"。

安全边界：只打开收银台页面（人工登录/输密码才可能扣款），本脚本不做任何扣款动作，
与 PaySubmitBlocked 安全门一致。

两种消费方式：
  CLI（仓库根目录运行）：
    python scripts/frida_mint_cashier.py --order-str "<alipay_sdk=...>"   # 直接给串
    python scripts/frida_mint_cashier.py --order-no 2026...               # 从系统 PaySession 取
  服务端复用（account_system/server/services/cashier_mint.py 后台线程 import 本模块）：
    mint_via_frida / snapshot_mclient_urls / capture_new_cashier /
    reconstruct_entry / probe_alive（全部自捕获异常，返回值判错不抛栈）
前置：frida-server 已在云手机运行（adb forward tcp:27042 已建）；隧道在线。
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))  # 可选 --order-no 时读系统库
sys.path.insert(0, str(REPO / "scripts"))  # 兄弟模块（extract_cashier_link）在任意导入路径下可达

# 云手机环境锚点（服务端/测试经函数参数覆盖）
ADB = r"C:\platform-tools\adb.exe"


def _detect_dev() -> str:
    """云手机地址会变（2026-09-27→28 一夜从 125.109.27.7 换到 39.174.221.6）：
    优先环境变量 CLOUDPHONE_SERIAL，否则自动取 adb 在线设备。"""
    import os
    serial = os.environ.get("CLOUDPHONE_SERIAL", "").strip()
    if serial:
        return serial
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                             timeout=8).stdout or ""
        devs = [l.split("\t")[0] for l in out.splitlines()
                if "\tdevice" in l and not l.startswith("List")]
        if devs:
            return devs[0]
    except Exception:
        pass
    return "125.109.27.7:58445"


DEV = _detect_dev()
FRIDA_HOST = "127.0.0.1:27042"
PACKAGE = "com.chagee.application.cn"
# webview_devtools socket → 本地转发端口起点（与 capture_cashier_devtools.py 同端口段约定）
DEVTOOLS_PORT_BASE = 9230

JS = r"""
'use strict';
// 取 Activity 实例：优先堆扫描主界面类（版本无关），兜底扫描基类
function findActivity() {
    var found = null;
    var tryChoose = function (cls) {
        try {
            Java.choose(cls, {
                onMatch: function (inst) { if (!found) found = inst; },
                onComplete: function () {}
            });
        } catch (e) {}
    };
    tryChoose('com.chagee.application.cn.MainActivity');
    if (!found) tryChoose('android.app.Activity');
    return found;
}
rpc.exports = {
    mint: function (orderStr) {
        return new Promise(function (resolve) {
            Java.perform(function () {
                try {
                    var act = findActivity();
                    if (!act) { resolve({ok: false, error: 'no activity instance found'}); return; }
                    var PayTask = Java.use('com.alipay.sdk.app.PayTask');
                    var task = PayTask.$new(act);
                    // pay() 阻塞且不可在 UI 线程调用——起 Java 线程执行
                    var Runnable = Java.registerClass({
                        name: 'com.chagee.mint.MintRunnable',
                        implements: [Java.use('java.lang.Runnable')],
                        methods: {
                            run: function () {
                                try {
                                    var r = task.pay(orderStr, true);
                                    send({stage: 'pay_return', result: String(r)});
                                } catch (e) {
                                    send({stage: 'pay_error', error: String(e)});
                                }
                            }
                        }
                    });
                    Java.use('java.lang.Thread').$new(Runnable.$new()).start();
                    resolve({ok: true, started: true, note: 'PayTask 已在新线程调用，H5PayActivity 将打开'});
                } catch (e) {
                    resolve({ok: false, error: String(e)});
                }
            });
        });
    }
};
"""


# ---------------- 底层：adb 子调用 / 进程定位 ----------------

def _sh(adb: str, dev: str, *args, timeout: float = 10):
    """adb 子调用统一封装（失败返回 None 不抛——铸造链路全部靠返回值判错）。"""
    try:
        return subprocess.run([adb, "-s", dev, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except Exception:
        return None


def _main_pid(adb: str, dev: str) -> int | None:
    """茶姬主进程 pid 用 adb 精确取（frida 枚举曾只见 :pushcore 子进程，模糊匹配会 attach 错）。"""
    r = _sh(adb, dev, "shell", f"pidof {PACKAGE}")
    out = ((r.stdout if r else "") or "").strip()
    return int(out.split()[0]) if out else None


# ---------------- 核心步骤 1：frida 铸造 ----------------

def mint_via_frida(order_str: str, *, host: str = FRIDA_HOST, adb: str = ADB,
                   dev: str = DEV, timeout_s: float = 45) -> dict:
    """frida attach 茶姬主进程 → 进程内调 PayTask.pay(orderStr) 打开该单收银台页面。

    全路径异常自捕获（失败返回 {ok:False, error:...} 而非抛出）：调用方是服务端后台
    线程（services/cashier_mint.mint_worker），任何失败只需记事件不需要栈冒泡。
    timeout_s 覆盖「拉前台→attach→load→RPC」整体软预算（步骤间检查，RPC 自身由
    frida 传输层超时兜底——真挂死也只占用一个 daemon 线程，不影响请求路径）。
    """
    session = None
    try:
        started = time.time()
        pid = _main_pid(adb, dev)
        if not pid:
            return {"ok": False, "error": f"{PACKAGE} 主进程未运行（先在云手机打开茶姬 App）"}
        # 铸造需要前台 Activity：把 App 拉到前台（不影响登录态，只是打开主界面）
        _sh(adb, dev, "shell", "monkey", "-p", PACKAGE,
            "-c", "android.intent.category.LAUNCHER", "1", timeout=15)
        time.sleep(3)
        if time.time() - started > timeout_s:
            return {"ok": False, "error": "mint_precheck_timeout"}
        import frida  # 延迟导入：本模块被服务端 import 时不强依赖 frida 已装
        device = frida.get_device_manager().add_remote_device(host)
        session = device.attach(pid)
        script = session.create_script(JS)
        script.on("message", lambda m, d: print(f"[device] {json.dumps(m, ensure_ascii=False)[:300]}"))
        script.load()
        result = script.exports_sync.mint(order_str)
        return dict(result or {})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


# ---------------- 核心步骤 2：DevTools 捕获新收银台页面 ----------------

def _devtools_pages(adb: str, dev: str) -> list[dict]:
    """枚举当前全部 webview_devtools socket，逐个 forward 后拉 /json 页面清单。

    每轮全量重扫（不能缓存 forward）：H5PayActivity 打开时会出现新的 webview 进程
    （新 socket），固定一次 forward 会漏掉铸造产物页面。单 socket 拉取失败仅跳过
    （socket 可能刚销毁，forward 残留映射连不上属正常噪声）。
    """
    r = _sh(adb, dev, "shell", "cat /proc/net/unix")
    socks = sorted(set(re.findall(r"(webview_devtools_remote_\d+)", (r.stdout if r else "") or "")))
    pages: list[dict] = []
    for i, sock in enumerate(socks):
        port = DEVTOOLS_PORT_BASE + i
        _sh(adb, dev, "forward", f"tcp:{port}", f"localabstract:{sock}")
        try:
            raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=4)
            data = json.loads(raw.read().decode("utf-8", "replace"))
            if isinstance(data, list):
                pages.extend(p for p in data if isinstance(p, dict))
        except Exception:
            continue
    return pages


def snapshot_mclient_urls(*, adb: str = ADB, dev: str = DEV) -> set[str]:
    """当前云手机上已存在的 mclient 收银台页面 URL 集合（铸造前基线快照）。

    失败返回空集而非抛出：基线拿不到时 capture_new_cashier 退化为「任一 mclient 页
    都算新页」，宁可多捕获一次也不让铸造链路断在旁路观测上。
    """
    try:
        return {str(p.get("url") or "") for p in _devtools_pages(adb, dev)
                if str(p.get("url") or "").startswith("https://mclient.alipay.com/")}
    except Exception:
        return set()


def capture_new_cashier(baseline_urls: set[str], *, wait_s: float = 25,
                        adb: str = ADB, dev: str = DEV) -> str | None:
    """轮询（1.5s 间隔）全部 webview socket 的 /json，发现不在 baseline 里的新 mclient
    页面 URL 即返回该 URL；超时返回 None。

    原样返回页面 URL（landing / cashierRoutePay 形态皆可）——反构入口链接由
    reconstruct_entry 在服务端做，本函数只负责"捕获"。PayTask.pay 之后 H5PayActivity
    数秒内打开（2026-09-27 实证），1.5s 轮询粒度足够。
    """
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            for p in _devtools_pages(adb, dev):
                url = str(p.get("url") or "")
                if url.startswith("https://mclient.alipay.com/") and url not in baseline_urls:
                    return url
        except Exception:
            continue
    return None


# ---------------- 核心步骤 3：反构入口链接 / 存活探测 ----------------

def reconstruct_entry(url: str) -> str | None:
    """landing / cashierRoutePay URL → 可直开的收银台入口链接。

    - 入参已是 cashierRoutePay 形态 → 原样返回（探测/回填直接可用）
    - h5pay/landing 形态 → query_params（URL 编码嵌套）里解出 session/utdid/tid，
      反构 https://mclient.alipay.com/cashierRoutePay.htm?route_pay_from=h5&init_from=
      SDKLite&session=..&utdid=..&tid=..&cc=y（与 alipay_autopay.build_cashier_url 同
      形态，safe='/' 保持 utdid 的 '/' 不转义）
    - 缺 session（核心凭证）或形态不符 → None
    """
    try:
        raw = (url or "").strip()
        u = urllib.parse.urlsplit(raw)
        if "mclient.alipay.com" not in (u.netloc or ""):
            return None
        if u.path.startswith("/cashierRoutePay"):
            return raw
        if not u.path.startswith("/h5pay/landing"):
            return None
        q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        inner = dict(urllib.parse.parse_qsl(q.get("query_params", ""), keep_blank_values=True))
        session = inner.get("session") or q.get("session") or ""
        if not session:
            return None
        params = {"route_pay_from": "h5", "init_from": "SDKLite", "session": session,
                  "cc": "y"}
        # utdid/tid 缺哪个就不带哪个（不造空参数）；tid 外层优先（与 _extract_landing_params 同口径）
        utdid = inner.get("utdid") or q.get("utdid") or ""
        tid = q.get("tid") or inner.get("tid") or ""
        if utdid:
            params["utdid"] = utdid
        if tid:
            params["tid"] = tid
        return ("https://mclient.alipay.com/cashierRoutePay.htm?"
                + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="/"))
    except Exception:
        return None


def probe_alive(url: str) -> dict:
    """链接存活探测（只读 GET，与 alipay_autopay 只读口径一致）——复用既有实现。"""
    from extract_cashier_link import probe_url  # scripts/ 已注入 sys.path
    return probe_url(url)


# ---------------- CLI ----------------

def load_order_str(order_no: str | None, order_str: str | None) -> str:
    if order_str:
        return order_str
    server_dir = REPO / "account_system" / "server"
    sys.path.insert(0, str(server_dir))
    import database  # noqa: E402
    import models  # noqa: E402
    with database.SessionLocal() as db:
        sess = db.query(models.PaySession).filter(models.PaySession.order_no == order_no).first()
        if not sess or not sess.order_str:
            raise SystemExit(f"订单 {order_no} 无支付会话/支付串（先在系统内获取支付串）")
        return sess.order_str


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order-str", help="直接给 orderStr（alipay_sdk=... 整串）")
    ap.add_argument("--order-no", help="从系统 PaySession 取该订单最新支付串")
    args = ap.parse_args()
    if not (args.order_str or args.order_no):
        ap.error("--order-str 与 --order-no 二选一")

    order_str = load_order_str(args.order_no, args.order_str)
    print(f"[*] orderStr 长度 {len(order_str)}，out_trade_no="
          f"{order_str.split('out_trade_no%22%3A%22')[1][:24] if 'out_trade_no%22%3A%22' in order_str else '?'}")

    result = mint_via_frida(order_str)
    print("[*] mint 结果:", json.dumps(result, ensure_ascii=False, indent=1))
    if result.get("ok"):
        print("[*] H5PayActivity 应已打开——DevTools 哨兵会捕获新链接；本脚本保持 5s 观察后退出")
        time.sleep(5)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
