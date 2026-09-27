# 云真机（TJF10128022）环境暴露面基线审计

- 日期：2026-09-21
- 设备：云手机 # 8179（4G TJ 云真机），剩余约 19 时
- 通道：H5 自定义命令（uid=2000 shell，无 root）+ 控制窗口屏幕读取（`am start VIEW text/plain` + 截图）
- 用途：作为霸王茶姬 APK 脱壳学习前的环境隐藏对照基线

## 属性面（getprop）

| 属性 | 值 | 评估 |
|---|---|---|
| ro.build.version.release | 13 | Android 13 |
| ro.build.version.sdk | 33 | |
| ro.product.cpu.abi | arm64-v8a | ✅ 原生 ARM64（frida 可原生跑） |
| ro.product.model | K61L-F0 | 平台已伪装荣耀机型 |
| ro.product.brand / manufacturer | HONOR / HUAWEI | ✅ |
| ro.build.tags | release-keys | ✅ 非 test-keys |
| ro.debuggable | （空） | ✅ 非 debuggable |
| ro.kernel.qemu | （空） | ✅ 无 qemu 特征 |
| ro.build.type | user | ✅ |
| ro.build.version.security_patch | 2023-11-05 | |

结论：属性面干净，云真机是真 ARM 硬件容器，无模拟器特征，无需属性层隐藏。

## Root / 注入暴露面（shell 视角）

| 检查项 | 结果 | 评估 |
|---|---|---|
| /system/bin/su 等 4 条标准 su 路径 | 全部 No such file | ✅ KSU 已隐藏 su 二进制 |
| which su | 无 | ✅ |
| /data/adb | **存在（Permission denied）** | ⚠️ 存在性本身是 root 指纹（非 root 设备应为 No such file） |
| KSU 管理器包名（me.weishu.kernelsu 等） | 包列表不可见 | ✅ 已隐藏/未装 |
| settings get global adb_enabled | **1**（本轮为调试开启） | ⚠️ 待隐藏阶段关闭 |
| xposed/lsposed/frida/抓包工具包名 | 无命中 | ✅ |

## 敏感包名（pm list packages 过滤）

- `com.android.adbkeyboard` — 平台「分发」功能用的 ADB 输入法（已知工具指纹，中风险）
- `com.android.fileexplorer`（及 -1/-2/-3/-4 变体，共 5 份）— 平台文件管理器
- `com.tjphone.cloudphonehelper`（含变体）— 云手机平台助手 ⚠️ 云机最直接指纹，平台功能依赖，暂不移除，记为残留风险

## 检测面对照（来自本地静态分析结论）

App 自身业务层无 root/反调试/注入检测；检测集中在：
- 网易易盾（`com.netease.nis`，运行时访问 da.dun.163.com / ye.dun.163.com）— 设备指纹 + 风控上报
- 极光推送（su 路径 / test-keys / 调试器检查）、支付宝 SDK（su 路径）、Sentry（RootChecker）、神策

→ 隐藏目标：让上述 SDK 的环境探测拿不到明确 root/调试证据；属性面已天然干净，重点是 adb_enabled、/data/adb 存在性、云机助手包名。

## 方法论备注

1. 平台自定义命令行**不回显 stdout、不区分退出码**（`ls 不存在路径` 也报执行成功）→ 输出通道 = `> /sdcard/x.txt` + `am start -a android.intent.action.VIEW -d file:///sdcard/x.txt -t text/plain` + 控制窗口截图（Android 13 上 file:// VIEW 对 shell 可用）。
2. 指令菜单是 hover+toggle 混合行为；「自定义命令」菜单项是特殊的 `.menu-item.custom-command`（无 marquee-wrap），其余项用 `.marquee-wrap[title=…]` 定位。
3. 命令身份 uid=2000(shell)：后续 root 操作需走 KSU 授权（预计屏幕弹授权框，经控制窗口点允许）。
