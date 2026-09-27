#!/usr/bin/env python3
"""持久 attach：钩 android.webkit.WebView 全部加载入口，实时打印 URL 到文件+stdout。"""
import frida
import sys
import time

DEVICE_SERIAL = "125.109.27.7:58445"
SCRIPT_PATH = r"C:\baidunetdiskdownload\霸王茶姬\frida\scripts\hook_webview_url.js"
LOG_PATH = r"C:\baidunetdiskdownload\霸王茶姬\output\webview_hook.log"

JS_EXTRA = r"""
Java.perform(function () {
    var WV = Java.use('android.webkit.WebView');
    WV.loadDataWithBaseURL.implementation = function (base, data, mime, enc, hist) {
        console.log('[loadDataWithBaseURL] base=' + base + ' len=' + (data ? data.length : 0));
        return this.loadDataWithBaseURL(base, data, mime, enc, hist);
    };
    try {
        var InAppWV = Java.use('com.pichillilorenzo.flutter_inappwebview.InAppWebView');
        console.log('[+] InAppWebView class found');
    } catch (e) {}
    console.log('[+] extra hooks ready');
});
"""

def main():
    out = open(LOG_PATH, "a", encoding="utf-8")
    def on_message(message, data):
        if message.get("type") == "error":
            line = "[error] " + str(message.get("description"))
        else:
            line = str(message.get("payload"))
        print(line, flush=True)
        out.write(line + "\n"); out.flush()

    dev = frida.get_device(DEVICE_SERIAL, timeout=10)
    pid = None
    for p in dev.enumerate_processes():
        if p.name == "霸王茶姬" or p.name == "com.chagee.application.cn":
            pid = p.pid
            break
    if pid is None:
        sys.exit("app not running")
    print("attach pid", pid, flush=True)
    session = dev.attach(pid)
    src = open(SCRIPT_PATH, encoding="utf-8").read() + JS_EXTRA
    script = session.create_script(src)
    script.on("message", on_message)
    script.load()
    print("hooks loaded, streaming...", flush=True)
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
