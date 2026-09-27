"""mitmproxy reverse-mode addon: route upstream by TLS SNI.

Correct hook: `tls_clienthello` fires when the ClientHello arrives, BEFORE any
upstream connection is made (works with any connection_strategy). Setting the
address in `request` is too late in reverse mode — the mode's own hook runs
after addon `request` hooks and overwrites the destination with the mode
default (verified by misrouted traffic on 2026-09-22).

Device side: /system/etc/hosts points business domains at 127.0.0.1, the app
connects to 127.0.0.1:443 with its real SNI, adb reverse tunnels to this
mitmproxy, and this addon sends each connection to the host the client asked
for.
"""

from mitmproxy import ctx
from mitmproxy import tls


def tls_clienthello(data: tls.ClientHelloData):
    sni = data.client_hello.sni
    if sni:
        data.context.server.address = (sni, 443)
        ctx.log.info(f"[sni_route] {sni}")
