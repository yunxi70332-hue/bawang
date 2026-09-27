# -*- coding: utf-8 -*-
"""Spawn Bawang Chaji with httptoolkit certificate-unpinning script attached.
Stays alive as a background daemon so the injection persists."""
import sys, time, threading
import frida

HOST = '127.0.0.1:27042'
PKG = 'com.chagee.application.cn'
SCRIPT = r'E:\霸王茶姬\frida\scripts\unpin-combined.js'

def log(*a):
    print('[unpin]', *a, flush=True)

device = frida.get_device_manager().add_remote_device(HOST)

try:
    device.kill(PKG)
    log('killed existing', PKG)
except Exception as e:
    log('kill skipped:', e)

pid = device.spawn([PKG])
session = device.attach(pid)
session.on('detached', lambda reason, *rest: log('session detached:', reason))

with open(SCRIPT, encoding='utf-8') as f:
    code = f.read()

script = session.create_script(code)
script.on('message', lambda m, d: log('msg:', m.get('description') or m))
script.load()
device.resume(pid)
log(f'spawned {PKG} pid={pid}, unpinning script active')

while True:
    time.sleep(5)
