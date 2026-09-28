#!/usr/bin/env python3
"""inject_slider_shim — frida 注入滑块 WebView 劫持 ChageeCall/JS桥，捕获 captchaVerifyParam。

原理：App 的滑块页 success(captchaVerifyParam) → ChageeCall.postMessage →
flutter_inappwebview.callHandler('ChageeHandler', msg)。两层都钩：
  1) 重写 window.ChageeCall（页面先定义者胜——注入在 App 桥之前或直接覆盖）
  2) 包裹 flutter_inappwebview.callHandler
捕获后写入 document.title=CAPTOKEN:<param>，本脚本轮询 getTitle 取走存文件。
"""
import frida
import json
import sys
import time

DEV = "39.174.221.6:58445"
OUT = r"C:\baidunetdiskdownload\霸王茶姬\output\mitm\minted_token.txt"

SHIM = r"""
(function(){
  try {
    var payload = null;
    function capture(tag, msg){
      try{
        var s = String(msg);
        if (s.indexOf('captchaVerify') >= 0) {
          document.title = 'CAPTOKEN:' + s;
        }
      }catch(e){}
    }
    // 层2：包 flutter_inappwebview.callHandler
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      var orig = window.flutter_inappwebview.callHandler;
      window.flutter_inappwebview.callHandler = function(name, msg){
        capture(name, msg);
        return orig.apply(this, arguments);
      };
    }
    // 层1：覆盖 ChageeCall（页面 success 里 if(window.ChageeCall) 直接用）
    window.ChageeCall = {
      postMessage: function(m){
        capture('chageecall', m);
        // 同时透传给原生桥，App 正常继续流程
        if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
          try { window.flutter_inappwebview.callHandler('ChageeHandler', m); } catch(e){}
        }
      }
    };
    return 'shim-ok';
  } catch(e) { return 'shim-err:' + e; }
})()
"""

POLL = "(function(){ try{ return document.title || ''; }catch(e){ return ''; } })()"


def main():
    dev = frida.get_device(DEV, timeout=10)
    pid = next(p.pid for p in dev.enumerate_processes()
               if p.name in ("霸王茶姬", "com.chagee.application.cn"))
    print("attach", pid, flush=True)
    s = dev.attach(pid)
    sc = s.create_script(r"""
    function findWebViews(){
        var out = [];
        Java.choose('android.webkit.WebView', {
            onMatch: function(wv){ out.push(wv); },
            onComplete: function(){}
        });
        return out;
    }
    function injectAll(){
        Java.perform(function(){
            var wvs = findWebViews();
            for (var i = 0; i < wvs.length; i++) {
                (function(wv, idx){
                    Java.scheduleOnMainThread(function(){
                        try {
                            wv.evaluateJavascript(SHIM_SRC, null);
                            console.log('[inject] wv#' + idx + ' ok');
                        } catch(e) { console.log('[inject] wv#' + idx + ' err ' + e); }
                    });
                })(wvs, i);
            }
            console.log('[inject] total=' + wvs.length);
        });
    }
    function pollTitles(){
        Java.perform(function(){
            var wvs = findWebViews();
            for (var i = 0; i < wvs.length; i++) {
                (function(wv, idx){
                    Java.scheduleOnMainThread(function(){
                        try {
                            wv.evaluateJavascript(POLL_SRC, Java.registerClass({
                                name: 'cb' + Date.now() + idx,
                                implements: [Java.use('android.webkit.ValueCallback')],
                                methods: {
                                    onReceiveValue: function(v){ if (v) console.log('[title#' + idx + '] ' + v); }
                                }
                            }));
                        } catch(e) {}
                    });
                })(wvs, i);
            }
        });
    }
    rpc.exports = { inject: injectAll, poll: pollTitles };
    """)
    # 简化：直接把 SHIM/POLL 源码内联
    sc = s.create_script(
        "var SHIM_SRC = " + json.dumps(SHIM) + ";\n" +
        "var POLL_SRC = " + json.dumps(POLL) + ";\n" + r"""
    function findWebViews(){
        var out = [];
        Java.choose('android.webkit.WebView', {
            onMatch: function(wv){ out.push(wv); },
            onComplete: function(){}
        });
        return out;
    }
    function injectAll(){
        var wvs = findWebViews();
        for (var i = 0; i < wvs.length; i++) {
            (function(wv, idx){
                Java.scheduleOnMainThread(function(){
                    try {
                        wv.evaluateJavascript(SHIM_SRC, null);
                        console.log('[inject] wv#' + idx + ' ok');
                    } catch(e) { console.log('[inject] wv#' + idx + ' err ' + e); }
                });
            })(wvs, i);
        }
        console.log('[inject] total=' + wvs.length);
    }
    var ValueCallback = Java.use('android.webkit.ValueCallback');
    function pollTitles(){
        var wvs = findWebViews();
        for (var i = 0; i < wvs.length; i++) {
            (function(wv, idx){
                Java.scheduleOnMainThread(function(){
                    try {
                        var cb = Java.registerClass({
                            name: 'com.shim.Poll' + Date.now() + '_' + idx + '_' + Math.floor(Math.random()*99999),
                            implements: [ValueCallback],
                            methods: { onReceiveValue: function(v){ if (v) console.log('[title#' + idx + '] ' + v); } }
                        });
                        wv.evaluateJavascript(POLL_SRC, cb.$new());
                    } catch(e) {}
                });
            })(wvs, i);
        }
    }
    injectAll();
    setInterval(function(){ if (!tokenFound) pollTitles(); }, 3000);
    var tokenFound = false;
    """)
    token_done = {"v": False}
    def on_message(m, d):
        if m.get("type") == "log":
            p = str(m.get("payload"))
            print(p, flush=True)
            if "CAPTOKEN:" in p:
                raw = p.split("CAPTOKEN:", 1)[1]
                raw = raw.strip('"').replace('\\"', '"')
                try:
                    obj = json.loads(raw)
                    param = obj.get("param", "")
                except Exception:
                    param = raw
                if param.startswith("eyJ"):
                    with open(OUT, "w", encoding="utf-8") as f:
                        f.write(param)
                    token_done["v"] = True
                    print("!!! TOKEN SAVED:", param[:60], flush=True)
        else:
            print(m, flush=True)
    sc.on("message", on_message)
    sc.load()
    print("shim injected, polling titles... (用户此刻可拖滑块)", flush=True)
    while not token_done["v"]:
        time.sleep(1)
    print("done", flush=True)
    time.sleep(2)
    s.detach()


if __name__ == "__main__":
    main()
