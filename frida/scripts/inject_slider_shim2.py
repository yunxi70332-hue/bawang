#!/usr/bin/env python3
"""inject_slider_shim2 — loadUrl(javascript:) 注入 + getTitle() 轮询捕获 token。"""
import frida
import json
import time

DEV = "39.174.221.6:58445"
OUT = r"C:\baidunetdiskdownload\霸王茶姬\output\mitm\minted_token.txt"

SHIM = r"""
(function(){
  try {
    function capture(msg){
      try{
        var s = String(msg);
        if (s.indexOf('captchaVerify') >= 0) {
          document.title = 'CAPTOKEN:' + s;
        }
      }catch(e){}
    }
    if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
      var orig = window.flutter_inappwebview.callHandler;
      window.flutter_inappwebview.callHandler = function(name, msg){
        capture(msg);
        return orig.apply(this, arguments);
      };
    }
    window.ChageeCall = {
      postMessage: function(m){
        capture(m);
        if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
          try { window.flutter_inappwebview.callHandler('ChageeHandler', m); } catch(e){}
        }
      }
    };
    document.title = 'SHIM-OK';
    return 'ok';
  } catch(e) { return 'err:' + e; }
})()
"""


def main():
    dev = frida.get_device(DEV, timeout=10)
    pid = next(p.pid for p in dev.enumerate_processes()
               if p.name in ("霸王茶姬", "com.chagee.application.cn"))
    print("attach", pid, flush=True)
    s = dev.attach(pid)
    js = "var SHIM_SRC = " + json.dumps(SHIM) + ";\n" + r"""
    var WV = Java.use('android.webkit.WebView');
    function doInject(){
        Java.perform(function(){
            Java.choose('android.webkit.WebView', {
                onMatch: function(wv){
                    Java.scheduleOnMainThread(function(){
                        try {
                            wv.loadUrl('javascript:' + encodeURIComponent(SHIM_SRC));
                            console.log('[inject] loadUrl ok');
                        } catch(e) { console.log('[inject] err ' + e); }
                    });
                },
                onComplete: function(){}
            });
        });
    }
    function pollTitles(){
        Java.perform(function(){
            Java.choose('android.webkit.WebView', {
                onMatch: function(wv){
                    Java.scheduleOnMainThread(function(){
                        try {
                            var t = wv.getTitle();
                            if (t) console.log('[title] ' + t);
                        } catch(e) {}
                    });
                },
                onComplete: function(){}
            });
        });
    }
    Java.perform(function(){ doInject(); });
    setInterval(function(){ Java.perform(pollTitles); }, 2000);
    """
    done = {"v": False}
    def on_message(m, d):
        if m.get("type") == "log":
            p = str(m.get("payload"))
            print(p, flush=True)
            if "CAPTOKEN:" in p:
                raw = p.split("CAPTOKEN:", 1)[1].strip()
                try:
                    obj = json.loads(raw)
                    param = obj.get("param", "")
                except Exception:
                    # loadUrl 无回调时 title 已是纯 JSON 字符串（可能带转义）
                    raw2 = raw.replace('\\"', '"')
                    try:
                        obj = json.loads(raw2)
                        param = obj.get("param", "")
                    except Exception:
                        param = ""
                if param.startswith("eyJ"):
                    with open(OUT, "w", encoding="utf-8") as f:
                        f.write(param)
                    done["v"] = True
                    print("!!! TOKEN SAVED len=" + str(len(param)), flush=True)
        else:
            print(m, flush=True)
    sc = s.create_script(js)
    sc.on("message", on_message)
    sc.load()
    print(">>> 等待用户拖滑块（每2s轮询 title）...", flush=True)
    for _ in range(600):
        if done["v"]:
            break
        time.sleep(1)
    print("done" if done["v"] else "timeout", flush=True)
    time.sleep(2)
    s.detach()


if __name__ == "__main__":
    main()
