# -*- coding: utf-8 -*-
"""82041201 只读探针：定位「网络异常，请稍后重试」发生在哪一跳。

链路（全部只读，不 createOrder）：
  1. whoami                     GET  user-client/customer/userInfo/query
  2. calculate_price            POST navigation/goods/sku/calculatePrice
  3. settle_direct              POST trade-web/order/settlePrice（试算，无券）

用法：python scripts/probe_82041201.py [account_label]   # 默认全部有 token 的账号
产物：output/probe_82041201_<ts>.json
"""
import json
import os
import sqlite3
import sys
import time
import traceback
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "account_system", "server", "services"))

from chagee_bridge import build_client  # noqa: E402
from chagee_client import ChageeClient, ChageeError  # noqa: E402
from chagee_trade_api import ChageeTradeApi  # noqa: E402

DB = os.path.join(ROOT, "account_system", "data", "app.db")

# 2026-09-26 wire 实证商品（粉芭乐 @ CN08121，真实 App 抓包同款参数）
TARGET = {
    "storeNo": "CN08121",
    "spuId": "1253292834069934081", "spuName": "粉芭乐",
    "skuId": "1253292834082516992", "skuName": "粉芭乐",
    "quantity": 1, "salePrice": 20,
    "specList": [{"specId": "653599312273510400", "specOptionId": "653599312273510402",
                  "specOptionName": "大杯"}],
    "attributeList": [
        {"attributeId": "745317722624679942", "attributeOptionId": "745317722624679945"},
        {"attributeId": "623882672850116609", "attributeOptionId": "623882672850116612"},
    ],
    "imageUrl": "https://images.qmai.cn/s49006/2026/09/14/efc78d54275abbe932.jpg",
    "spuType": "stand", "nutritionInfo": {"energyLevel": "C", "energyUnit": "kcal/杯",
                                          "energyValue": "192"},
}


def load_accounts(want_label=None):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select id,label,phone,device_uuid,token,sk,customer_id,status from chagee_accounts"
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        if want_label and r["label"] != want_label:
            continue
        if not r["token"]:
            continue
        out.append(SimpleNamespace(**dict(r)))
    return out


def instrument(client, log):
    """包一层 post/get：留档完整请求（URL/header/body）与响应（原始 JSON），写入 log[-1]。"""
    orig_post, orig_get = client.post, client.get

    def snap_headers():
        try:
            hdrs = dict(client.headers())
        except Exception:
            hdrs = {}
        return {k: ((v[:24] + "...") if k in ("authorization", "sk") and v else v)
                for k, v in hdrs.items()}

    def wrap(orig, method):
        def call(path, body=None, **kw):
            t0 = time.time()
            log[-1].setdefault("requests", []).append({
                "method": method, "path": path, "headers": snap_headers(),
                "body": _trim(body) if body is not None else None,
            })
            try:
                resp = orig(path, body, **kw) if method == "POST" else orig(path, **kw)
                log[-1]["requests"][-1].update({
                    "status": "ok", "elapsed_ms": round((time.time() - t0) * 1000),
                    "resp": _trim(resp)})
                return resp
            except ChageeError as e:
                log[-1]["requests"][-1].update({
                    "status": "chagee_error", "elapsed_ms": round((time.time() - t0) * 1000),
                    "errcode": getattr(e, "errcode", None), "errmsg": str(e)})
                raise
            except Exception as e:
                log[-1]["requests"][-1].update({
                    "status": "exc", "elapsed_ms": round((time.time() - t0) * 1000),
                    "exc": repr(e)})
                raise
        return call

    client.post = wrap(orig_post, "POST")
    client.get = wrap(orig_get, "GET")


def _trim(obj, depth=0):
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return {k: _trim(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_trim(v, depth + 1) for v in obj[:6]] + (["...(%d more)" % (len(obj) - 6)] if len(obj) > 6 else [])
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:300] + "..."
    return obj


def probe_account(acc):
    log = []
    rec = {"label": acc.label, "customer_id": acc.customer_id,
           "token_head": (acc.token or "")[:24], "sk_head": (acc.sk or "")[:16],
           "steps": log, "verdict": None}
    client = build_client(acc)
    instrument(client, log)
    api = ChageeTradeApi(client)   # 用插桩后的同一 client，勿走 trade_api 另建

    def step(name, fn):
        entry = {"step": name}
        log.append(entry)
        # dump_req 已在包装层 append？——包装层 append 的是自己的条目，这里改成共享同一条
        try:
            r = fn()
            entry["summary"] = _summarize(name, r)
            print(f"  [OK]   {name}: {entry['summary']}")
            return r
        except ChageeError as e:
            entry["error"] = {"errcode": getattr(e, "errcode", "?"), "msg": str(e)}
            print(f"  [FAIL] {name}: errcode={entry['error']['errcode']} {e}")
            rec["verdict"] = f"{name} -> {getattr(e, 'errcode', '?')}"
            raise
        except Exception as e:
            entry["error"] = repr(e)
            print(f"  [EXC]  {name}: {e!r}")
            rec["verdict"] = f"{name} -> exc {e!r}"
            raise

    try:
        step("whoami", lambda: client.whoami())
        price = step("calculatePrice", lambda: api.calculate_price(TARGET))
        step("settlePrice", lambda: api.settle_direct(TARGET, price))
        rec["verdict"] = "ALL_OK"
    except Exception:
        pass
    return rec


def _summarize(name, r):
    if name == "whoami":
        d = (r or {}).get("data") or {}
        return f"customerId={d.get('customerId')} mobile={d.get('mobileEncrypt', '')[:12]}..."
    if name == "calculatePrice":
        return (f"totalGoodsItemPrice={r.get('totalGoodsItemPrice')} "
                f"totalTradePrice={r.get('totalTradePrice')}")
    if name == "settlePrice":
        fund = r.trade_fund_info or {}
        return (f"confirmOrderKey={r.confirm_order_key[:12]}... "
                f"total={fund.get('totalTradePrice')} real={fund.get('buyerRealPrice')} "
                f"coupons={len(r.available_coupons)}")
    return str(r)[:120]


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else None
    accs = load_accounts(want)
    if not accs:
        print("无可探测账号（无 token）")
        return
    print(f"探测 {len(accs)} 个账号，商品: 粉芭乐@CN08121（只读试算）")
    results = []
    for acc in accs:
        print(f"\n=== 账号 {acc.label} (customerId={acc.customer_id}) ===")
        try:
            results.append(probe_account(acc))
        except Exception:
            traceback.print_exc()
            results.append({"label": acc.label, "verdict": "crashed"})
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(ROOT, "output", f"probe_82041201_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"generated_at": ts, "target": TARGET, "results": results},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"\n留档: {out}")
    for r in results:
        print(f"  {r.get('label')}: {r.get('verdict')}")


if __name__ == "__main__":
    main()
