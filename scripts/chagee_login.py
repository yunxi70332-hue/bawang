#!/usr/bin/env python3
"""chagee_login — 设备注册+登录全流程（Phase 2）。

功能1（设备注册流程）的协议层等价实现：
  设备身份(uuid/cid) + 13 公共头 + getsk 握手  ← chagee_client.ChageeClient 已完成
  + 短信发送 /user-client/message/send        ← 请求体已 wire 字节级验证（test 环境）
  + 短信登录 /user-client/auth/login/sms      ← 本模块新增（生产闭环）
  + 登出     /user-client/auth/logout

CLI:
  python chagee_login.py status            # 会话自检（设备+sk+token+whoami）
  python chagee_login.py send-sms          # 发送登录短信（真实投递，60s 限流）
  python chagee_login.py login <code>      # 用验证码登录，落盘新 session
  python chagee_login.py logout            # 协议登出（会话作废）
注意：生产外向动作（发短信）按用户已授权范围执行；token/手机号日志脱敏。
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from chagee_client import ChageeClient, RateLimitError, ChageeError  # noqa: E402

# login/sms 响应 data 结构（2026-09-23 生产实测）：
#   {customerId, accessToken(608B 裸 JWT), refreshToken, firstLogin, dispose}
TOKEN_KEYS = ["accessToken", "loginToken", "token", "authorization", "loginUserToken", "userToken"]


def mask(s: str, keep: int = 6) -> str:
    if not s:
        return ""
    return s[:keep] + f"...len={len(s)}"


def load_phone(c: ChageeClient) -> str:
    seed = os.path.join(HERE, "session_seed.json")
    if os.path.exists(seed):
        p = json.load(open(seed, encoding="utf-8")).get("phone", "")
        if len(p) == 11:
            return p
    # 种子没有则从 whoami 响应解密兜底
    info = c.whoami()
    enc = info.get("data", {}).get("mobileEncrypt", "")
    return c.proto.decrypt_field(enc) if enc else ""


def send_sms(c: ChageeClient, phone: str) -> dict:
    """POST /user-client/message/send（请求体结构与 wire 字节级一致的构造，改用 release 签名密钥）。

    extra 配置：signFields=[sendObj,sid,timestamp]，requestEncryptFields=[sendObj,mobile]
    sid = 设备 uuid（知识库样本 e55a9fa2-… 为 uuid 形态）。
    """
    ts = int(time.time() * 1000)
    body = {
        "scene": "login",
        "mobile": phone,
        "sendObj": phone,
        "phoneCode": "86",
        "blockParam": "不验证",
        "sid": c.uuid,
        "timestamp": ts,
    }
    resp = c.post(
        "/user-client/message/send",
        body=body,
        logged_in=False,
        sign_fields=["sendObj", "sid", "timestamp"],
        encrypt_fields=["sendObj", "mobile"],
    )
    d = resp.get("data") or {}
    print(f"[send-sms] errcode=0 sendFlag={d.get('sendFlag')} action={(d.get('dispose') or {}).get('action')}")
    return resp


def _extract_token(data: dict) -> tuple:
    """从 login 响应 data 中探测 token（键名以静态结论为准，未命中则全量报告辅助定位）。"""
    if not isinstance(data, dict):
        return "", []
    for k in TOKEN_KEYS:
        v = data.get(k)
        if isinstance(v, str) and len(v) > 100:  # 608B JWT 特征
            return v, [k]
    # 兜底：任何超长字符串字段都可能是 token
    cands = [(k, len(v)) for k, v in data.items() if isinstance(v, str) and len(v) > 100]
    return "", cands


def login_sms(c: ChageeClient, phone: str, code: str) -> str:
    """POST /user-client/auth/login/sms（smsCode 明文、无 sign、仅 mobile 加密）。"""
    body = {
        "phoneCode": "86",
        "smsCode": code,
        "mobile": phone,
        "storeNo": "",
    }
    resp = c.post(
        "/user-client/auth/login/sms",
        body=body,
        logged_in=False,
        encrypt_fields=["mobile"],
    )
    token, hits = _extract_token(resp.get("data") or {})
    if not token:
        print("[login] 响应未见 token 候选字段，data 键：", list((resp.get("data") or {}).keys()))
        if hits:
            print("[login] 超长字段候选（可能需解密）：", hits)
        # 登录响应若含加密字段（responseEncryptFields），在此解密后重试提取
        try:
            dec = c.proto.decrypt_fields_in_response(resp, {"token", "loginToken", "mobileEncrypt"})
            token, hits = _extract_token(dec.get("data") or {})
        except Exception as e:
            print("[login] 响应解密尝试失败：", e)
    if not token:
        raise ChageeError("-1", "login 成功但未提取到 token，需人工分析响应结构")
    c.token = token
    c.proto.token = token
    c.session_source = "session.json (protocol login)"
    c.save_session()
    print(f"[login] token 提取自字段 {hits[0]}：{mask(token)}")
    return token


def logout(c: ChageeClient) -> dict:
    """POST /user-client/auth/logout（body 结构以静态/样本为准，先空体）。"""
    resp = c.post("/user-client/auth/logout", body={}, logged_in=True)
    c.token = ""
    c.proto.token = ""
    if os.path.exists(c.session_file):
        os.remove(c.session_file)
    print("[logout] done, session cleared")
    return resp


def status(c: ChageeClient):
    print("env      :", c.env_name)
    print("device   :", c.uuid, f"({c.device.get('source')})")
    print("sk       :", mask(c.proto.sk, 8) or "(未握手)")
    print("token    :", mask(c.token) or "(无)", "from", c.session_source or "-")
    if c.token:
        info = c.whoami()
        d = info.get("data", {})
        print("whoami   :", info.get("errcode"), "| customerId:", mask(str(d.get("customerId", "")), 4),
              "| nick:", d.get("nickName", ""))


def main():
    c = ChageeClient(env="release")
    c.ensure_sk()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status(c)
    elif cmd == "send-sms":
        phone = load_phone(c)
        print("[send-sms] target:", phone[:3] + "****" + phone[-4:])
        try:
            send_sms(c, phone)
        except RateLimitError as e:
            print("[send-sms] 限流：", e)
    elif cmd == "login":
        code = sys.argv[2] if len(sys.argv) > 2 else ""
        if not code:
            sys.exit("usage: python chagee_login.py login <code>")
        phone = load_phone(c)
        token = login_sms(c, phone, code)
        print("[login] 会话已建立，验证 whoami：")
        status(c)
    elif cmd == "logout":
        logout(c)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
