#!/usr/bin/env python3
"""一体化滑块探针：attach 装钩 → adb 驱动到滑块 → 收集 WebView 构造/加载事件。"""
import frida
import subprocess
import sys
import time

ADB = r"C:\platform-tools\adb.exe"
DEV = "125.109.27.7:58445"

def sh(*args, wait=1.0):
    cmd = [ADB, "shell"] + list(args)
    subprocess.run(["MSYS_NO_PATHCONV=1"] + cmd, shell=False, capture_output=True)
    time.sleep(wait)

def adb_shell(cmd, wait=1.0):
    env = {"MSYS_NO_PATHCONV": "1", "PATH": "", "SYSTEMROOT": "C:\\Windows"}
    subprocess.run([ADB, "shell", cmd], capture_output=True, env=env)
    time.sleep(wait)

JS = r"""
Java.perform(function () {
    function log(m) { console.log(m); }
    try {
        var IAW = Java.use('com.pichillilorenzo.flutter_inappwebview_android.webview.in_app_webview.InputAwareWebView');
        IAW.$init.overload('android.content.Context').implementation = function (c) {
            log('[IAW<init> ctx]');
            return this.$init(c);
        };
        IAW.$init.overload('android.content.Context', 'android.view.View', 'java.lang.Boolean').implementation = function (a, b, c) {
            log('[IAW<init> ctx,view,bool]');
            return this.$init(a, b, c);
        };
        log('[+] IAW ctor hooked');
    } catch (e) { log('[IAW err] ' + e); }
    var WV = Java.use('android.webkit.WebView');
    WV.$init.overload('android.content.Context').implementation = function (c) {
        log('[WebView<init>]');
        return this.$init(c);
    };
    log('[+] WebView ctor hooked');
    // v6 loadUrl 真实路径：InAppWebViewManager / InAppWebView (非View类) 的 loadUrl(UrlRequest)
    ['com.pichillilorenzo.flutter_inappwebview_android.webview.in_app_webview.InAppWebView',
     'com.pichillilorenzo.flutter_inappwebview_android.InAppWebView'].forEach(function (n) {
        try {
            var C = Java.use(n);
            C.loadUrl.overload('com.pichillilorenzo.flutter_inappwebview_android.types.UrlRequest').implementation = function (req) {
                log('[' + n + '.loadUrl] ' + req.getUrl());
                return this.loadUrl(req);
            };
            log('[+] hooked ' + n + '.loadUrl');
        } catch (e) { log('[skip] ' + n); }
    });
});
"""

def main():
    dev = frida.get_device(DEV, timeout=10)
    pid = next(p.pid for p in dev.enumerate_processes() if p.name in ("霸王茶姬", "com.chagee.application.cn"))
    print("attach", pid, flush=True)
    s = dev.attach(pid)
    sc = s.create_script(JS)
    sc.on("message", lambda m, d: print(m.get("payload") if m.get("type") == "log" else f"[{m.get('type')}] {m.get('description') or m.get('payload')}", flush=True))
    sc.load()
    time.sleep(1)
    print("=== tapping get-code ===", flush=True)
    adb_shell("input tap 360 695", 8)
    print("=== events above ===", flush=True)
    time.sleep(2)
    s.detach()

if __name__ == "__main__":
    main()
