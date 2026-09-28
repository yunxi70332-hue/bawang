#!/usr/bin/env python3
"""capture_token_daemon — frida 注入滑块页桥劫持 shim，捕获 captchaVerifyParam 落盘。

关键：WebView.evaluateJavascript 必须显式 overload call（frida null 传参坑）。
"""
import frida
import json
import time

DEV = "39.174.221.6:58445"
OUT = r"C:\baidunetdiskdownload\霸王茶姬\output\mitm\minted_token.txt"

JS = r"""
Java.perform(function(){
    var WV = Java.use('android.webkit.WebView');
    var evalJs = WV.evaluateJavascript.overload('java.lang.String','android.webkit.ValueCallback');
    var SHIM = "(function(){try{" +
        "if(window.__cap_installed) return 'dup';window.__cap_installed=1;" +
        "function capture(m){try{var s=String(m);if(s.indexOf('captchaVerify')>=0){document.title='CAPTOKEN:'+s;}}catch(e){}}" +
        "if(window.flutter_inappwebview&&window.flutter_inappwebview.callHandler){var o=window.flutter_inappwebview.callHandler;" +
        "window.flutter_inappwebview.callHandler=function(n,m){capture(m);return o.apply(this,arguments);};}" +
        "window.ChageeCall={postMessage:function(m){capture(m);" +
        "if(window.flutter_inappwebview&&window.flutter_inappwebview.callHandler){try{window.flutter_inappwebview.callHandler('ChageeHandler',m);}catch(e){}}}};" +
        "document.title='SHIM-OK';return 'ok';}catch(e){return 'err:'+e;}})()";

    function eachWv(cb) {
        Java.choose('android.webkit.WebView', {
            onMatch: function(wv){ cb(wv); },
            onComplete: function(){}
        });
    }
    function inject() {
        eachWv(function(wv){
            Java.scheduleOnMainThread(function(){
                try { evalJs.call(wv, SHIM, null); console.log('[inject] ok'); }
                catch(e){ console.log('[inject] ' + e); }
            });
        });
    }
    function poll() {
        eachWv(function(wv){
            Java.scheduleOnMainThread(function(){
                try {
                    var t = wv.getTitle();
                    if (t) console.log('[title] ' + t);
                } catch(e){}
            });
        });
    }
    inject();
    setInterval(function(){ Java.perform(function(){ poll(); }); }, 2000);
});
"""


def main():
    dev = frida.get_device(DEV, timeout=10)
    pid = next(p.pid for p in dev.enumerate_processes()
               if p.name in ("霸王茶姬", "com.chagee.application.cn"))
    print("attach", pid, flush=True)
    s = dev.attach(pid)
    got = {"v": False}

    def on_message(m, d):
        if m.get("type") == "log":
            p = str(m.get("payload"))
            print(p, flush=True)
            if "CAPTOKEN:" in p:
                raw = p.split("CAPTOKEN:", 1)[1].strip()
                param = ""
                for attempt in (raw, raw.replace('\\"', '"')):
                    try:
                        param = json.loads(attempt).get("param", "")
                        if param:
                            break
                    except Exception:
                        pass
                if param.startswith("eyJ"):
                    with open(OUT, "w", encoding="utf-8") as f:
                        f.write(param)
                    got["v"] = True
                    print("!!! TOKEN SAVED len=%d head=%s" % (len(param), param[:50]), flush=True)
        else:
            print(m, flush=True)

    sc = s.create_script(JS)
    sc.on("message", on_message)
    sc.load()
    print(">>> 捕获器就绪，等滑块完成...", flush=True)
    for _ in range(1200):
        if got["v"]:
            break
        time.sleep(1)
    print("EXIT", "captured" if got["v"] else "timeout", flush=True)
    s.detach()


if __name__ == "__main__":
    main()
