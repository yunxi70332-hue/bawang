#!/usr/bin/env python3
"""DevTools 收银台协议链录制器：全程明文记录 WebView 的每个请求/响应。

原理：mclient 收银台跑在茶姬 App 的 WebView（com.alipay.sdk.app.H5PayActivity）里，
WebView 的 Chromium 调试协议（webview_devtools_remote socket）可直接开启 Network 域，
逐事件拿到 request URL/method/headers/POST body 与 response 状态/响应体——无需 MITM、
无需证书，纯观测（只读，不注入、不点击、不代付）。

用法：python scripts/capture_cashier_devtools.py [--port 9231] [--minutes 30]
输出：output/cashier_devtools_capture_<时间戳>.json（requests[] 逐条 + console[]）
"""
import argparse
import datetime
import json
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request

ADB = r"C:\platform-tools\adb.exe"
DEV = "125.109.27.7:58445"


def sh(*args, timeout=10):
    try:
        return subprocess.run([ADB, "-s", DEV] + list(args), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except Exception:
        return None


def ensure_forwards():
    r = sh("shell", "cat /proc/net/unix")
    socks = sorted(set(re.findall(r"(webview_devtools_remote_\d+)", (r.stdout if r else "") or "")))
    ports = {}
    for i, s in enumerate(socks):
        port = 9230 + i
        sh("forward", f"tcp:{port}", f"localabstract:{s}")
        ports[s] = port
    return ports


class WS:
    def __init__(self, port, path):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.s.send((f"GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                     "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                     "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        assert b"101" in self.s.recv(4096).split(b"\r\n", 1)[0], "WS 握手失败"
        self.buf = b""

    def send(self, obj):
        d = json.dumps(obj).encode()
        m = b"\x11\x22\x33\x44"
        h = b"\x81"
        n = len(d)
        if n < 126:
            h += bytes([0x80 | n])
        elif n < 65536:
            h += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            h += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.s.send(h + m + bytes(b ^ m[i % 4] for i, b in enumerate(d)))

    def recv(self, timeout=5):
        self.s.settimeout(timeout)
        while True:
            # 先从缓冲里解完整帧
            while len(self.buf) >= 2:
                ln = self.buf[1] & 0x7F
                off = 2
                if ln == 126:
                    if len(self.buf) < 4: break
                    ln = struct.unpack(">H", self.buf[2:4])[0]; off = 4
                elif ln == 127:
                    if len(self.buf) < 10: break
                    ln = struct.unpack(">Q", self.buf[2:10])[0]; off = 10
                if len(self.buf) >= off + ln:
                    payload = self.buf[off:off + ln]
                    self.buf = self.buf[off + ln:]
                    try:
                        return json.loads(payload.decode())
                    except Exception:
                        continue
                break
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk


SENSITIVE = re.compile(r"(password|passwd|pwd|spwd|token|secret|cvv|cvn)", re.I)


def scrub(d):
    """脱敏：值只保留前后 4 位 + 长度。"""
    if isinstance(d, dict):
        return {k: (f"{str(v)[:4]}...{str(v)[-4:]}(len={len(str(v))})" if SENSITIVE.search(k) and v
                    else scrub(v)) for k, v in d.items()}
    if isinstance(d, list):
        return [scrub(x) for x in d]
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9231)
    ap.add_argument("--minutes", type=int, default=30)
    args = ap.parse_args()

    ensure_forwards()
    pages = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json", timeout=4).read().decode())
    page = next(p for p in pages if p.get("webSocketDebuggerUrl"))
    print(f"[{datetime.datetime.now():%H:%M:%S}] 目标页面: {page.get('title')} | {page.get('url', '')[:80]}", flush=True)
    path = page["webSocketDebuggerUrl"].split(f":{args.port}", 1)[1]
    ws = WS(args.port, path)

    reqs: dict[str, dict] = {}
    order: list[str] = []
    console: list[str] = []
    urls_seen: set[str] = set()

    ws.send({"id": 1, "method": "Network.enable", "params": {"maxTotalBufferSize": 20_000_000,
                                                              "maxResourceBufferSize": 10_000_000}})
    ws.send({"id": 2, "method": "Runtime.enable"})
    ws.send({"id": 3, "method": "Page.enable"})

    deadline = time.time() + args.minutes * 60
    next_save = time.time() + 15
    body_pending: list[tuple[str, int]] = []
    next_cmd = 10

    def save(out_path):
        data = {
            "meta": {"page": {"title": page.get("title"), "url": page.get("url")},
                     "recorded_at": datetime.datetime.now().isoformat(),
                     "request_count": len(order)},
            "requests": [reqs[rid] for rid in order if rid in reqs],
            "console": console[-200:],
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(scrub(data), f, ensure_ascii=False, indent=1)

    out_path = (f"output/cashier_devtools_capture_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
    print(f"[rec] 开始录制（{args.minutes} 分钟，滚动落盘 {out_path}）——请在云手机上继续操作收银台…", flush=True)

    while time.time() < deadline:
        m = ws.recv(timeout=5)
        now = time.time()
        if m:
            method, params, mid = m.get("method"), m.get("params") or {}, m.get("id")
            if method == "Network.requestWillBeSent":
                rid = params["requestId"]
                r = params["request"]
                entry = reqs.setdefault(rid, {"requestId": rid})
                entry.update({
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "url": r.get("url"), "method": r.get("method"),
                    "headers": scrub(r.get("headers") or {}),
                    "post_data": (r.get("postData") or "")[:4000],
                    "type": params.get("type"),
                    "initiator": (params.get("initiator") or {}).get("type"),
                })
                if rid not in urls_seen:
                    urls_seen.add(rid)
                    order.append(rid)
                print(f"[req] {r.get('method')} {r.get('url', '')[:110]}", flush=True)
            elif method == "Network.responseReceived":
                rid = params["requestId"]
                entry = reqs.setdefault(rid, {"requestId": rid})
                resp = params["response"]
                entry.update({"status": resp.get("status"), "resp_headers": scrub(resp.get("headers") or {}),
                              "mime": resp.get("mimeType")})
                if "json" in (resp.get("mimeType") or "") or "text" in (resp.get("mimeType") or ""):
                    body_pending.append((rid, 0))
            elif method == "Network.loadingFailed":
                rid = params["requestId"]
                reqs.setdefault(rid, {"requestId": rid})["error"] = params.get("errorText")
            elif method == "Runtime.consoleAPICalled":
                text = " ".join(str(a.get("value", "")) for a in params.get("args", []))
                console.append(f"{params.get('type')}| {text[:300]}")
        # 拉响应体（每次最多 3 个，防洪）
        for rid, attempt in body_pending[:3]:
            body_pending.remove((rid, attempt))
            ws.send({"id": next_cmd, "method": "Network.getResponseBody",
                     "params": {"requestId": rid}})
            reqs[rid]["_body_cmd"] = next_cmd
            next_cmd += 1
        if m and m.get("id") and m["id"] >= 10:
            rid = next((r for r, v in reqs.items() if v.get("_body_cmd") == m["id"]), None)
            if rid:
                body = (m.get("result") or {}).get("body")
                if body:
                    reqs[rid]["resp_body"] = body[:6000]
                reqs[rid].pop("_body_cmd", None)
        if now > next_save:
            save(out_path)
            next_save = now + 15
    save(out_path)
    print(f"[rec] 结束：{len(order)} 请求已存 {out_path}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
