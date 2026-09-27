// hook_webview_url.js — 抓 flutter_inappwebview 实际加载的 URL（定位滑块 H5 页面地址）
Java.perform(function () {
    var WebView = Java.use('android.webkit.WebView');
    WebView.loadUrl.overload('java.lang.String').implementation = function (url) {
        console.log('[loadUrl] ' + url);
        return this.loadUrl(url);
    };
    WebView.loadUrl.overload('java.lang.String', 'java.util.Map').implementation = function (url, m) {
        console.log('[loadUrl+hdrs] ' + url);
        return this.loadUrl(url, m);
    };
    WebView.postUrl.implementation = function (url, data) {
        console.log('[postUrl] ' + url);
        return this.postUrl(url, data);
    };
    WebView.loadDataWithBaseURL.implementation = function (base, data, mime, enc, hist) {
        console.log('[loadDataWithBaseURL] base=' + base + ' len=' + (data ? data.length : 0));
        return this.loadDataWithBaseURL(base, data, mime, enc, hist);
    };
    WebView.evaluateJavascript.implementation = function (script, cb) {
        var s = String(script);
        if (s.indexOf('ChageeCall') >= 0 || s.indexOf('captcha') >= 0 || s.length < 200)
            console.log('[evalJS] ' + s.substring(0, 300));
        return this.evaluateJavascript(script, cb);
    };
    try {
        var InAppWebViewClient = Java.use('com.pichillilorenzo.flutter_inappwebview.inappwebview.InAppWebViewClient');
        InAppWebViewClient.shouldOverrideUrlLoading.overload('android.webkit.WebView', 'java.lang.String').implementation = function (v, url) {
            console.log('[shouldOverride] ' + url);
            return this.shouldOverrideUrlLoading(v, url);
        };
    } catch (e) { console.log('[+] InAppWebViewClient hook skip: ' + e); }
    console.log('[+] webview url hooks ready');
});
