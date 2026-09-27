#!/usr/bin/env python3
"""repro_send_raw — 复现 /user-client/message/send 的风控（滑块）拦截返回。

与 chagee_login.send_sms 请求体字节级一致（scene/mobile/sendObj/phoneCode/
blockParam/sid/timestamp + 同签名同加密），唯一差别：不走 _unwrap 抛异常，
把服务端原始 JSON 全量打出来，用于判定是否触发滑块/安全验证。

用法:
  python repro_send_raw.py <phone> <account_id>
  （account_id 用于复用账号管理系统的隔离设备身份 data/accounts/<id>/）
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from chagee_client import ChageeClient  # noqa: E402


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python repro_send_raw.py <phone> <account_id>")
    phone, account_id = sys.argv[1], sys.argv[2]
    workdir = os.path.normpath(os.path.join(
        HERE, "..", "account_system", "data", "accounts", account_id))
    device_file = os.path.join(workdir, "device.json")
    if not os.path.exists(device_file):
        sys.exit(f"device file not found: {device_file}")
    c = ChageeClient(
        env="release",
        device_file=device_file,
        session_file=os.path.join(workdir, "session.json"),
        seed_file=os.path.join(workdir, "seed_unused.json"),
    )
    c.ensure_sk()
    print(f"[repro] uuid={c.uuid} phone={phone[:3]}****{phone[-4:]} ts={time.strftime('%F %T')}")

    body = {
        "scene": "login",
        "mobile": phone,
        "sendObj": phone,
        "phoneCode": "86",
        "blockParam": "不验证",
        "sid": c.uuid,
        "timestamp": int(time.time() * 1000),
    }
    body = c._prepare_body(
        body,
        sign_fields=["sendObj", "sid", "timestamp"],
        encrypt_fields=["sendObj", "mobile"],
    )
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        c.env["gw"] + "/user-client/message/send", data=data,
        headers=c.headers(logged_in=False), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    print(f"[repro] HTTP {status}")
    try:
        resp = json.loads(raw)
    except Exception:
        print("[repro] 非 JSON 响应：", raw[:2000])
        return
    # 脱敏打印（token 类字段截断）
    def _mask(v):
        return v if not isinstance(v, str) or len(v) < 120 else v[:60] + f"...len={len(v)}"
    print(json.dumps({k: _mask(v) for k, v in resp.items()}, ensure_ascii=False, indent=2))
    d = resp.get("data")
    if isinstance(d, dict):
        print("[repro] data keys:", list(d.keys()))
        print("[repro] sendFlag =", d.get("sendFlag"), "| dispose =", d.get("dispose"))


if __name__ == "__main__":
    main()
