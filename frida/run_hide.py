# -*- coding: utf-8 -*-
"""Spawn Bawang Chaji with environment-hiding script (anti-Yidun detection).
Background daemon; keeps injection alive."""
import sys, time
import frida

HOST = '127.0.0.1:27042'
PKG = 'com.chagee.application.cn'
SCRIPT = r'E:\霸王茶姬\frida\scripts\env_hide.js'

def log(*a):
    print('[hide-daemon]', *a, flush=True)

device = frida.get_device_manager().add_remote_device(HOST)
try:
    device.kill(PKG)
    log('killed existing')
except Exception as e:
    log('kill skipped:', e)

pid = device.spawn([PKG])
session = device.attach(pid)
session.on('detached', lambda reason, *r: log('detached:', reason))

with open(SCRIPT, encoding='utf-8') as f:
    code = f.read()
script = session.create_script(code)
script.on('message', lambda m, d: log('msg:', m.get('payload') or m))
script.load()
device.resume(pid)
log(f'spawned {PKG} pid={pid}, env_hide active')

while True:
    time.sleep(5)
