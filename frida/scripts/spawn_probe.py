#!/usr/bin/env python3
"""spawn 模式全钩探针：进程冻结时装好所有 WebView 相关钩子，再放行并驱动 UI。
覆盖：WebView 全部 $init 重载、loadUrl 全部重载、loadDataWithBaseURL、postUrl、
InputAwareWebView 全部 $init。"""
import frida
import subprocess
import sys
import time

ADB = r"C:\platform-tools\adb.exe"
DEV = "125.109.27.7:58445"
PKG = "com.chagee.application.cn"

JS = r"""
Java.perform(function () {
    function H(cls, method, sig, tag) {
        try {
            var C = Java.use(cls);
            if (sig === null) {
                C[method].implementation = function () {
                    var a = Array.prototype.slice.call(arguments);
                    console.log('[' + tag + '] ' + a.map(String).join(' | ').substring(0, 200));
                    return C[method].apply(this, a);
                };
            } else {
                C[method].overload.apply(C[method], sig).implementation = function () {
                    var a = Array.prototype.slice.call(arguments);
                    console.log('[' + tag + '] ' + a.map(String).join(' | ').substring(0, 200));
                    return C[method].overload.apply(C[method], sig).apply(this, a);
                };
            }
            console.log('[hooked] ' + tag);
        } catch (e) { console.log('[hookfail] ' + tag + ' : ' + e); }
    }
    var WV = 'android.webkit.WebView';
    H(WV, '$init', ['android.content.Context'], 'WV.ctor1');
    H(WV, '$init', ['android.content.Context', 'android.util.AttributeSet'], 'WV.ctor2');
    H(WV, 'loadUrl', ['java.lang.String'], 'WV.loadUrl');
    H(WV, 'loadUrl', ['java.lang.String', 'java.util.Map'], 'WV.loadUrl2');
    H(WV, 'loadDataWithBaseURL', null, 'WV.loadData');
    H(WV, 'postUrl', null, 'WV.postUrl');
    H('com.pichillilorenzo.flutter_inappwebview_android.webview.in_app_webview.InputAwareWebView',
      '$init', ['android.content.Context'], 'IAW.ctor1');
    H('com.pichillilorenzo.flutter_inappwebview_android.webview.in_app_webview.InputAwareWebView',
      '$init', ['android.content.Context', 'android.view.View', 'java.lang.Boolean'], 'IAW.ctor3');
});
"""


def drive():
    def sh(cmd, wait):
        subprocess.run([ADB, "shell", cmd], capture_output=True,
                       env={"MSYS_NO_PATHCONV": "1", "PATH": "", "SYSTEMROOT": "C:\\Windows"})
        time.sleep(wait)
    sh("input tap 648 1236", 4)      # 我的
    sh("input tap 600 145", 4)       # 立即登录
    sh("input tap 360 450", 2)       # 手机号框
    sh("input text 19241719504", 2)
    sh("input tap 115 565", 1)       # 协议
    sh("input tap 360 695", 9)       # 获取验证码


def main():
    dev = frida.get_device(DEV, timeout=10)
    pid = dev.spawn([PKG])
    print("spawned", pid, flush=True)
    s = dev.attach(pid)
    sc = s.create_script(JS)
    sc.on("message", lambda m, d: print(m.get("payload") if m.get("type") == "log" else f"[{m.get('type')}] {m.get('description') or m.get('payload')}", flush=True))
    sc.load()
    dev.resume(pid)
    print("resumed, waiting app boot...", flush=True)
    time.sleep(14)
    print("=== driving UI ===", flush=True)
    drive()
    print("=== done ===", flush=True)
    time.sleep(3)
    s.detach()

if __name__ == "__main__":
    main()
