#!/usr/bin/env python3
"""repro_send_with_captcha — 带 captchaVerifyParam 重发 message/send，验证滑块令牌形态。

候选值实验：challenge 响应里的 smSessionId / 其他。观察 sendFlag 与 dispose.action。
用法: python repro_send_with_captcha.py <phone> <account_id> [smSessionId]
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


def raw_send(c: ChageeClient, phone: str, extra: dict) -> dict:
    body = {
        "scene": "login",
        "mobile": phone,
        "sendObj": phone,
        "phoneCode": "86",
        "blockParam": "不验证",
        "sid": c.uuid,
        "timestamp": int(time.time() * 1000),
    }
    body.update(extra)
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
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode(errors="replace")[:300]}


def main():
    phone, account_id = sys.argv[1], sys.argv[2]
    sm_session = sys.argv[3] if len(sys.argv) > 3 else ""
    workdir = os.path.normpath(os.path.join(
        HERE, "..", "account_system", "data", "accounts", account_id))
    c = ChageeClient(env="release",
                     device_file=os.path.join(workdir, "device.json"),
                     session_file=os.path.join(workdir, "session.json"),
                     seed_file=os.path.join(workdir, "seed_unused.json"))
    c.ensure_sk()
    print(f"[probe] phone={phone[:3]}****{phone[-4:]} uuid={c.uuid}")

    # 第一步：裸发，拿 challenge 的 smSessionId
    r1 = raw_send(c, phone, {})
    d1 = (r1.get("data") or {})
    print(f"[1] bare      sendFlag={d1.get('sendFlag')} action={(d1.get('dispose') or {}).get('action')}")
    sess = ((d1.get("dispose") or {}).get("smSessionId")) or sm_session
    if not sess:
        print("[1] 无 challenge 会话，也无需验证码令牌，直接结束")
        return
    print(f"[1] smSessionId={sess[:40]}...len={len(sess)}")

    # 第二步：带 captchaVerifyParam=smSessionId 重发（新 timestamp+签名）
    time.sleep(2)
    r2 = raw_send(c, phone, {"captchaVerifyParam": sess})
    d2 = (r2.get("data") or {})
    print(f"[2] +smSession sendFlag={d2.get('sendFlag')} action={(d2.get('dispose') or {}).get('action')} msg={r2.get('errmsg')}")
    if d2.get("sendFlag") is True:
        print("[2] *** 短信真实投递成功：captchaVerifyParam = smSessionId 实锤 ***")


if __name__ == "__main__":
    main()
