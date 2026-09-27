import frida, time, sys

pid = int(sys.argv[1])
dev = frida.get_device_manager().add_remote_device('127.0.0.1:27042')
s = dev.attach(pid)
script = s.create_script('''
const names = ['ssl_crypto_x509_session_verify_cert_chain', 'session_verify_cert_chain',
               'ssl_verify_cert_chain', 'X509_verify_cert'];
for (const n of names) {
    try {
        const a = Module.findExportByName('libflutter.so', n);
        console.log(n + ' => ' + a);
    } catch (e) { console.log(n + ' => ERR ' + e.message); }
}
const m = Process.findModuleByName('libflutter.so');
console.log('libflutter base: ' + (m ? m.base + ' size ' + Math.round(m.size/1024) + 'KB' : 'NOT LOADED'));
''')
out = []
script.on('message', lambda m, d: out.append(m.get('payload') or str(m)))
script.load()
time.sleep(2)
for l in out:
    print(l)
s.detach()
