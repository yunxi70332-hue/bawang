# 霸王茶姬 Flutter AOT 脱壳与网络链路学习记录

## 结论

APK 的业务主体是 Flutter AOT：`libapp.so` 并非传统 Java 壳中可直接还原的 DEX 业务代码。已通过 Blutter 为 arm64 产出可导航的 Dart AOT 恢复结果（伪 Dart + ARM64 地址），可视为本次“脱壳/符号恢复”的完成产物。

- 原始 arm64 `libapp.so`：`E:\霸王茶姬\decompiled\raw_apk\lib\arm64-v8a\libapp.so`
- SHA-256：`8b4aa0dd6dc15ec8380e402bfdb251941678c5b7830ee5a9200822857da5edd7`
- 大小：12911520 bytes
- 输出目录：`so_analysis/blutter_out/`，包含 `asm/`、`pp.txt`、`objs.txt`、`ida_script/` 和 `blutter_frida.js`。

## 为什么这叫 AOT 脱壳/恢复，而不是直接拿到源码

Flutter release 包把 Dart 编译为 AOT ARM64 指令。Blutter 使用相同 Dart VM 版本解析 snapshot/object pool，再把包路径、类、函数和指令地址组织成可读文件。恢复结果用于定位；函数名、类型和伪代码仍要与原始 `libapp.so` 和脱敏运行日志交叉验证。

## 已确认的网络链路锚点

### 请求侧（观察到的调用职责）

`ChageeInterceptor.onRequest` → `_handleRequestPostBody` → `_generateSign` / `buildEncryptedDataFromOriginal` → `_handleRequestHeaders` → `_handleCustomBaseUrl`。

这说明网络层将请求体处理、签名、可选加密、统一 header 合并及可选 base URL 覆盖放在同一个 Dio interceptor 中。这里是理解请求构造的主入口，但当前记录只保留函数边界和字段名，不保存真实 token、设备标识或加密材料。

### 响应侧（观察到的调用职责）

`ChageeInterceptor.onResponse` → `_decryptResponseIfNeeded` → `decryptFieldsInResponse`。恢复指令中可见 `base64Decode`、UTF-8 decode、`AES`、`Encrypted.fromUtf8` 和对 Map/List 的递归遍历。因此可以确认客户端存在按配置字段选择性解密响应字段的代码路径；算法模式、key、IV 与触发条件仍需用脱敏抓包/运行时观测确认。

## 地址与字段证据

### `so_analysis/blutter_out/asm/chagee_base_network/network/chagee_interceptor.dart`

| 函数 | ARM64 RVA | size |
|---|---:|---:|
| `onRequest` | `0xb48248` | `0x214` |
| `_handleRequestPostBody` | `0xb48518` | `0x888` |
| `buildEncryptedDataFromOriginal` | `0xb49234` | `0x59c` |
| `_handleRequestHeaders` | `0xb49ad4` | `0x1b0` |
| `_handleCustomBaseUrl` | `0xb49fa8` | `0xc4` |
| `onResponse` | `0xb21470` | `0x70` |
| `_decryptResponseIfNeeded` | `0xb21730` | `0x404` |
| `decryptFieldsInResponse` | `0xb21ba8` | `0x554` |

保留的结构字段：`customBaseUrl`、`customHeaders`、`sign`。

### `so_analysis/blutter_out/asm/chagee_base_network/network/chagee_network.dart`

| 函数 | ARM64 RVA | size |
|---|---:|---:|
| `_downloadRequestBy` | `0x552994` | `0x108` |

### `so_analysis/blutter_out/asm/chagee_cn_login_module/business/login_service.dart`

| 函数 | ARM64 RVA | size |
|---|---:|---:|
| `initOneClickLogin` | `0xb74810` | `0x208` |
| `preGetPhoneNumber` | `0xb742a0` | `0x244` |
| `_tryOneClickLoginOrFallback` | `0xba1080` | `0x660` |
| `_callOneClickLoginAPI` | `0xba18e8` | `0x598` |
| `_handleOneClickLoginSuccess` | `0xba16e0` | `0x208` |
| `_processLoginSuccess` | `0xba1e80` | `0x20c` |
| `cleanToken` | `0xb75714` | `0x44` |
| `loginSuccess` | `0xb75890` | `0x17c` |

保留的结构字段：`accessToken`、`data`、`login_type`、`login_userLoginToken`、`token`。

## 登录状态模型（静态证据）

登录服务含 `initOneClickLogin`、`preGetPhoneNumber`、`_tryOneClickLoginOrFallback`、`_callOneClickLoginAPI`、`_processLoginSuccess`、`cleanToken` 等入口。令牌模型中可见 `customerId`、`accessToken`、`token`、`newUser`、`firstLogin` 字段；这些字段只说明客户端状态模型，不代表可离线伪造服务端登录。

## 本轮工具问题与解决记录

1. Blutter Windows 安装要求使用 Visual Studio 的 x64 Native/Developer command prompt；本机普通 PowerShell PATH 缺 `cmake`/`ninja` 时会直接报 `FileNotFoundError`。
2. 本机 CMake 4 对上游模板中的空 `CMAKE_CXX_FLAGS` 触发 `string(REPLACE requires at least four arguments)`；本地工具副本通过为 C/C++ flags 传入空白占位继续配置。
3. 编译环境还需要 Windows SDK 的 `rc.exe`/`mt.exe`。从 VS Developer prompt 运行后可被 CMake 发现。
4. 上游 CMake/Ninja + MSVC 对包含中文的绝对路径生成 PCH 时发生 ANSI 路径解码错误（`pch.h` 找不到）。将 Blutter 构建和输出暂存到 ASCII 路径 `C:\blutter_work` / `C:\blutter_out` 后构建成功，再把最终结果复制回本工作区。

GitHub 参考：Blutter 上游仓库 `https://github.com/worawit/blutter`（本地工具版本 commit `4a60ac6`）。README 的 Windows 前置条件与本次工具链修复一致；本轮未依赖第三方业务 APK 示例或泄露数据。

## 下一步（安全的学习验证）

1. 用现有 `scripts/capture_runtime.ps1` 只采集未登录启动流程的脱敏 Logcat，确认 Flutter engine、网络初始化与一键登录预取的发生顺序。
2. 结合 `asm/chagee_base_network/network/chagee_interceptor.dart` 的 RVA，在 IDA/Ghidra 打开原始 arm64 `libapp.so`，验证 call target 与函数范围。
3. 有测试账号或已脱敏 HAR 后，只建立登录前/登录后状态机和字段字典；抓包文件进入 `capture/` 前必须先跑 `scripts/sanitize_log.py`。

## 本轮续做：ABI 归档与静态字段处理证据

### 1. 修正 `libapp.so` ABI 混用

根目录 `so_analysis/libapp.so` 经 ELF header 检查为 `e_machine=0x003e`（x86_64），SHA-256 为 `cd44b8…e148e15c`；它不是本轮 arm64 Blutter 输出的对应原件。原始 arm64 文件仍是：

```text
decompiled/raw_apk/lib/arm64-v8a/libapp.so
e_machine=0x00b7 (AArch64)
SHA-256=8b4aa0dd6dc15ec8380e402bfdb251941678c5b7830ee5a9200822857da5edd7
```

已执行 `scripts/stage_native_libraries.py`，将 APK 内 26 个 `.so` 按 ABI 归档到 `so_analysis/native/<ABI>/`，并生成 `docs/native_abi_inventory.json`。根目录的误副本已改名为 `so_analysis/libapp.x86_64.legacy-copy.so`；今后针对本笔记中的 ARM64 RVA，只打开 `so_analysis/native/arm64-v8a/libapp.so`。

**学习点**：同名 `libapp.so` 可能同时存在 arm64、armeabi-v7a、x86_64 三份。RVA 只在同一编译产物/同一架构内有意义；把 AArch64 地址交给 x86_64 文件，反汇编表面上可能还能跳转，但函数边界和指令语义一定错。

### 2. Blutter 校验命令的真实参数

`validate_blutter_output.py` 的 `--root` 是 **Blutter 恢复输出目录**，不是工程根目录；正确命令为：

```powershell
python scripts/validate_blutter_output.py `
  --root so_analysis/blutter_out `
  --libapp decompiled/raw_apk/lib/arm64-v8a/libapp.so `
  --out docs/blutter_validation.json
```

本次校验 `ok=true`、`dart_file_count=1845`，并确认 interceptor/login 五个关键锚点都存在。渲染和索引脚本也分别使用位置参数 `map`、`index`；把 `--input` 或 `--root` 传给它们会是 CLI 参数错误，不代表脱壳失败。

### 3. 请求/响应字段处理的新增静态证据

新产物：`docs/aot_request_response_flow.md` 和 `docs/aot_crypto_flow.json`。证据链是：

```text
request extra.requestEncryptFields + extra.sk
  -> base64Decode / Key / Encrypted.fromUtf8 / AES
  -> buildEncryptedDataFromOriginal
  -> Encrypter.encrypt -> Encrypted.base64

response extra.responseEncryptFields + extra.sk
  -> base64Decode / Key / Encrypted.fromUtf8 / AES
  -> decryptFieldsInResponse
  -> Encrypter.decrypt64 (Map/List recursion)
```

这只是静态函数/字段证据：没有保存或推导密钥、IV、token、真实请求体，也不能仅靠此结论推定服务端验收规则。下一轮动态验证只需采集未登录启动时的脱敏调用次序。

### 4. 无 pytest 的离线回归

现有测试是 pytest 函数风格，而环境未安装 pytest。新增 `scripts/run_offline_tests.py` 直接发现并执行 `tests/test_*.py` 里的 `test_*` 函数，避免为离线学习工作区新增包依赖。运行：

```powershell
python scripts/run_offline_tests.py
```

本轮结果：`test_aot_crypto_flow`、APK triage 与日志脱敏共 4 项全部通过。

### 5. Java 壳到 Flutter AOT 的装载链

新增 `docs/flutter_loader_chain.md`。JADX 证据表明 `ChageeApplication extends FlutterApplication`，`MainActivity extends FlutterFragmentActivity`，并在 `configureFlutterEngine(FlutterEngine)` 注册 `ChageeFlutterEngine` MethodChannel。Java 入口可以解释 Android 生命周期和平台调用；网络/登录的核心业务锚点则位于恢复后的 AOT 文件。

当前没有发现应用自定义动态 DEX 解密加载或自定义 `System.loadLibrary("app")` 的直接证据，故本轮将“脱壳主体”定义为 ABI 正确的 Flutter `libapp.so` AOT snapshot 恢复。这个结论的边界已写入文档：要验证运行期是否新增模块，仍需受控设备的脱敏启动日志与模块映射。

当前 `adb devices -l` 没有设备；模拟器未启动/未连接是动态验证唯一阻塞。未登录账号不影响已完成的 APK、Dex、AOT 静态分析，也不影响后续未登录启动时序采集。

## 云真机（TJF10128022）脱壳会话（2026-09-21）

### 背景
模拟器（x86_64+ARM 转译、Android 9）上 `/proc/<pid>/maps` Permission denied、Frida 别扭、无蜂窝。用户租了云真机（4G TJ 云真机，Android 13，**原生 arm64-v8a**，HONOR K61L-F0 伪装），提供 H5 控制台（pc.tjphone.cloud）+ 指令调试行。

### 环境与通道（本轮最有价值的工程结论）
- 平台**不暴露 adb connect**；前端只有 WebSocket 串流（create_connect/ws_screen）。
- 三条可用通道：
  1. **命令**：H5「自定义命令」→ 后端 `POST /api/devices/execute_command_custom`，可直接用本地 curl + `X-Token` 调（token 从页面网络请求里取）。身份 uid=2000(shell)。**响应不含 stdout、不区分退出码**（`ls 不存在路径` 也报成功）。
  2. **输出**：`> /sdcard/x.txt` + `am start -a android.intent.action.VIEW -d file:///x.txt -t text/plain`（Android 13 对 shell 可用 file://）+ 控制窗口截图 OCR。清理模块会移除默认文本查看器（弹打开方式，Via 浏览器可看）。
  3. **文件**：本机 curl → `POST /api/file/upload`（云盘 2GB）→ H5「推送」→ 设备 `/sdcard/`；`pm install -r` 安装（shell 可装）。`POST /api/devices/get_install_apps {"device_ids":[id]}` 以特权查包列表（能看到 shell 的 pm list 隐藏的包）。
- 触控注入 = 对串流 video 派发 MouseEvent 序列（pointerdown/mousedown/up/click）；指令菜单是 hover+toggle，「自定义命令」项是特殊的 `.menu-item.custom-command`（无 marquee-wrap），其余项按 `.marquee-wrap[title]` 定位。

### 环境隐藏（阶段 2/3，用户选「平台工具+自检」）
- 检测面（静态已结案）：App 自身无 root/反调试/注入检测；检测集中在易盾风控（com.netease.nis + da/ye.dun.163.com 实测流量）与极光/支付宝/Sentry/神策。
- 基线：属性面干净（release-keys、debuggable 空、qemu 空、user 构建、arm64）；su 四路径全无（KSU 隐藏）；**/data/adb 存在（Permission denied ≠ No such file）= root 指纹残留**；敏感包 = com.tjphone.cloudphonehelper（云机助手，平台依赖）+ com.android.adbkeyboard（分发用输入法，伪装名「映数云输入法」）+ fileexplorer×5。
- 执行：一键清理标记/一键清理模块/一键仿真（国内）/关闭调试 全部成功；复检 shell 可见面不变（其清理的是应用层标记/KSU 模块/指纹值）。**adb_enabled 1→0**，且关闭后 H5 命令通道不受影响（平台自有通道）。
- 残留风险（已记录 docs/cloud_phone_hiding_verification.json）：/data/adb 存在性、云机助手包名。对霸王茶姬本体零影响；对易盾低-中风险。
- **KSU 管理器伪装名「拓展服务」（me.weishu.kernelsu）**，对 shell 的 pm list 隐藏、对平台特权查询可见——这是「包名级隐藏只挡同权限查询」的活例子。

### 安装与基线（阶段 4）
- APK（SHA-256 c4f2e82d…e2959a，155475443B）经 curl 上传云盘（14.5s，平台返回哈希一致）→ 推送 → `pm install -r` 成功（get_install_apps 确认 com.chagee.application.cn）。
- 冷启动到未登录首页（「Hello 欢迎加入霸王茶姬会员」+ 四导航），与模拟器基线同构；截图 capture/cloudphone-baseline-20260921.jpg。
- 流程抽测：自取模式默认激活，门店自动定位**南昌·红谷滩万达店**（云机出口 IP 定位），商品列表完整（伯牙绝弦 ¥16 等）。

### Root 与 maps（阶段 5a/5b）
- **`su -c id` → uid=0(root) context=u:r:magisk:s0，EXIT=0**——KSU 对 shell 无条件授权（平台预授权），/data/adb 下有 ksud。
- root 读 `/proc/5473/maps` 成功：**libapp.so / libflutter.so 映射行确认存在**（模拟器上被 Permission denied 挡掉的验证补齐）。PID=5473。
- 期间发现平台网关（183.253.116.107:80）约 20:41 整体不可达（DNS 正常、MinIO 存储节点 .203:9000 存活、本机 80 出网正常）→ 命令与串流全断（串流弹「连接断开 1001」，此后截图为残留帧）。恢复手册：docs/cloud_phone_recovery_playbook.md。

### 工具链产出
- **dex 上传器**（tools/uploader/）：Uploader.java（HttpURLConnection multipart POST → /api/file/upload）→ javac(JDK17) → r8/D8（阿里云镜像 maven.aliyun.com/repository/google）→ classes.dex 3.5KB。用法：`CLASSPATH=/sdcard/classes.dex app_process / Uploader <file> <token>`（root 文件套 su -c）。目标：设备文件回传云盘，摆脱屏幕 OCR。

### 协议化路线修正（关键认知）
- **无 adb ⇒ 本机 frida 无法远控设备上的 frida-server**（frida 需 adb/网络传输层）。云真机上 frida 锚点验证不可行。
- 替代协议化路径：① 内存 dump libapp.so 验证完整性（进行中）→ ② 设备内 Reqable + root 把 CA 写进系统证书库（libflutter 只读系统目录）→ 抓 test 环境 API 明文 → ③ 结合 Blutter 静态结论（generateSign=base64(HMAC(secretKey,"k1=v1&…"))、AES 字段级加解密、sk 经 getsk 下发）用 Python 复刻协议。

## RK3588S 新设备会话（2026-09-22）：路线 ① 完结

### 设备与环境（第三代战场）
- 新设备 `125.109.27.7:56915`（公网 adb 直连）。指纹 `Nokia/rk3588s_base/rk3588s_q:10/QD4A.200805.003/eng.work.20250311`：**RK3588S 云手机伪装 Nokia X6**，内核 4.4.302-dirty（2025-04 编译），release 声称 10 但 SDK 属性全为 33（伪装 ROM，真实框架行为介于 A10/A13，/apex 为空 tmpfs）。SELinux Enforcing，adb 直接 uid=0（eng 构建 su 域）。
- 相比 TJ 云真机的决定性优势：**adb 通道 + root + `adb remount` 成功（/ 为 ext4 rw）**——本机 frida 可远控 frida-server，系统 CA 注入路线打通。
- 干净度：188 包，第三方仅 com.liuzh.deviceinfo；无 KSU/Magisk/云机助手残留。平台侧有 com.nx.lryk 连接监控守护（logcat 可见其 devinfo 上报，含 chagee 安装状态）。
- 注意：公网租用设备、adb 明文——只做学习，不登录真实支付账号。Git Bash 下 adb 设备路径需 `MSYS_NO_PATHCONV=1`，否则 `/data/...` 被转成 `E:/Git/data/...`。
- 宿主侧变化：`E:\PythonCodeObject1\APPCodeObjectdemo1` 在本机不存在（.mcp.json 的 3 个自建 Android MCP 不可用）；本机全局 Python 3.14.3 自带 frida client 16.6.6，直接以 adb forward + `add_remote_device` 模式工作。

### 部署记录
- APK：`apk/chagee_upload.apk`（SHA-256 `c4f2e82d…e2959a`，与 霸王茶姬.apk 同文件）→ 设备端哈希一致 → `pm install -r` 成功。`versionCode=638 targetSdk=35 primaryCpuAbi=arm64-v8a`（原生，无转译）。
- frida-server 16.6.6 arm64：GitHub 直连极慢（2 分钟 61KB），ghproxy.net 镜像断点续传完成（xz 完整性校验通过）。部署为 `/data/local/tmp/fs1666-arm64`，`-l 127.0.0.1:27042` **仅 loopback 监听**（公网设备必须），PID 13682。client/server 16.6.6 匹配，`verify_frida_env.py` 验证 110 进程可见。

### 路线 ①：内存 dump libapp.so 完整性验证（通过）
- 冷启动（monkey）后 MainActivity 获焦，无崩溃；logcat 复现未登录基线（`隐私政策同意状态: false`、`一键登录服务初始化完成`、`只执行基础初始化`）——与模拟器/TJ 云真机三端同构。
- **新知识点：`extractNativeLibs=false` 时 .so 不解压，直接以 base.apk 文件切片映射**。maps 里没有 `libapp.so` 路径，只有 `base.apk r--p 00bb0000` 这类行。定位方法：本地解析 APK ZIP 本地头得 `lib/arm64-v8a/libapp.so` 数据偏移 `0xbb0000`（stored、4K 对齐、12911520B），再与 base.apk 映射区间求交集。
- 用 frida `Memory.readByteArray` dump 全部 3 个 file-backed 区域并逐页对比静态 arm64 文件：
  - r--p（ELF 头+rodata）4984832B：1217/1217 页一致
  - r-xp（AOT 指令）7815168B：1908/1908 页一致
  - rw-p（数据尾段）928B（不足一页，不计页）
  - **合计 3125/3125 页 100% 一致；运行期 libapp.so 与静态分析目标逐字节相同，无运行时 patch/解密/替换**
- 产物：`docs/libapp_memory_dump_report.json`、`capture/libapp_mem_13771_*.bin`；脚本 `scripts/dump_libapp_memory.py`（支持 extracted/apk-direct 两种映射模式）、`scripts/verify_frida_env.py`。
- 结论边界：证明"运行的就是静态分析的那份 Dart snapshot"；不涉及网络行为与密钥。TJ 云真机上"只看到映射行存在"的验证在此升级为全页对拍。

### 下一步（路线 ②）
1. Reqable CA 写入 `/system/etc/security/cacerts/`（remount 已验证可行，注意 Android 13 的 conscrypt/APEX 变体——本机 /apex 为空 tmpfs，大概率走 /system 目录即可）。
2. 宿主 Reqable 代理 + 设备 wifi 指向，抓未登录启动流量，先验 getsk 下发与 header 谱系。
3. 结合 Blutter 静态结论（sign=base64(HMAC(sk,kv))、AES 字段加解密）进入路线 ③ Python 复刻。

## 路线 ② 完结：Dart 明文捕获 + getsk 实证（2026-09-22 下午）

### 实际执行与 9/10 结案的对应
- CA：宿主 mitmproxy 12.2.3（Python 3.14 用户级安装，入口 `C:/Users/Administrator/AppData/Roaming/Python/Python314/Scripts/mitmdump.exe`）自带 CA → `openssl subject_hash_old` → `/system/etc/security/cacerts/c8750f0d.0`，root:root 644，设备 curl 无 `-k` 验证 200。Reqable 未用（本机未装，mitmproxy 即"设备内 Reqable+系统 CA"路线的宿主等价实现）。
- 全局代理 `settings put global http_proxy 127.0.0.1:8080` + `adb reverse tcp:8080`：**只覆盖原生 SDK**（神策 `upload-tracking-sea.chagee.com/sa?project=mys_dev` 200×7、极光 config/ce3e75d5/bjuser/fcapi、易盾 da.dun.163.com、高德 apilocate/arestapi/logs）——与《不走流量与验证码诊断》结论 B"Dart 结构性直连"完全一致（App 无 DEBUGGABLE 标志，wrap 注入环境变量方案不可行）。
- **Dart 直连流量的解法（本轮新工程成果）**：root 写 `/system/etc/hosts` 把 8 个业务域名指向 127.0.0.1（备份 /data/local/tmp/hosts.bak）+ `adb reverse tcp:443 tcp:8443` + 宿主 `mitmdump --mode reverse:https://test-gj-api.bwcj.com/ --set connection_strategy=lazy -s scripts/sni_route_addon.py`。**坑：不加 lazy 时 upstream 在 addon 改写前就按 reverse 默认值建连**（SNI=test-gw 的请求发去了 test-gj-api → 404 假象）。此链路无需改 APK/无新二进制，可直接复用。
- 首启流程（脱代理视角）：隐私弹窗（已阅读并同意）→ 通知权限 → 定位权限（仅在使用时）→ 自取页"定位响应慢"→ 城市列表 → 门店选择。uiautomator dump 是无视觉环境下驱动 Flutter UI 的可用通道（content-desc 承载语义）。

### 路线 ② 核心收获（详见 docs/field_genealogy_20260922.md）
1. **getsk 实证闭环**：`GET test-gj-api.bwcj.com/encrypt-server/enctrypt/api/getsk?code=CHAGEE_C_001` → `data`=base64(16hex)=sk；设备 SP `flutter.test-chagee_encrypt_sp_key` 落盘值与响应一字不差，键名 `test-` 前缀同时证实运行环境=test（`initEnv` 兜底）。
2. **请求头谱系**（13 个固定头，`ua: Dart/2.12` 与 `user-agent: Dart/3.6` 并存）；未登录期 `/api/*` **无 sign 头**——sign 触发面比静态预期窄，留路线 ③。
3. **airhub 独立签名体系**：body 内 `sign`=base64(32hex)+`appId=HVRk4cIj7puaOPAB`+`timestamp`；5 组样本，md5/hmac 快速候选空间未命中（自有密钥），待反汇编。
4. test-gw 业务路由大面积 404（airhub 重试 107 次/分钟），test 环境服务已迁移；未登录明文样本已拿全。

### 下一步（路线 ③）
1. 反汇编 airhub sign 生成函数（Blutter asm 中定位 `queryList` 请求构造处）。
2. 用 getsk 的 sk 对拍主 API 的 AES 字段加解密与 sign 触发条件（需登录态或切 `c_debug_env=release` 打生产——切前需用户确认）。
3. Python 复刻（scripts/chagee_protocol.py 已有骨架）。

## 路线 ③ 第一阶段：airhub sign 完全复刻 + getsk 宿主直连（2026-09-22 下午）

### airhub sign 算法（Blutter 反汇编 → 8778 样本全对拍）
- 定位链：`pp+0xd9b8 "HVRk4cIj7puaOPAB"` → `AirHubSDK::fetchConfigFromNetwork @0x58fddc`（构造 timestamp=(micros/1000).toString()，调 `_generateSign`）→ `_generateSign @0x5900b4`。
- 算法：`sign = base64(md5_hex(join(groupKeys) + appId + "json" + timestamp + secret))`——**纯 MD5 非 HMAC**（静态旧结论"sign=base64(HMAC(...))"只适用于主 API，airhub 是独立体系）。
- 四环境凭据（appId/secret/groupKey/URL）硬编码于 `init.dart @0x806644-0x806710`，已全表入 `scripts/chagee_protocol.py:AIRHUB_ENVS`。
- 回归：`verify-airhub` 子命令对 flows 全量 **8778/8778 通过**（含 gk6 前缀样本）。教训：对拍脚本里手工抄 hex 会引入 c8/8c 转置假失配——样本必须从 flows 程序化读取。

### getsk 宿主直连复现
- 用还原的 13 固定头从宿主 Python GET getsk → 200，sk 与设备一致（`f7346022c2d57c81`）。**协议级复现首次脱离 App 独立完成**。

### test 环境端点图谱 + 响应包络
- 存活：getsk、cityList（真实数据）、pageInfo（偶发）；其余 store/list 等全 404（请求体已捕获备用）。
- 包络：`{errcode,errmsg,data,thirdTraceId,globalTicket,timestamp}`，globalTicket=32hex/响应。

### 剩余缺口
主 API sign（RT1/RT2）与 AES 字段加密（RT3-RT5）未登录期不触发；需登录态样本或切 release 打生产（切前问用户）。

## 路线 ③ 第二阶段：协议栈全破解（2026-09-22 傍晚，用户选"测试账号登录"）

### 触发与捕获
- 登录页输入占位号 19900000000（test 不投真短信）→ `POST /user-client/message/send` 捕获，新头 `uuid/cid/sk`，body 含 AES 密文 mobile/sendObj + sign。
- **AES 当场破解**：key=base64decode(sk) 16 字节 ASCII，ECB+PKCS7，`Eb4a8EnsqITU1KDkQ4YFCQ==` → `19900000000`。
- **sign 反汇编定位链**：`generateSign @0xb48da0`（sorted-kv → Hmac → base64 → trim）→ 调用方读 `extra["signFields"]/extra["secretKey"]` → `MeService::getEncryptExtra @0x8b335c` → `EncryptUtils::getSecretKey @0x8b35f0`：**硬编码双密钥**（release=`9b83…f06e`，其他=`686c…76bd`）。登录 signFields=["sendObj","sid","timestamp"]（`phone_login_bloc @0x8da140`）。
- **wire 验证**：HMAC-SHA1(686c…76bd, "sendObj=<enc>&sid=…&timestamp=…") base64 = wire 一字不差。
- **端到端**：`build_login_sms_body` 从纯输入重建请求体与 wire **字节一致**；airhub 回归增至 13285/0。

### 方法论增量
1. extra 驱动的按请求加密/签名配置（signFields/requestEncryptFields/secretKey/sk）是 field-level 逆向的正确入口——比全局搜"sign 生成"快。
2. 反汇编→假设→wire 单样本精验→端到端字节重组，是 AOT 协议复刻的最短闭环；证据边界如实标注（RT6-RT8）。
3. uiautomator content-desc 可无视觉驱动 Flutter UI（含登录输入与协议勾选）。

## 真实账号短信链路诊断 + 生产登录闭环（2026-09-22 晚）

### 短信不达的梯度定案（用户号 159****1290）
三级梯度实验：抓包全开 / 干净网络（留 CA）/ 全净（撤 CA）——服务端**每次都受理**（`errcode=0`、风控 `action:"pass"`、限流器真实计数），但短信均未到达。结合 9/10 模拟器时代异号同样现象，定案：**test 环境短信通道不对真实号码投递**（`sendFlag:true` 仅受理语义，`smSessionId:null` 为佐证），与 root/抓包/风控拦截无关。客户端不解析 `dispose/sendFlag`（pp.txt 无此业务字段），只看 errcode=="0"。

### c_debug_env=release 切换与生产真实登录（成功）
- 切换：停 App → SP 插入 `<string name="flutter.c_debug_env">release</string>`（备份 `/data/local/tmp/FlutterSharedPreferences.xml.bak`）→ 重启。验证特征：`putStringEnv` 在 release 前缀为空 → SP 新增**无前缀** `flutter.chagee_encrypt_sp_key`；生产 getsk（gj-api.bwcj.com）返回与 test 相同的 sk（CHAGEE_C_001 跨环境同密钥）。
- **生产短信真实送达**（几秒内），验证码登录成功：JPUSH 设置别名 `1190018250`（customerId，登录成功专属动作）；UI 进入会员页"Hi，茶友 / 0% 素胚象 / 1 张优惠券"；SP 落盘 `login_userLoginToken`/`login_type`（值脱敏不入档）。截图 `capture/prod-login-success-20260922.png`。
- 反向证实：生产通道可送达 ⇒ 此前不达确为 test 通道行为，非环境检测。

### 会话状态备忘（下轮接续用）
- 设备当前：release 环境（SP 键在），抓包链路全关（hosts 已还原/代理已删/CA 已撤），App 已登录真实账号。
- 若需登录态抓包：重装 CA+hosts 重定向+reverse+SNI（`tls_clienthello`+lazy 版 addon）即可；登录 token 会出现在抓包文件中，注意脱敏。
- 回退 test 环境：停 App 后删 SP 中 `flutter.c_debug_env` 键。

## 登录态链路打通 + Python 协议化终验闭环（2026-09-22 深夜）

### 还差什么 → 全部打通
本日最后两条未通链路：(1) 登录态请求协议动态面；(2) Python 独立客户端带登录态调生产。
- **Authorization 静态链**（Explore 代理定位）：`NetHeaderService.headers @0xb49c84` → `getLoginToken @0xb7f4c4` → SP `login_userLoginToken`，**裸 JWT 无 Bearer**；合并优先级 dio < 公共头 < extra.customHeaders；无 refresh（401/12320120400401 踢登录）。
- **动态实测**：重开抓包链路（CA+hosts 生产 4 域名+reverse+代理），冷重启 App 抓到 `GET gw.chagee.com/user-client/customer/userInfo/query` 200，authorization=608B JWT、sk 同值；响应 `mobileEncrypt` 用 sk key 解密成功（**RT7 动态闭环**——服务端响应加密字段实锤）。
- **Python 终验**（用户批准，只读单次）：SP 读 token → `ChageeProtocol.query_user_info()` 自构 13+3 头直调生产 → **errcode=0，customerId/等级与 App 抓包一致**。协议复刻全链路闭环：getsk→sk→AES 字段加密→HMAC-SHA1 签名→头组装→服务端接受（App+Python 双客户端验证）→响应解密。
- 生产网关无 test 专属路由（countryInfo 等直连亦 404）——环境差异以 `/user-client/*` 核心路由为准。

### 设备在位状态（用户选：全部保留）
登录态+release+抓包链路全开；回滚与重建命令见 docs/field_genealogy_20260922.md 末节。token/手机号只存在于 capture/ 与设备 SP，docs 脱敏。

### 项目终态
霸王茶姬协议栈研究的主目标（Flutter AOT 逆向 + 协议全复刻）已无遗留缺口。可选延伸：下单/购物车写接口复刻（涉及交易，另行授权）、易盾风控深挖、多账号会话管理。

## 设备 IP 漂移重连 + 复现能力回归验证（2026-09-22 深夜·第二轮）

### 触发
用户给出设备新地址 `39.174.221.6:56915`（原 `125.109.27.7:56915`，同端口同指纹=Nokia_X6 伪装的 RK3588S 重新拨号）。`adb connect` 直连成功，uid=0(root)，SDK 属性 33 / user / release-keys 与此前一致。

### 断线后状态盘点（重要工程结论）
- **保留**：App（versionCode=638）+ 登录态（SP `login_userLoginToken` 608B JWT、`login_type`）、`c_debug_env=release`、系统 CA `c8750f0d.0`、hosts 4 条生产域名重定向、`test-chagee_encrypt_sp_key` 与无前缀 sk 双键。
- **失效**：`adb reverse --list` 空、宿主 mitmdump 双实例全退、全局代理被清。hosts 在而隧道死 ⇒ App 业务流量黑洞——**断线重连后必须先重建链路再用 App**。

### 复现能力回归（全部通过，证明逆向成果可持续独立复用）
1. **游客菜单链**（宿主直连生产）：`chagee_menu_api.py --city 3301` → 首店 CN12250 快闪店（6 分类/28 商品）；`--store CN00529 --with-sku` → **14 分类/82 商品/94 SKU**，与基线一致。
2. **登录态协议链**（宿主直连生产）：设备 SP 静默取 token（不回显）→ 生产 getsk errcode=0 → `userInfo/query` **errcode=0**，customerId=1190018250、昵称"Hi，茶友"、`mobileEncrypt` 解密=159\*\*\*\*1290（与 App 内一致）。
3. Python 复现对设备零依赖（仅需 token 时读 SP）——协议复刻的独立性再次实证。

### 链路重建（顺序与验收）
宿主 mitmdump 双实例（8443 reverse+SNI `tls_clienthello`+lazy / 8080 常规）→ reverse 双端口 → 全局代理 → 探针 cityList **200** → App 冷重启 → UI dump 证实登录态首页。flows：`capture/chagee_dart_resession.flows`（173+ SNI 路由，navigation/multi-lang/skin/messaging/getsk 全套）、`capture/chagee_native_resession.flows`（Sentry/神策/易盾/淘宝 amdc）。

### 新增观察
- `api-cn.chagee.com`（airhub 生产端点）经 mitmdump 上游 TLS 握手被服务端关闭（宿主 curl 直连同端点 TLS+HTTP 正常，gw/gj-api 经隧道亦正常）——疑与该端点对代理上游指纹/高频重试的处置有关；App 侧 airhub 无限重试但核心业务不受影响（与 test 环境 airhub 404 同构的良性降级）。airhub 样本采集需走宿主直连而非隧道。




### 结案项
- **RT6 ECB 定论**：`AES::AES @0xb2237c` 默认 mode = 常量池 `AESMode@c13261`（objs.txt: index 3 "ecb"）→ `AES/ECB/PKCS7`；key = `Key.fromUtf8(utf8Decode(base64Decode(sk)))`。此 app 的 encrypt 包默认值被改为 ECB（上游默认 SIC）。
- **RT7**：响应侧构造相同（同 key 同模式）；实测响应明文（responseEncryptFields 未触发于已见接口）。
- **RT8**：3 组独立 wire 样本（...107/...157/...6999）全部命中 sign 公式，符合 ≥3 组证据规则。

### SNI 路由缺陷（本日最重要的纠错）
下午的 `request`-hook addon **从未生效**：reverse 模式自身 hook 覆盖目标，全部流量实发 test-gj-api。"test-gw 大面积 404"是该缺陷的假象——修正（`tls_clienthello` + `data.context.server.address` + `connection_strategy=lazy`）后 **test-gw 全端点 200**。教训：**代理日志里的 sni_route 打印只证明 hook 跑了，不证明路由生效；必须用"上游独占端点"验证路由**（本例 airhub 只在 gw 上存在，是最灵敏的路由探针）。

### 新端点与新知识
- `POST /user-client/auth/login/sms`：smsCode 明文、无 sign、mobile 加密；假码 → 真实错误码 `1232020200004 验证码错误`。
- message/send 真实路由下 200（sendFlag:true, action:pass）。
- airhub 服务端返回 `9002010000001 APPID错误或已停用`（test appId 停用 → App 无限重试，量级 13000+/小时）。
- adb 断线重连后：reverse 转发需重建、宿主 mitmdump 可能全部退出——每次会话先 `netstat` 验监听再用 curl 隧道探针验收。





## 动态基线执行结果（2026-09-07）

已把现有模拟器 `emulator-5554` 从 offline 状态恢复为 online，并执行干净基线：卸载旧包 → 安装原始 APK → 冷启动 → 脱敏日志。记录文件：`capture/baseline-20260907-021235.json`；日志：`capture/runtime-20260907-021321.sanitized.log`；当前画面：`capture/baseline-20260907-021445.png`。

### 已通过的验收点

- 设备：型号 `V1824A`，Android `9`，设备 ABI `x86_64`。
- 包：`com.chagee.application.cn`，`versionCode=638`、`versionName=1.0.0`。
- 安装后 `primaryCpuAbi=arm64-v8a`，说明该 x86_64 模拟器通过 native bridge 运行 APK 内 arm64 库；日志中出现 `native_bridge3_loadLibraryExt ... libflutter.so`、`libdartjni.so`、`libmmkv.so`。
- `MainActivity` 已启动并保持 resumed；日志出现 `Displayed com.chagee.application.cn/.MainActivity`。
- Flutter 未登录初始化已执行：`隐私政策同意状态: false`、`一键登录服务初始化完成`、`只执行基础初始化`。
- 截图确认落在未登录首页，底部“首页/点单/订单/我的”导航可见。
- `capture/` 只保留 sanitized 日志，没有 `*.raw.log`。

### `/proc/<pid>/maps` 回退说明

对运行 PID `4582` 读取 `/proc/4582/maps` 返回 `Permission denied`（Android 9 模拟器权限限制）。因此本轮没有伪造“已看到 libapp 映射”的结论，改用安装后的 `primaryCpuAbi`、APK 包路径、Flutter/native bridge 启动事件，以及静态 arm64 `libapp.so` SHA-256 `8b4aa0dd…7da5edd7` 作为回退证据。机器可读验收脚本为 `scripts/verify_runtime_baseline.py`，输出写入 `docs/runtime_baseline_verification.json`。

### 结论

原始签名 APK 已在模拟器完成可重复的未登录冷启动基线；没有生成或安装所谓“脱壳 APK”，也没有修改 DEX、SO 或签名。后续若要做插桩/重打包，应以本次基线日志和截图作为对照组。

## 设备换新（:58445）链路重建 + CA 命名根因（2026-09-26）

### 新设备状态
- `39.174.221.6:58445`，RK3588S 伪装 HUAWEI_NXT_AL10，Android 13(SDK 33)，**magisk** root（旧设备为 KSU）。
- **App 换为商店正式版 1.0.3 (versionCode=2060)**（旧 638/1.0.0）；协议头/签名经 userInfo 200 验证仍兼容。
- App 已由用户登录同账号（SP `login_userLoginToken` 608B JWT 今日签发，无前缀 sk 键=release 语义）；无需再注入 SP。

### 两个新根因（旧脚本在此设备失效的原因）
1. **SELinux 上下文**：deploy 脚本从 /data/local/tmp bind-mount，stage 文件带 `shell_data_file` 标签，Enforcing 下 App 域不可读 → hosts 回退 DNS、CA 不信任。修复：chcon `u:object_r:system_file:s0`。
2. **CA 文件命名（本日最关键）**：Android/OpenSSL 按subject_hash_old（**MD5** 前 4 字节小端）索引证书，脚本写的 `reqable_ca.0` 从未被索引；mitmproxy CA 的正确名 = **`c8750f0d.0`**（与旧设备回滚记录吻合，此前误以为是别的 CA）。命名修正后 curl 默认信任 200、App 全链 TLS 通过。
3. 附：本机 / 为 ext4 可 remount rw——最终方案弃用 bind-mount，**直接落盘** /system/etc/security/cacerts/c8750f0d.0 + /system/etc/hosts 追加 2 域名（gw.chagee.com、gj-api.bwcj.com；api-cn.chagee.com 不重定向，其 airhub 流量已迁至 gw 域 `chagee-airhub-config-server` 并正常 200，无重试风暴）。

### 链路终态（在位，走查结束后回滚）
- 宿主：mitmdump 8443 `--mode reverse:https://gw.chagee.com/ --set connection_strategy=lazy -s scripts/sni_route_addon.py -w capture/chagee_phase0b_order_20260926.flows`；mitmdump 18080 常规 `-w capture/chagee_native_phase0b_20260926.flows`（宿主 8080 被 java.exe 占用，故 18080）。
- 设备：`adb reverse tcp:443 tcp:8443` + `tcp:8080 tcp:18080`；`settings put global http_proxy 127.0.0.1:8080`。
- 验收：cityList/userInfo/decoration 全 200（Dart 解密）；sentry 200（原生）；UI=登录态首页"Hi，茶友/7 优惠券"。
- 回滚（走查后执行）：`settings delete global http_proxy`；`adb reverse --remove-all`；`su -c 'rm /system/etc/security/cacerts/{c8750f0d.0,reqable_ca.0}；cp /data/local/tmp/hosts.new 的反向——直接 sed 删 hosts 两行'；mount -o remount,ro /`。

### 券策略定案（Phase 5 前置）
- effective-list 实测 7 张全为 20 元代金券 bizType=1：**5 张 DN 模板无门槛（thresholdTips 空）**、1 张 HYW 无门槛、1 张 DX 满 20 可用；有效期至 10 月下旬。
- 方案：**无门槛 20 元代金券 + ≤20 元饮品 = 真 0 元**（替代原"饮品兑换券"方案，账号无兑换券）；CN00529 快照 ≤20 元 SKU 例：云中绿中杯 12 / 花田坞中杯 13 / 轻因·折桂令中杯 14。

## 支付宝支付全生命周期 wire 实证（2026-09-26，¥8 单支付成功闭环）

- **订单状态枚举（wire 定案）**：1=待支付、3=制作中（支付成功即翻转，pickupNo=TA0001 同步下发）、6=已完成、7=已取消。orderStatusText 服务端下发。
- **autoCancel 实证**：¥10 单 16:40:50 下单（paymentExpiryTime=16:50:50, paymentExpiryType=autoCancel）→ 16:52:54 轮询见 s7。CHAGEE 侧窗口 10 分钟 < 支付宝 orderStr timeout_express=30m，有效窗口取 min。
- **continuePay 实证（重新拉起支付）**：请求 `{tcode:"CHAGEE", userId, orderNo, channelCode:"UnionPay", payType:60}`（与汇编还原完全一致）；响应与 createOrder 同构（OrderCreateModel：全新 payUrl.orderStr + 新 out_trade_no + payNo），**重签支付串免走购物车**。订单列表/详情两处入口（order_list_bloc/order_detail_bloc @0x9c232c/@0x9bb330）。
- **支付宝渠道无独立 commitPay**：payUrl 直接由 createOrder/continuePay 返回；收银台会话 = mclient.alipay.com cashierRoutePay → h5pay/landing；mobilegw mgw.htm 为支付宝应用层加密（mcpay），不可解密也不需要。
- **getWaitingInfo 首个 wire 样本**：`{waitingCups, waitingTime(s), queueLimit}` — Phase 6 输入。
- 产物：output/alipay_pay_link_20260926.json（首单支付串）、output/pay_lifecycle_20260926.json（全周期+continuePay 样本）、output/trade_samples_20260926.json（购物车/试算）。

## 0 元单闭环定案（2026-09-26，20 元券抵扣 ¥20 饮品实测）

- **0 元单完全跳过支付环节**：createOrder 请求 `paymentInfo = {payerId, payType:null, channelCode:null, currencyType:156, payAmount:"0"}`，券入 discountList（discountId=券 couponCode，discountAmount="20"）；**响应 data 仅 `{orderNo}`——无 payUrl/payNo/commitPay/支付宝跳转**。下一跳 getOrderDetail 即 s3 制作中 + pickupNo（TA0001，与 ¥8 单同码，疑为门店内日序列）。
- 自动化分支判定：`payAmount=="0"` → createOrder 即成单；`payAmount>0` → createOrder/continuePay 返回 payUrl → 支付 → getOrderStatus 轮询。
- 六功能抓包战役至此样本齐备：settlePrice/createOrder(付费+0元)/continuePay/getOrderList/getOrderDetail/getOrderStatus/getWaitingInfo/券三列表。仅 cancelOrder 与 commitPay 无样本（静态已知，非六功能必需）。
- 产物：output/pay_zero_capture_20260926.json（0元 createOrder 全文）。

## Phase 5 核心落地：支付场景引擎 chagee_trade_api.py（2026-09-26，多智能体协作完成）

- **模块**：scripts/chagee_trade_api.py——三场景分类（classify: buyerRealPrice "0"→零元/否则差额）、券匹配（pick_coupon：无门槛+有效期过滤、覆盖取最小面额、差额取最大面额、抵扣 min(面额,总额)）、createOrder 双分支构造（零元 payType/channelCode=null；差额 60/UnionPay）、PayLink 解析（payUrl 内嵌 JSON→orderStr→out_trade_no/total_amount/time_expire）、三向一致性断言（settle==构造==orderGroup 的 buyerRealPrice；ΣdiscountAmount==totalDiscountAmount）、continuePay 续付、wait_status 轮询（getOrderStatus 轻探针 2s 起 1.5×退避）、verify 核销校验（payAmount+promotionId==couponCode）、OrderHangError 防悬挂语义（createOrder 无幂等键，超时先查单）。
- **测试**：tests/test_trade_api.py 20/20 离线全过（wire 回放三存档夹具；过期时间用固定时间戳避免依赖运行日期）；连同 test_menu_api 26 项回归全绿。
- **文档/勘误**：docs/pay_scenarios_design_20260926.md 定稿；BIZ_TYPE_ORDER 2→1 勘误（wire 定案，下单券域 order-coupon-list 的 int 2 为另一端点不受影响）。
- **轮询行为实证补充**：待支付期无后台心跳——页面/事件驱动（进入订单页 detail×2+list×3；到期前 1s 有一次 detail；支付宝收银台期间 380s 零轮询）；支付完成后 detail 一次带回状态+pickupNo，取餐页 getWaitingInfo 2-3s/拍（waitingCups/waitingTime/queueLimit 动态变化）。取餐码为门店级日序列（同店同日 TA0001→TA0002）。

## 「网络异常，请稍后重试」= trade 校验兜底文案定案：门店打烊后 settle 必败（2026-09-27 凌晨）

- **事件**：工作台下单报 `[82041201] 网络异常，请稍后重试 (trace=)`；同刻云手机 App「选择门店」页「网络不给力哦」。
- **云手机侧（独立故障，第 3 次）**：adb 断线 → reverse 隧道全丢 + hosts 劫持/全局代理残留 → 业务域名黑洞。重建 `adb reverse tcp:443 tcp:8443 / tcp:8080 tcp:18080` 后全恢复；mitmdump 双进程全程存活。
- **协议侧定案**：兜底文案下错误码不固定（本次 91010009 复现 / 用户 82041201 / 历史 App commitPay 8202020200007）——**码无意义，看文案+阶段**。三跳探针（whoami✅→calculatePrice✅→settlePrice❌）锁定失败在 settlePrice；同分钟真实 App 在 CN07078（05:00–04:00 跨夜营业）settle 成功而探针在 CN08121（09:00–22:29 已打烊）失败；**换 CN07078 重试立即成功（22 元/券后 2 元/confirmOrderKey 正常）**。昨晚 22:28 成功单恰在 CN08121 打烊前 1 分钟。排除签名/IP 风控/token。
- **改动**：orders.py `_fail` 对"网络异常"文案追加门店打烊提示；新增只读探针 scripts/probe_82041201.py（三跳定位，绝不 createOrder）。定案文档：docs/order_error_82041201_20260927.md。
- **运维规则**：凌晨下单选跨夜门店（CN07078 衡阳南华大学东门店）或营业时段；营业时间字段 `store/detail → businessInfo.diningInfo.runningTime`（`[{"worktime":[{"time":["HH:MM","HH:MM"]}],"workweek":[1-7]}]`，注意跨夜区间换算）。
