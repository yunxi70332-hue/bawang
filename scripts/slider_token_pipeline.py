#!/usr/bin/env python3
"""slider_token_pipeline — 滑块 token 拦截→抓取→双路重发 总装。

循环：
  1. 每 3s 向全部 WebView 注入阻断 shim（token 只进 console/logcat，App 拿不到）
  2. 每 0.5s 扫 logcat 的 "success captchaVerifyParam"
  3. 抓到即：落盘 → PC 协议重发 → 若拒 → 手机 curl 重发（同 IP）
用法：python slider_token_pipeline.py <phone> <account_id> [分钟]
"""
import frida
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

ADB = r"C:\platform-tools\adb.exe"
DEV = "39.174.221.6:58445"
OUT = os.path.normpath(os.path.join(HERE, "..", "output", "mitm", "minted_token.txt"))

BLOCK_JS = ("(function(){if(window.__blk)return 'd';window.__blk=1;"
 "window.ChageeCall={postMessage:function(m){try{console.log('BLOCKED');}catch(e){}}};"
 "if(window.flutter_inappwebview&&window.flutter_inappwebview.callHandler){"
 "var o=window.flutter_inappwebview.callHandler;"
 "window.flutter_inappwebview.callHandler=function(n,m){"
 "if(m&&String(m).indexOf('captchaVerify')>=0){console.log('BLOCKED_H');return;}"
 "return o.apply(this,arguments);};}"
 "console.log('BLOCKSHIM_ON');return 'on';})()")

INJECT = ("Java.perform(function(){\n"
 "var WV = Java.use('android.webkit.WebView');\n"
 "var evalJs = WV.evaluateJavascript.overload('java.lang.String','android.webkit.ValueCallback');\n"
 "var SRC = " + json.dumps(BLOCK_JS) + ";\n"
 "Java.choose('android.webkit.WebView', {\n"
 "  onMatch: function(wv){ Java.scheduleOnMainThread(function(){\n"
 "    try{ evalJs.call(wv, SRC, null); }catch(e){}\n"
 "  }); },\n"
 "  onComplete: function(){} });\n"
 "});\n")

TOKEN_RE = re.compile(r"success captchaVerifyParam: (eyJ[A-Za-z0-9+/=]+)")


def adb_logcat_dump():
    env = {"MSYS_NO_PATHCONV": "1", "PATH": "", "SYSTEMROOT": "C:\\Windows"}
    r = subprocess.run([ADB, "-s", DEV, "logcat", "-d"], capture_output=True, env=env)
    return r.stdout.decode("utf-8", "replace")


def send_with_token(phone, account_id, token, via_phone=False):
    """构造签名加密请求；via_phone=True 时在手机上以 curl 发出（同 IP）。"""
    from chagee_client import ChageeClient
    import urllib.request
    wd = os.path.normpath(os.path.join(
        HERE, "..", "account_system", "data", "accounts", account_id))
    c = ChageeClient(env="release",
                     device_file=os.path.join(wd, "device.json"),
                     session_file=os.path.join(wd, "session.json"),
                     seed_file=os.path.join(wd, "seed_unused.json"))
    c.ensure_sk()
    body = {"scene": "login", "mobile": phone, "sendObj": phone, "phoneCode": "86",
            "blockParam": "不验证", "sid": c.uuid,
            "timestamp": int(time.time() * 1000), "captchaVerifyParam": token}
    body = c._prepare_body(body, sign_fields=["sendObj", "sid", "timestamp"],
                           encrypt_fields=["sendObj", "mobile"])
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = c.env["gw"] + "/user-client/message/send"
    if not via_phone:
        req = urllib.request.Request(url, data=data,
                                     headers=c.headers(logged_in=False), method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    # 手机出口：把完整请求推到手机用 curl 发
    hdr_lines = "\r\n".join(f"-H '{k}: {v}'" for k, v in c.headers(logged_in=False).items())
    esc = data.decode().replace("'", "'\\''")
    cmd = (f"curl -s --max-time 15 -X POST {hdr_lines} "
           f"-H 'Content-Type: application/json' "
           f"--data-binary '{esc}' '{url}'")
    env = {"MSYS_NO_PATHCONV": "1", "PATH": "", "SYSTEMROOT": "C:\\Windows"}
    r = subprocess.run([ADB, "-s", DEV, "shell", f"su -c \"{cmd.replace(chr(34), chr(39))}\""],
                       capture_output=True, env=env, timeout=30)
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except Exception:
        return {"raw": r.stdout.decode("utf-8", "replace")[:300]}


def main():
    phone, account_id = sys.argv[1], sys.argv[2]
    minutes = float(sys.argv[3]) if len(sys.argv) > 3 else 5
    dev = frida.get_device(DEV, timeout=10)
    pid = next(p.pid for p in dev.enumerate_processes()
               if p.name in ("霸王茶姬", "com.chagee.application.cn"))
    print(f"[pipeline] attach {pid}，阻断循环启动（{minutes} 分钟窗口）——请触发滑块并拖动", flush=True)
    s = dev.attach(pid)
    sc = s.create_script(INJECT)
    sc.on("message", lambda m, d: None)
    sc.load()

    baseline = len(adb_logcat_dump())
    deadline = time.time() + minutes * 60
    n = 0
    while time.time() < deadline:
        # 重复注入（页面重载后幂等补注入）
        try:
            s2 = dev.attach(pid)
            sc2 = s2.create_script(INJECT)
            sc2.on("message", lambda m, d: None)
            sc2.load()
            s2.detach()
        except Exception:
            pass
        # 扫 logcat
        log = adb_logcat_dump()
        m = None
        for m in TOKEN_RE.finditer(log[baseline:] if baseline < len(log) else log):
            pass
        if m:
            token = m.group(1)
            print(f"[pipeline] TOKEN 抓到 len={len(token)} head={token[:36]}", flush=True)
            with open(OUT, "w", encoding="utf-8") as f:
                f.write(token)
            r1 = send_with_token(phone, account_id, token, via_phone=False)
            d1 = r1.get("data") or {}
            print(f"[PC发] sendFlag={d1.get('sendFlag')} action={(d1.get('dispose') or {}).get('action')}", flush=True)
            if d1.get("sendFlag") is not True:
                r2 = send_with_token(phone, account_id, token, via_phone=True)
                d2 = r2.get("data") or {}
                print(f"[手机发] sendFlag={d2.get('sendFlag')} action={(d2.get('dispose') or {}).get('action')} raw={str(r2)[:160]}", flush=True)
                ok = d2.get("sendFlag") is True
            else:
                ok = True
            print("[RESULT]", "短信已投递 ✓（纯协议过滑块成功）" if ok else "两路均被拒", flush=True)
            return
        time.sleep(3)
        n += 1
        if n % 20 == 0:
            print(f"[pipeline] 等待中... {int(n*3)}s", flush=True)
    print("[RESULT] 超时未捕获", flush=True)


if __name__ == "__main__":
    main()
