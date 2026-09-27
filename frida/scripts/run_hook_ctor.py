#!/usr/bin/env python3
"""后台常驻：运行 hook_wv_ctor.js，消息写 output/wv_ctor.log"""
import frida
import sys
import time

OUT = r"C:\baidunetdiskdownload\霸王茶姬\output\wv_ctor.log"

def main():
    out = open(OUT, "a", encoding="utf-8")
    dev = frida.get_device("125.109.27.7:58445", timeout=10)
    pid = next(p.pid for p in dev.enumerate_processes() if p.name in ("霸王茶姬", "com.chagee.application.cn"))
    s = dev.attach(pid)
    sc = s.create_script(open(r"C:\baidunetdiskdownload\霸王茶姬\frida\scripts\hook_wv_ctor.js", encoding="utf-8").read())
    def on_msg(m, d):
        line = str(m.get("payload") if m.get("type") != "error" else "[error] " + str(m.get("description")))
        out.write(line + "\n")
        out.flush()
    sc.on("message", on_msg)
    sc.load()
    out.write(f"--- session started pid={pid} ---\n")
    out.flush()
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
