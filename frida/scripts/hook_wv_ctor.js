// hook_wv_ctor.js — WebView/InputAwareWebView 构造钩 + loadUrl 全路径钩，栈回溯定位滑块页面加载
Java.perform(function () {
    function stack(tag) {
        try {
            var Exc = Java.use('java.lang.Exception');
            var Log = Java.use('android.util.Log');
            var st = Log.getStackTraceString(Exc.$new()).split('\n');
            console.log(tag + ' STACK: ' + st.slice(2, 10).join(' <= '));
        } catch (e) { console.log(tag + ' (no stack)'); }
    }
    var IAW = Java.use('com.pichillilorenzo.flutter_inappwebview_android.webview.in_app_webview.InputAwareWebView');
    IAW.$init.overload('android.content.Context').implementation = function (c) {
        console.log('[InputAwareWebView<init> ctx]');
        stack('IAW');
        return this.$init(c);
    };
    IAW.$init.overload('android.content.Context', 'android.view.View', 'java.lang.Boolean').implementation = function (a, b, c) {
        console.log('[InputAwareWebView<init> ctx,view,bool]');
        stack('IAW2');
        return this.$init(a, b, c);
    };
    console.log('[+] InputAwareWebView ctor hooked');
    // v6 的加载入口在 InAppWebViewInterface 实现里；直接钩 UrlRequest 消费点兜底
    Java.enumerateLoadedClasses({
        onMatch: function (name) {
            if (name.indexOf('flutter_inappwebview_android') >= 0 && name.indexOf('InAppWebView') >= 0) {
                console.log('[cls] ' + name);
            }
        },
        onComplete: function () { console.log('[enum done]'); }
    });
});
