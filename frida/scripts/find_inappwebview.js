// find_inappwebview.js — 枚举已加载类，找 InAppWebView 真实类名
Java.perform(function () {
    Java.enumerateClassLoaders({
        onMatch: function (loader) {
            try {
                var factory = Java.ClassFactory.get(loader);
                ['com.pichillilorenzo.flutter_inappwebview.inappwebview.InAppWebView',
                 'com.pichillilorenzo.flutter_inappwebview.android.inappwebview.InAppWebView',
                 'com.pichillilorenzo.flutter_inappwebviewflutter.inappwebview.InAppWebView'
                ].forEach(function (name) {
                    try {
                        var cls = factory.use(name);
                        console.log('[FOUND] ' + name);
                        var proto = cls.class.getSuperclass();
                        console.log('  super=' + proto.getName());
                    } catch (e) { /* not this one */ }
                });
            } catch (e) { }
        },
        onComplete: function () { console.log('[enum done]'); }
    });
});
