#!/usr/bin/env python3
"""一次性附加运行 dump_webviews.js 并打印结果。"""
import frida
import sys
import time

DEVICE_SERIAL = "125.109.27.7:58445"
PKG = "com.chagee.application.cn"
SCRIPT_PATH = r"C:\baidunetdiskdownload\霸王茶姬\frida\scripts\dump_webviews.js"

def on_message(message, data):
    if message.get("type") == "send":
        print("[send]", message.get("payload"))
    elif message.get("type") == "error":
        print("[error]", message.get("description"))
    else:
        print("[msg]", message)

dev = frida.get_device(DEVICE_SERIAL, timeout=10)
pid = None
for p in dev.enumerate_processes():
    if p.name == PKG or p.name == "霸王茶姬":
        pid = p.pid
        break
if pid is None:
    sys.exit(f"{PKG} not running")
print("attach pid", pid)
session = dev.attach(pid)
script = session.create_script(open(SCRIPT_PATH, encoding="utf-8").read())
script.on("message", on_message)
script.load()
time.sleep(3)
session.detach()
