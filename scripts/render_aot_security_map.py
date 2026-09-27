#!/usr/bin/env python3
"""Render an offline Markdown explanation for the extracted Flutter AOT map."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("map", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    data = json.loads(args.map.read_text(encoding="utf-8"))
    artifact = data["artifact"]
    lines = [
        "# 霸王茶姬 Flutter AOT 脱壳与网络链路学习记录",
        "",
        "## 结论",
        "",
        "APK 的业务主体是 Flutter AOT：`libapp.so` 并非传统 Java 壳中可直接还原的 DEX 业务代码。已通过 Blutter 为 arm64 产出可导航的 Dart AOT 恢复结果（伪 Dart + ARM64 地址），可视为本次“脱壳/符号恢复”的完成产物。",
        "",
        "- 原始 arm64 `libapp.so`：`%s`" % artifact["libapp"],
        "- SHA-256：`%s`" % artifact["sha256"],
        "- 大小：%s bytes" % artifact["bytes"],
        "- 输出目录：`so_analysis/blutter_out/`，包含 `asm/`、`pp.txt`、`objs.txt`、`ida_script/` 和 `blutter_frida.js`。",
        "",
        "## 为什么这叫 AOT 脱壳/恢复，而不是直接拿到源码",
        "",
        "Flutter release 包把 Dart 编译为 AOT ARM64 指令。Blutter 使用相同 Dart VM 版本解析 snapshot/object pool，再把包路径、类、函数和指令地址组织成可读文件。恢复结果用于定位；函数名、类型和伪代码仍要与原始 `libapp.so` 和脱敏运行日志交叉验证。",
        "",
        "## 已确认的网络链路锚点",
        "",
        "### 请求侧（观察到的调用职责）",
        "",
        "`ChageeInterceptor.onRequest` → `_handleRequestPostBody` → `_generateSign` / `buildEncryptedDataFromOriginal` → `_handleRequestHeaders` → `_handleCustomBaseUrl`。",
        "",
        "这说明网络层将请求体处理、签名、可选加密、统一 header 合并及可选 base URL 覆盖放在同一个 Dio interceptor 中。这里是理解请求构造的主入口，但当前记录只保留函数边界和字段名，不保存真实 token、设备标识或加密材料。",
        "",
        "### 响应侧（观察到的调用职责）",
        "",
        "`ChageeInterceptor.onResponse` → `_decryptResponseIfNeeded` → `decryptFieldsInResponse`。恢复指令中可见 `base64Decode`、UTF-8 decode、`AES`、`Encrypted.fromUtf8` 和对 Map/List 的递归遍历。因此可以确认客户端存在按配置字段选择性解密响应字段的代码路径；算法模式、key、IV 与触发条件仍需用脱敏抓包/运行时观测确认。",
        "",
        "## 地址与字段证据",
        "",
    ]
    for entry in data["files"]:
        lines += [f"### `{entry['file']}`", "", "| 函数 | ARM64 RVA | size |", "|---|---:|---:|"]
        for name, meta in entry["methods"].items():
            lines.append(f"| `{name}` | `{meta['address']}` | `{meta['size']}` |")
        literals = entry["selected_literals"]
        if literals:
            lines += ["", "保留的结构字段：" + "、".join(f"`{x}`" for x in literals) + "。"]
        lines.append("")
    lines += [
        "## 登录状态模型（静态证据）",
        "",
        "登录服务含 `initOneClickLogin`、`preGetPhoneNumber`、`_tryOneClickLoginOrFallback`、`_callOneClickLoginAPI`、`_processLoginSuccess`、`cleanToken` 等入口。令牌模型中可见 `customerId`、`accessToken`、`token`、`newUser`、`firstLogin` 字段；这些字段只说明客户端状态模型，不代表可离线伪造服务端登录。",
        "",
        "## 本轮工具问题与解决记录",
        "",
        "1. Blutter Windows 安装要求使用 Visual Studio 的 x64 Native/Developer command prompt；本机普通 PowerShell PATH 缺 `cmake`/`ninja` 时会直接报 `FileNotFoundError`。",
        "2. 本机 CMake 4 对上游模板中的空 `CMAKE_CXX_FLAGS` 触发 `string(REPLACE requires at least four arguments)`；本地工具副本通过为 C/C++ flags 传入空白占位继续配置。",
        "3. 编译环境还需要 Windows SDK 的 `rc.exe`/`mt.exe`。从 VS Developer prompt 运行后可被 CMake 发现。",
        "4. 上游 CMake/Ninja + MSVC 对包含中文的绝对路径生成 PCH 时发生 ANSI 路径解码错误（`pch.h` 找不到）。将 Blutter 构建和输出暂存到 ASCII 路径 `C:\\blutter_work` / `C:\\blutter_out` 后构建成功，再把最终结果复制回本工作区。",
        "",
        "GitHub 参考：Blutter 上游仓库 `https://github.com/worawit/blutter`（本地工具版本 commit `4a60ac6`）。README 的 Windows 前置条件与本次工具链修复一致；本轮未依赖第三方业务 APK 示例或泄露数据。",
        "",
        "## 下一步（安全的学习验证）",
        "",
        "1. 用现有 `scripts/capture_runtime.ps1` 只采集未登录启动流程的脱敏 Logcat，确认 Flutter engine、网络初始化与一键登录预取的发生顺序。",
        "2. 结合 `asm/chagee_base_network/network/chagee_interceptor.dart` 的 RVA，在 IDA/Ghidra 打开原始 arm64 `libapp.so`，验证 call target 与函数范围。",
        "3. 有测试账号或已脱敏 HAR 后，只建立登录前/登录后状态机和字段字典；抓包文件进入 `capture/` 前必须先跑 `scripts/sanitize_log.py`。",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
