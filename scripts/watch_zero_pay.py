"""Watch the ¥0 (20-yuan voucher on ¥20 drink) order leg.

Archives settlePrice / order-coupon-list / createOrder / commitPay /
continuePay / getOrderDetail / getOrderStatus / getWaitingInfo traffic.
Terminal: getWaitingInfo, or the NEW order's status leaving 待支付(1).
"""
import json
import time

from mitmproxy import io

FLOW = r"E:\霸王茶姬\capture\chagee_phase0b_order_20260926.flows"
OUT = r"E:\霸王茶姬\output\pay_zero_capture_20260926.json"
KEYS = ("settlePrice", "order-coupon-list", "createOrder", "commitPay", "continuePay",
        "getOrderDetail", "getOrderStatus", "getWaitingInfo", "cancelOrder")
DEADLINE = time.time() + 1500
POLL = 3
WATCH_START = time.time()

seen = set()
collected = []
target_order = None


def safe_text(msg):
    try:
        return msg.get_text() or ""
    except Exception:
        raw = msg.raw_content or b""
        return f"<binary {len(raw)}B>"


while time.time() < DEADLINE:
    terminal = None
    hits = []
    try:
        with open(FLOW, "rb") as fh:
            for i, fl in enumerate(io.FlowReader(fh).stream()):
                if fl.type != "http" or fl.timestamp_start < WATCH_START:
                    continue
                key = i
                if key in seen:
                    continue
                if any(k in fl.request.path for k in KEYS):
                    seen.add(key)
                    hits.append(fl)
    except Exception:
        pass
    for fl in hits:
        req, resp = fl.request, fl.response
        body = safe_text(resp) if resp else ""
        entry = {
            "ts": fl.timestamp_start,
            "path": req.path.split("?")[0],
            "req_body": safe_text(req)[:6000],
            "status": resp.status_code if resp else None,
            "resp_body": body[:30000],
        }
        tag = req.path.split("/")[-1]
        try:
            d = json.loads(body)
            data = d.get("data") or {}
            if "createOrder" in req.path:
                entry["note"] = f"new orderNo={data.get('orderNo')} payNo={data.get('payNo')} hasPayUrl={'payUrl' in data}"
                global_target = data.get("orderNo")
                if global_target:
                    target_order = global_target
            elif "getOrderDetail" in req.path and data.get("orderNo") == target_order:
                entry["note"] = f"target status={data.get('orderStatus')} {data.get('orderStatusText')} pickupNo={data.get('pickupNo')}"
                if data.get("orderStatus") not in (None, 1):
                    terminal = f"target orderStatus={data.get('orderStatus')}"
            elif "getOrderStatus" in req.path and target_order and str(target_order) in safe_text(req):
                entry["note"] = f"target orderStatus={data.get('orderStatus') if isinstance(data, dict) else data}"
                if isinstance(data, dict) and data.get("orderStatus") not in (None, 1):
                    terminal = f"target orderStatus={data.get('orderStatus')}"
            elif "getWaitingInfo" in req.path:
                terminal = "getWaitingInfo"
            elif "settlePrice" in req.path:
                entry["note"] = "settle trial"
        except Exception:
            pass
        collected.append(entry)
        print(f"[{len(collected)}] {tag} -> {entry['status']}  {entry.get('note','')}"
              + (f"  TERMINAL: {terminal}" if terminal else ""), flush=True)
    if terminal:
        break
    time.sleep(POLL)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(collected, f, ensure_ascii=False, indent=1)
print(f"DONE archived {len(collected)} entries -> {OUT}")
