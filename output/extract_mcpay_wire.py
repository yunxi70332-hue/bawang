# -*- coding: utf-8 -*-
"""从 mitmproxy flows (tnetstring) 提取 mobilegw mcpay 铸造流的真实设备凭据与请求头。

产出 output/mcpay_wire_extract.json，供纯协议客户端 alipay_msp_client.py 复用：
- flow 48: alipay.security.device.data.report 明文请求体（apdid/apdidToken/dynamicKey/deviceData）
- flow 50/146/456: alipay.msp.cashier.dispatch.bytes 完整请求头 + 三段式请求体 + 响应头
- WebView UA（Android 版本）与 cashierRoutePay 样本 URL
"""
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\baidunetdiskdownload\霸王茶姬")
FLOWS = ROOT / "capture" / "chagee_native_phase0b_20260926.flows"
OUT = ROOT / "output" / "mcpay_wire_extract.json"


def parse_tnetstring(buf, pos=0):
    """mitmproxy 旧 tnetstring: <len>:<payload><type>"""
    colon = buf.index(b":", pos)
    length = int(buf[pos:colon])
    start = colon + 1
    end = start + length
    payload = buf[start:end]
    t = chr(buf[end])
    pos = end + 1
    if t == "}":  # dict: keys end with ';'
        d, p = {}, 0
        while p < len(payload):
            k, p = parse_tnetstring(payload, p)
            assert isinstance(k, bytes)
            v, p = parse_tnetstring(payload, p)
            d[k.decode("utf-8", "replace")] = v
        return d, pos
    if t == "]":  # list
        arr, p = [], 0
        while p < len(payload):
            v, p = parse_tnetstring(payload, p)
            arr.append(v)
        return arr, pos
    if t == ",":
        return payload.decode("utf-8", "replace"), pos
    if t == "#":
        return int(payload), pos
    if t == "^":
        return float(payload), pos
    if t == "!":
        return payload == b"true", pos
    if t == "~":
        return None, pos
    if t == ";":
        return payload, pos  # raw bytes
    raise ValueError(f"unknown type {t!r}")


def header_dict(lst):
    if not lst:
        return {}
    out = {}
    for item in lst:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            k, v = item[0], item[1]
            out[k if isinstance(k, str) else k.decode("utf-8", "replace")] = (
                v if isinstance(v, str) else v.decode("utf-8", "replace"))
    return out


def main():
    raw = FLOWS.read_bytes()
    flows, pos = [], 0
    while pos < len(raw):
        try:
            rec, pos = parse_tnetstring(raw, pos)
        except Exception:
            break
        flows.append(rec)
    print(f"parsed {len(flows)} records")

    result = {"flows_meta": len(flows), "mobilegw": [], "webview_ua": None,
              "cashier_route_urls": [], "data_report": None}

    for i, fl in enumerate(flows):
        if not isinstance(fl, dict):
            continue
        req = fl.get("request") or {}
        if not isinstance(req, dict):
            continue
        host = str(req.get("host", ""))
        url = str(req.get("scheme", "")) + "://" + host + str(req.get("path", ""))
        if "mobilegw" not in host:
            continue
        h = header_dict(req.get("headers"))
        body = req.get("content") or b""
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        resp = fl.get("response") or {}
        rh = header_dict(resp.get("headers") if isinstance(resp, dict) else {})
        entry = {
            "flow_index": i,
            "url": url,
            "method": str(req.get("method", "")),
            "headers": h,
            "body_prefix_hex": body[:96].hex(),
            "body_len": len(body),
        }
        op = h.get("Operation-Type", "")
        if "security.device.data.report" in op or "device.data" in str(body[:400]):
            entry["body_text"] = body.decode("utf-8", "replace")
            result["data_report"] = entry
        elif "msp.cashier.dispatch" in op:
            entry["resp_headers"] = rh
            entry["resp_status"] = resp.get("status_code") if isinstance(resp, dict) else None
            result["mobilegw"].append(entry)

    # WebView UA + cashierRoutePay 样本
    for i, fl in enumerate(flows):
        if not isinstance(fl, dict):
            continue
        req = fl.get("request") or {}
        if not isinstance(req, dict):
            continue
        url = str(req.get("scheme", "")) + "://" + str(req.get("host", "")) + str(req.get("path", ""))
        h = header_dict(req.get("headers"))
        ua = h.get("user-agent", "")
        if "cashierRoutePay" in url:
            result["cashier_route_urls"].append({"flow_index": i, "url": url, "ua": ua})
        if result["webview_ua"] is None and "mclient.alipay.com" in url and "Mozilla" in ua:
            result["webview_ua"] = {"flow_index": i, "ua": ua}

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"mobilegw dispatch flows: {len(result['mobilegw'])}, data_report: {result['data_report'] is not None}")
    print(f"cashierRoutePay urls: {len(result['cashier_route_urls'])}")
    if result["webview_ua"]:
        print("WebView UA:", result["webview_ua"]["ua"][:160])


if __name__ == "__main__":
    sys.exit(main())
