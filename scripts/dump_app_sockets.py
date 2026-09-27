#!/usr/bin/env python3
"""转储某个 App 进程的真实网络连接，用来判断它到底走没走代理。

为什么需要它：Android 的 `settings put global http_proxy` 只对"愿意读系统代理"
的 App 生效。Flutter/Dart 的 `findProxyFromEnvironment` 读的是**环境变量**
（`http_proxy`/`no_proxy`），在 Android 上根本没设置，所以 Dart 流量往往直连。
代理是否生效，不能看 App "还能不能用"，必须看内核连接表里连的是谁。

用法:
    python scripts/dump_app_sockets.py --serial emulator-5554 \
        --package com.chagee.application.cn --samples 10 --interval 1

判读：
  * remote 出现 `<代理IP>:<代理端口>`  → 该 App 走代理
  * remote 出现 `<公网IP>:443`        → 直连，绕开了代理
  两者同时出现，说明该 App 内部既有走代理的组件（通常是 Java/OkHttp），
  也有绕开代理的组件（通常是 Dart）。

只读操作，不改设备、不解析报文内容。
"""

import argparse
import subprocess
import sys
import time

TCP_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")
# /proc/net/tcp 的字段数比表头词数少（表头把 tr 和 tm->when 拆成两词），
# 实测数据行为: 0=sl 1=local 2=remote 3=st 4=tx:rx 5=tr:tm 6=retrnsmt 7=uid 8=timeout 9=inode
IDX_LOCAL, IDX_REMOTE, IDX_STATE, IDX_UID = 1, 2, 3, 7

TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
    "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
    "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING",
}


def adb(serial, *args, binary=False):
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    out = subprocess.run(cmd, capture_output=True, check=False)
    if binary:
        return out.stdout
    return out.stdout.decode("utf-8", "replace")


def resolve_uid(serial, package):
    """解析 uid。优先 `pm list packages -U`（输出 `package:X uid:10079`，一次到位），
    再退回 `dumpsys package`。dumpsys 那条在 adb 守护进程刚重启时容易被打断。"""
    txt = adb(serial, "shell", f"pm list packages -U {package}")
    for line in txt.splitlines():
        if package not in line:
            continue
        for token in line.replace("=", " ").replace(":", " ").split():
            if token.isdigit():
                return int(token)

    txt = adb(serial, "shell", f"dumpsys package {package} | grep -m1 userId=")
    for token in txt.replace("=", " ").split():
        if token.isdigit():
            return int(token)
    return None


def decode_endpoint(field):
    """把 /proc/net/tcp{,6} 的地址字段还原成可读 `ip:port`。

    坑：内核按 **32 位字、宿主字节序（小端）** 打印地址，不是按 2 字节组。
    IPv4 行反 4 字节即可；IPv6 行必须先按 4 字节字各自翻转，再判断是否 v4-mapped，
    否则会解出 `0:0:0:0:ffff:0:10f:ac10` 这种垃圾地址。
    """
    ip_hex, port_hex = field.split(":")
    raw = bytes.fromhex(ip_hex)
    if len(raw) == 4:                                   # IPv4：反转 4 字节
        ip = ".".join(str(b) for b in raw[::-1])
    else:                                               # IPv6：逐 32 位字反转
        le = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        if le[:12] == b"\x00" * 10 + b"\xff\xff":       # ::ffff:a.b.c.d
            ip = ".".join(str(b) for b in le[12:])
        else:
            groups = [le[i] << 8 | le[i + 1] for i in range(0, 16, 2)]
            ip = ":".join(f"{g:x}" for g in groups)
    return f"{ip}:{int(port_hex, 16)}"


def snapshot(serial, uid):
    txt = adb(serial, "shell", f"su -c 'cat {' '.join(TCP_TABLES)}'")
    found = {}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 10 or not parts[0].rstrip(":").isdigit():
            continue
        if not parts[IDX_UID].isdigit() or int(parts[IDX_UID]) != uid:
            continue
        try:
            key = (
                decode_endpoint(parts[IDX_LOCAL]),
                decode_endpoint(parts[IDX_REMOTE]),
                TCP_STATES.get(parts[IDX_STATE], parts[IDX_STATE]),
            )
        except ValueError:
            continue
        found[key] = found.get(key, 0) + 1
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--package", default=None, help="按包名解析 uid")
    ap.add_argument("--uid", type=int, default=None, help="直接给十进制 uid")
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    uid = args.uid
    if uid is None:
        if not args.package:
            print("[x] 需要 --package 或 --uid")
            return 2
        uid = resolve_uid(args.serial, args.package)
        if uid is None:
            print(f"[x] 解析不到 {args.package} 的 uid")
            return 2
    print(f"[i] uid = {uid} (0x{uid:04X}), 采样 {args.samples} 次")

    total = {}
    for i in range(args.samples):
        total.update(snapshot(args.serial, uid))
        if i < args.samples - 1:
            time.sleep(args.interval)

    if not total:
        print("[!] 没抓到该 uid 的 socket —— 可能 App 已退出，或此刻没有连接。")
        print("    提示：采样期间主动操作 App（切页/下拉刷新）才能看到连接。")
        return 0

    print(f"\n{'x次数':<6} {'local':<24} {'remote':<24} state")
    for (local, remote, state), n in sorted(total.items(), key=lambda kv: -kv[1]):
        print(f"{n:<6} {local:<24} {remote:<24} {state}")
    print(f"\ndistinct = {len(total)}")
    print("判读：remote 是代理地址=走代理；remote 是公网 :443=绕开代理直连。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
