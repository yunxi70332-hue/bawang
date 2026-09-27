import frida, time, sys

pid = int(sys.argv[1])
dev = frida.get_device_manager().add_remote_device('127.0.0.1:27042')
s = dev.attach(pid)
script = s.create_script('''
const mods = Process.enumerateModules().filter(m =>
    !m.path.startsWith('/system') && !m.path.startsWith('/apex') &&
    !m.path.startsWith('/vendor') && !m.path.startsWith('/product'));
for (const m of mods) console.log(m.name + ' | ' + m.path.slice(0,95) + ' | ' + Math.round(m.size/1024) + 'KB');
console.log('TOTAL: ' + mods.length);
''')
out = []
script.on('message', lambda m, d: out.append(m.get('payload') or str(m)))
script.load()
time.sleep(3)
for l in out:
    print(l)
s.detach()
