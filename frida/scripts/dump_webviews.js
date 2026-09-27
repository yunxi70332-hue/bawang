// dump_webviews.js — 枚举进程内所有 android.webkit.WebView 实例，打印 getUrl()
Java.perform(function () {
    function dump() {
        console.log('=== WebView instances ===');
        Java.choose('android.webkit.WebView', {
            onMatch: function (wv) {
                Java.scheduleOnMainThread(function () {
                    try {
                        console.log('[wv] url=' + wv.getUrl() + ' | originalUrl=' + wv.getOriginalUrl());
                    } catch (e) {
                        console.log('[wv] err ' + e);
                    }
                });
            },
            onComplete: function () { console.log('=== end ==='); }
        });
    }
    dump();
});
