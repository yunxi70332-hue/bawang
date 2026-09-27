#!/usr/bin/env python3
"""云手机 WebView 幽灵代理恢复器：PC 起转发代理 + adb reverse tcp:8080 接管。

背景（2026-09-27 定案）：云手机茶姬 App 的收银台 WebView 持续报
net::ERR_PROXY_CONNECTION_FAILED——它固定走 127.0.0.1:8080 代理（昨天的抓包链路
残留：当时 8080 有监听所以支付正常，今天监听消失即断网）。系统层五键/WifiConfig/
LinkProperties 全部无代理，清 WebView 数据无效——代理配置在 App/SDK 层。

本脚本 = 复刻昨天的链路：
  1. PC 上 127.0.0.1:18080 起纯转发代理（CONNECT 隧道 + 绝对 URI GET 转发）
  2. adb reverse tcp:8080 tcp:18080（手机侧 127.0.0.1:8080 → PC 18080）
  3. WebView 流量恢复出网；CONNECT 行同时留下主机清单日志（顺带观测）

用法：python scripts/restore_webview_proxy.py [--port 18080] [--quiet]
停止：Ctrl+C（退出时自动 adb reverse --remove tcp:8080）
"""
import argparse
import select
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime

ADB = r"C:\platform-tools\adb.exe"
DEV = "125.109.27.7:58445"
hosts = Counter()
lock = threading.Lock()


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def handle(client):
    try:
        client.settimeout(15)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = client.recv(65536)
            if not chunk:
                return
            req += chunk
            if len(req) > 65536:
                return
        first = req.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        method, target = first.split(" ", 2)[:2] if " " in first else ("", "")
        if method == "CONNECT":
            host, _, port = target.partition(":")
            with lock:
                hosts[f"{host}:{port or 443}"] += 1
                log(f"[tunnel] {target}")
            # 上游短超时（6s）：mclient 多 CDN 边缘节点中偶有不可达 IP，
            # 快速失败让 Chromium 重试换节点（15s 硬等会把 WebView 请求拖死，2026-09-27 实证）
            upstream = socket.create_connection((host, int(port or 443)), timeout=6)
            upstream.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            pipe(client, upstream)
        else:
            # 绝对 URI 的明文代理请求（http://...）
            from urllib.parse import urlsplit
            u = urlsplit(target)
            host = u.hostname or ""
            port = u.port or 80
            with lock:
                hosts[f"{host}:{port}"] += 1
                log(f"[http] {method} {target[:100]}")
            upstream = socket.create_connection((host, port), timeout=15)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            headers = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
            upstream.sendall(headers.encode())
            client.settimeout(None)
            pipe(client, upstream)
    except Exception as e:
        log(f"[error] {type(e).__name__}: {e}")
        try:
            client.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(64)
    log(f"转发代理就绪 127.0.0.1:{args.port}")

    # adb reverse：手机 127.0.0.1:8080 → PC 本端口
    subprocess.run([ADB, "-s", DEV, "reverse", "--remove", "tcp:8080"], capture_output=True)
    r = subprocess.run([ADB, "-s", DEV, "reverse", "tcp:8080", f"tcp:{args.port}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"adb reverse 失败: {r.stderr.strip()[:120]}")
        return 1
    log("adb reverse 已建：手机 tcp:8080 → PC "
        f"tcp:{args.port}（若 WebView 固定走 127.0.0.1:8080，此刻即恢复出网）")

    def reporter():
        while True:
            time.sleep(30)
            with lock:
                if hosts:
                    snapshot = dict(hosts)
            if snapshot:
                log(f"[30s 汇总] {snapshot}")

    if not args.quiet:
        threading.Thread(target=reporter, daemon=True).start()

    try:
        while True:
            client, _ = srv.accept()
            threading.Thread(target=handle, args=(client,), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        subprocess.run([ADB, "-s", DEV, "reverse", "--remove", "tcp:8080"], capture_output=True)
        log(f"退出（已移除 reverse）。累计主机: {dict(hosts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
