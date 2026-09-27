#!/usr/bin/env python3
"""极简代理探测：监听一个端口，打印每个连接的请求目标主机。

用途：在给模拟器配置系统代理之前，先确认目标 App 是否真的会把流量交给
系统代理。收到形如 `CONNECT api.example.com:443 HTTP/1.1` 的请求，说明该
App 走的是显式代理；完全收不到连接，说明它绕开了系统代理（Flutter/Dart
的常见行为），需要改用其他重定向手段。

只打印主机名/端口，不解析、不落盘任何请求正文。
用法:
    python scripts/proxy_probe.py --port 8080 --seconds 90
"""

import argparse
import socket
import sys
import threading
import time

HOSTS = {}
LOCK = threading.Lock()


def handle(conn, addr):
    try:
        conn.settimeout(5)
        data = conn.recv(2048)
        first = data.split(b"\r\n", 1)[0].decode("latin1", "replace") if data else "<empty>"
        with LOCK:
            HOSTS[first] = HOSTS.get(first, 0) + 1
            n = HOSTS[first]
        print(f"{time.strftime('%H:%M:%S')} {addr[0]:>15}  x{n}  {first}", flush=True)
        conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {addr[0]} {type(exc).__name__}: {exc}", flush=True)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--seconds", type=int, default=0, help="0 = 一直监听，直到被终止")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(64)
    srv.settimeout(1.0)
    print(f"[probe] listening on 0.0.0.0:{args.port}", flush=True)

    deadline = time.time() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.time() < deadline:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()

    print(f"[probe] total distinct requests: {len(HOSTS)}", flush=True)
    for line, n in sorted(HOSTS.items(), key=lambda kv: -kv[1]):
        print(f"  x{n:<4} {line}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
