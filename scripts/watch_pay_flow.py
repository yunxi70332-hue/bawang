"""Watch the live 8443 flows file for order/payment endpoints (createOrder ->
commitPay) and extract the Alipay redirect payload as soon as it appears.

Exits 0 after writing output/alipay_pay_capture_<date>.json; prints TIMEOUT if
nothing matched within the deadline. Torn trailing records (file still being
appended) are retried on the next poll.
"""
import json
import os
import re
import sys
import time

from mitmproxy import io

FLOW = r"E:\霸王茶姬\capture\chagee_phase0b_order_20260926.flows"
OUT = r"E:\霸王茶姬\output\alipay_pay_capture_20260926.json"
KEYS = ("createOrder", "commitPay", "/pay", "Pay", "cashier", "prepay", "alipay", "order/submit")
DEADLINE = time.time() + 900
POLL = 3

seen = set()
while time.time() < DEADLINE:
    hits = []
    try:
        with open(FLOW, "rb") as fh:
            for i, fl in enumerate(io.FlowReader(fh).stream()):
                if fl.type != "http" or i in seen:
                    continue
                path = fl.request.path
                if any(k in path for k in KEYS):
                    seen.add(i)
                    hits.append(fl)
    except Exception:
        pass  # partial trailing record; retry next poll
    if hits:
        out = []
        for fl in hits:
            req, resp = fl.request, fl.response
            resp_text = (resp.get_text() or "") if resp else ""
            link_m = re.search(r'(alipays?://[^"\\\s]+|https?://[^"\\\s]*alipay[^"\\\s]*)', resp_text)
            out.append({
                "ts": fl.timestamp_start,
                "method": req.method,
                "host": req.host,
                "path": path.split("?")[0],
                "req_headers_sign": req.headers.get("sign"),
                "req_body": req.get_text() if req.content else "",
                "status": resp.status_code if resp else None,
                "resp_body": resp_text[:30000],
                "detected_link": link_m.group(1) if link_m else None,
            })
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"CAPTURED {len(out)} flow(s) -> {OUT}")
        for o in out:
            print(f"  {o['method']} {o['host']}{o['path']} -> {o['status']} link={bool(o['detected_link'])}")
        sys.exit(0)
    time.sleep(POLL)
print("TIMEOUT no payment endpoints in 900s")
