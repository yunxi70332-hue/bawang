// 环境隐藏脚本：对网易易盾风控 SDK 屏蔽 root/frida/代理痕迹
// 依据：reverse_learning_log.md:164 —— App 本体无检测，检测集中在易盾(com.netease.nis)
// 策略：File.exists 白名单化敏感路径 / Runtime.exec su 拒绝 / /proc maps 重定向净化 /
//       Settings(ADB/proxy) 读值伪装 / frida 线程名从 Thread 枚举中隐藏

var SENSITIVE_PATHS = [
    '/data/adb', '/data/adb/ksud', '/data/adb/magisk', '/data/adb/modules',
    '/data/local/tmp/frida-server', '/data/local/tmp/fs1666-arm64',
    '/system/bin/su', '/system/xbin/su', '/sbin/su', '/su/bin/su',
    '/system/app/Superuser.apk', '/system/bin/busybox',
    '/data/local/xposed', '/system/framework/xposed', 'edxposed', 'riru',
    '/data/local/tmp/re.frida.server',
];

function shouldHide(path) {
    if (!path) return false;
    var p = path.toString();
    for (var i = 0; i < SENSITIVE_PATHS.length; i++) {
        if (p === SENSITIVE_PATHS[i] || p.indexOf(SENSITIVE_PATHS[i]) === 0) return true;
    }
    if (p.indexOf('/proc/self/maps') === 0 || /\/proc\/\d+\/maps/.test(p)) return 'MAPS';
    return false;
}

Java.perform(function () {
    // 1) File.exists 屏蔽敏感路径
    try {
        var File = Java.use('java.io.File');
        File.exists.implementation = function () {
            var p = shouldHide(this.getAbsolutePath());
            if (p === true) return false;
            if (p === 'MAPS') return true; // 存在，但打开时给净化版
            return this.exists();
        };
    } catch (e) { console.log('[hide] File.exists skip: ' + e); }

    // 2) /proc/*/maps 读取重定向到净化副本
    try {
        var FIS = Java.use('java.io.FileInputStream');
        var File2 = Java.use('java.io.File');
        var String = Java.use('java.lang.String');
        FIS.$init.overload('java.io.File').implementation = function (f) {
            var p = shouldHide(f.getAbsolutePath ? f.getAbsolutePath() : '');
            if (p === 'MAPS') {
                try {
                    var real = Java.cast(f, File2).getAbsolutePath();
                    var fis0 = FIS.$new(real);
                    var br = Java.use('java.io.BufferedReader').$new(
                        Java.use('java.io.InputStreamReader').$new(fis0));
                    var sb = '';
                    var line;
                    while ((line = br.readLine()) !== null) {
                        var ls = line.toString();
                        if (ls.indexOf('frida') < 0 && ls.indexOf('gum') < 0 && ls.indexOf('linjector') < 0) {
                            sb += ls + '\n';
                        }
                    }
                    br.close();
                    var out = Java.use('java.io.FileOutputStream').$new('/data/local/tmp/.mc');
                    var bytes = String.$new(sb).getBytes('UTF-8');
                    out.write(bytes);
                    out.close();
                    return FIS.$new(File2.$new('/data/local/tmp/.mc'));
                } catch (e2) { console.log('[hide] maps redirect fail: ' + e2); }
            }
            return this.$init(f);
        };
    } catch (e) { console.log('[hide] FIS skip: ' + e); }

    // 3) Runtime.exec 拒绝 su 探测
    try {
        var RT = Java.use('java.lang.Runtime');
        RT.exec.overload('java.lang.String').implementation = function (cmd) {
            var c = cmd.toString();
            if (c.indexOf('su') === 0 || c.indexOf('which su') >= 0 || c.indexOf('/su ') >= 0) {
                throw Java.use('java.io.IOException').$new('Permission denied');
            }
            return this.exec(cmd);
        };
    } catch (e) { console.log('[hide] exec skip: ' + e); }

    // 4) Settings 伪装：adb 关、代理无
    try {
        var Secure = Java.use('android.provider.Settings$Secure');
        Secure.getInt.overload('android.content.ContentResolver', 'java.lang.String', 'int')
            .implementation = function (cr, name, def) {
            if (name.toString() === 'adb_enabled') return 0;
            return this.getInt(cr, name, def);
        };
    } catch (e) {}
    try {
        var Global = Java.use('android.provider.Settings$Global');
        Global.getString.overload('android.content.ContentResolver', 'java.lang.String')
            .implementation = function (cr, name) {
            if (name.toString() === 'http_proxy') return null;
            return this.getString(cr, name);
        };
    } catch (e) {}

    // 5) Thread 名隐藏（gum-js-loop / pool-frida 等）
    try {
        var Thread = Java.use('java.lang.Thread');
        Thread.getName.implementation = function () {
            var n = this.getName();
            if (n) {
                n = n.toString();
                if (n.indexOf('gum') >= 0 || n.indexOf('frida') >= 0 || n.indexOf('pool-thread-') >= 0 && false) {
                    return 'Worker-' + (this.hashCode() & 0xffff);
                }
            }
            return n;
        };
    } catch (e) {}

    console.log('[hide] env_hide active @ ' + new Date().toISOString());
});
