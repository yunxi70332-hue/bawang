# Flutter Java 装载链与“脱壳”边界

## 已观察到的 Java 侧入口

| 层 | 证据 | 含义 |
|---|---|---|
| Application | `decompiled/jadx/sources/com/chagee/application/cn/ChageeApplication.java` | `ChageeApplication extends FlutterApplication`；`onCreate()` 只调用父类初始化并保存 Android Context。 |
| Activity | `decompiled/jadx/sources/com/chagee/application/cn/MainActivity.java` | `MainActivity extends FlutterFragmentActivity`。 |
| Flutter engine | `MainActivity.configureFlutterEngine(FlutterEngine)` | 调用父类的 engine 配置，随后注册 `ChageeFlutterEngine` MethodChannel。 |
| Dart AOT | `so_analysis/native/arm64-v8a/libapp.so` | 对应 arm64 的 Flutter release AOT 代码；已由 Blutter 恢复为 `so_analysis/blutter_out/asm/`。 |
| Flutter assets | `decompiled/raw_apk/assets/flutter_assets/` | 包含 `AssetManifest.bin/json`、`FontManifest.json`、`NOTICES.Z` 与包资源。 |

## 为什么把 `libapp.so` 当作本次“脱壳”主体

Flutter release 构建把 Dart 业务实现 AOT 编入 ABI 对应的 `libapp.so`。Java/Dex 侧主要负责 Android 生命周期、插件与 Platform Channel；本样本的登录和网络业务函数实际上被恢复在 Blutter 的 Dart-like 结果里，例如：

```text
asm/chagee_base_network/network/chagee_interceptor.dart
asm/chagee_cn_login_module/business/login_service.dart
```

因此本次可复现的恢复流程是：**APK 解压 → 确认 ABI → 选取 arm64 原件 → Blutter snapshot 解析 → Dart-like 文件/RVA 交叉导航**。这不是 Java 源码的完整反编译，也不是将 Dart 恢复成原项目源码。

## 外层壳判断：当前结论与边界

在已反编译的应用类中：

- 未见应用自定义的 `System.loadLibrary("app")`、动态 DEX 解密加载或替换 `ClassLoader` 的直接证据；Flutter 运行库的常规加载流程足以解释 `libapp.so` 的加载。
- `ChageeApplication` 的 `onCreate()` 很短，`MainActivity` 将 Flutter 引擎交给父类初始化；这更符合常规 Flutter embedding，而不是 Java 层业务加密壳。
- APK 仍有第三方 native 库（例如运营商登录、JNI、Flutter engine），它们与 `libapp.so` 的 Flutter AOT 业务层要按 ABI 分开分析。

这只能说明**当前静态样本未发现额外 Java/Dex 壳的直接证据**。要确认运行期是否会下载、替换或二次加载模块，仍应在受控模拟器上采集脱敏启动日志和 `/proc/<pid>/maps` 模块列表。

## 额外静态入口

`MainActivity.onCreate()` 可见 `WebView.setWebContentsDebuggingEnabled(true)`；它是 Android WebView 调试开关，不是 Flutter AOT 的装载或解密逻辑。`MainActivity` 还通过 `ChageeFlutterEngine` MethodChannel 处理应用重启、平台配置和隐私同意后的初始化。

## 下一次动态核验（无需登录账号）

模拟器启动、ADB 可见后，先只观察应用启动：

```powershell
adb devices -l
powershell -ExecutionPolicy Bypass -File .\scripts\capture_runtime.ps1
```

采集前后都使用 `scripts/sanitize_log.py`；不保存 token、手机号、Cookie、完整请求体或任何账户数据。动态目标是核对 Flutter engine/libapp 的装载时序与未登录网络初始化，登录账号不是该步骤的前置条件。
