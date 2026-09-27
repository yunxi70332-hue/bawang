"""Watch both live flow files for the payment-completion leg.

Terminal signals (any one ends the watch):
- Dart getOrderDetail/getOrderList where orderStatus != 1 (status flip off 待支付)
- Dart getWaitingInfo / getOrderStatus response with paid state
- Native mobilegw.alipay.com mgw.htm POSTs (Alipay H5 payment execution)

Everything collected is archived to output/pay_complete_capture_20260926.json
on exit (including on timeout).
"""
import json
import time

from mitmproxy import io

FILES = {
    "dart": r"E:\霸王茶姬\capture\chagee_phase0b_order_20260926.flows",
    "native": r"E:\霸王茶姬\capture\chagee_native_phase0b_20260926.flows",
}
OUT = r"E:\霸王茶姬\output\pay_complete_capture_20260926.json"
KEYS_DART = ("getOrderStatus", "getOrderDetail", "getOrderList", "getWaitingInfo",
             "commitPay", "continuePay", "cancelOrder")
KEYS_NATIVE = ("mobilegw", "cashierMain", "cashierRoutePay", "h5pay", "payResult", "chinaums")
DEADLINE = time.time() + 1500  # 25 min
POLL = 3

seen = set()
collected = []
WATCH_START = time.time()


def safe_text(msg):
    try:
        return msg.get_text() or ""
    except Exception:
        raw = msg.raw_content or b""
        return f"<binary {len(raw)}B head={raw[:120]!r}>"


def scan(tag, path, keys):
    hits = []
    try:
        with open(path, "rb") as fh:
            for i, fl in enumerate(io.FlowReader(fh).stream()):
                if fl.type != "http" or fl.timestamp_start < WATCH_START:
                    continue
                key = (tag, i)
                if key in seen:
                    continue
                if any(k in fl.request.path or k in fl.request.host for k in keys):
                    seen.add(key)
                    hits.append((fl, key))
    except Exception:
        pass  # torn trailing record; retry next poll
    return hits


terminal = None
while time.time() < DEADLINE and terminal is None:
    for tag, path in FILES.items():
        keys = KEYS_DART if tag == "dart" else KEYS_NATIVE
        for fl, key in scan(tag, path, keys):
            req, resp = fl.request, fl.response
            body = safe_text(resp) if resp else ""
            entry = {
                "side": tag,
                "method": req.method,
                "host": req.host,
                "path": req.path.split("?")[0],
                "req_body": safe_text(req)[:4000],
                "status": resp.status_code if resp else None,
                "resp_body": body[:30000],
            }
            # order status flip = terminal
            if tag == "dart" and resp is not None:
                try:
                    d = json.loads(body)
                    data = d.get("data") or {}
                    if "getOrderDetail" in req.path or "getOrderList" in req.path:
                        objs = [data] if "getOrderDetail" in req.path else (data.get("orderList") or data.get("pageList") or [])
                        for o in objs:
                            if isinstance(o, dict) and o.get("orderStatus") not in (None, 1):
                                entry["terminal"] = f"orderStatus={o.get('orderStatus')} ({o.get('orderStatusText')})"
                                terminal = entry["terminal"]
                    if "getOrderStatus" in req.path and isinstance(data, dict) and data.get("orderStatus") not in (None, 1):
                        entry["terminal"] = f"getOrderStatus orderStatus={data.get('orderStatus')}"
                        terminal = entry["terminal"]
                    if "getWaitingInfo" in req.path:
                        entry["terminal"] = "getWaitingInfo (pickup page)"
                        terminal = entry["terminal"]
                except Exception:
                    pass
            if tag == "native" and "mobilegw" in req.host:
                entry["note"] = "alipay cashier/mobilegw traffic (non-terminal)"
            collected.append(entry)
            print(f"[{len(collected)}] {tag} {req.method} {req.host}{entry['path']} -> {entry['status']}"
                  + (f"  TERMINAL: {entry['terminal']}" if entry.get("terminal") else ""), flush=True)
    if terminal:
        break
    time.sleep(POLL)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(collected, f, ensure_ascii=False, indent=1)
print(f"DONE terminal={terminal} archived {len(collected)} entries -> {OUT}")
