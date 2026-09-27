#!/usr/bin/env python3
"""ChageeClient — 六功能纯协议流水线的会话客户端（Phase 1）。

职责：
  - 设备身份：uuid/cid（同值安装标识）本地生成并持久化（device.json），或复用设备真实值（session_seed.json）
  - 公共头：13 个固定头（含 apv:1.0.0，chagee_protocol.BASE_HEADERS 缺此项）+ 登录态头 uuid/cid/sk/authorization
  - getsk 握手与缓存（等价 SP 键 chagee_encrypt_sp_key；release 为空前缀）
  - 统一请求管线：字段加密(requestEncryptFields) → 签名(signFields) → POST → 包络校验 → 响应解密(responseEncryptFields)
  - 异常分级：SessionExpired / RateLimit / ChageeApiError / 网络层
环境：默认 release 生产（test 已弃用：短信不投递 + test-gj 网关 404）。
依赖：chagee_protocol.ChageeProtocol（密码学原语，全部已 wire 验证）。
"""

import json
import os
import time
import urllib.request
import urllib.error
import uuid as uuidlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys_path_fix = HERE
import sys

if sys_path_fix not in sys.path:
    sys.path.insert(0, sys_path_fix)

from chagee_protocol import ChageeProtocol  # noqa: E402

ENVIRONMENTS = {
    "release": {
        "gw": "https://gw.chagee.com",
        "gj_api": "https://gj-api.bwcj.com",
        "getsk": "https://gj-api.bwcj.com/encrypt-server/enctrypt/api/getsk?code=CHAGEE_C_001",
        "secret_key_env": "release",
    },
    # test 仅保留用于离线对拍，线上行为不可用（KB 定案）
    "test": {
        "gw": "https://test-gw.chagee.com",
        "gj_api": "https://test-gj-api.bwcj.com",
        "getsk": "https://test-gj-api.bwcj.com/encrypt-server/enctrypt/api/getsk?code=CHAGEE_C_001",
        "secret_key_env": "default",
    },
}

# 13 个公共头（2026-09-26 wire 定案：App 已升级，avc 638→2060、apv 1.0.0→1.0.3、Dart 3.6→3.8；
# 购物车服务按版本校验渠道注册表，旧版本号触发 [99997] 门店渠道no不存在——生产实测）
COMMON_HEADERS = {
    "ua": "Dart/2.12 (dart:io)",
    "user-agent": "Dart/3.8 (dart:io)",
    "avc": "2060",
    "apv": "1.0.3",
    "tcode": "CHAGEE",
    "channel": "APP",
    "os": "android",
    "aid": "100001",
    "language": "zh_CN",
    "region": "CN",
    "devicetimezoneregion": "Asia/Shanghai",
    "content-type": "application/json",
}

# 会话失效码：12320120400401（token 失效专用码）；"401"（实测 2026-09-26：HTTP 200 + errcode="401"
# + errmsg="您的账号已退出登录"，同样表示 token 失效需重登）
SESSION_EXPIRED_CODES = {"401", "12320120400401"}


class ChageeError(Exception):
    """基类：携带 errcode/errmsg/traceId 便于流水线判定。"""

    def __init__(self, errcode, errmsg, trace_id=""):
        super().__init__(f"[{errcode}] {errmsg} (trace={trace_id})")
        self.errcode = str(errcode)
        self.errmsg = errmsg
        self.trace_id = trace_id


class SessionExpiredError(ChageeError):
    """401 / 12320120400401：token 失效，无 refresh，需重登。"""


class RateLimitError(ChageeError):
    """限流（如短信 60s）。errmsg 通常含“60s内只能发送一次”。"""


class ChageeClient:
    def __init__(self, env: str = "release", device_file: str = None,
                 session_file: str = None, seed_file: str = None):
        self.env_name = env
        self.env = ENVIRONMENTS[env]
        self.device_file = device_file or os.path.join(HERE, "device.json")
        self.session_file = session_file or os.path.join(HERE, "session.json")
        self.seed_file = seed_file or os.path.join(HERE, "session_seed.json")

        self.proto = ChageeProtocol()
        self.uuid = ""
        self.token = ""
        self.device = self._load_device()
        self._load_session()

    # ---------------- 设备身份（功能1 的协议层等价物） ----------------

    def _load_device(self) -> dict:
        """安装标识：优先复用设备真实 uuid（seed），否则本地生成并持久化。"""
        if os.path.exists(self.device_file):
            with open(self.device_file, encoding="utf-8") as f:
                d = json.load(f)
            self.uuid = d.get("uuid", "")
            return d
        seed_uuid = ""
        if os.path.exists(self.seed_file):
            with open(self.seed_file, encoding="utf-8") as f:
                seed_uuid = json.load(f).get("uuid", "")
        d = {
            "uuid": seed_uuid or str(uuidlib.uuid4()),
            "persistent_device_id": "device_" + os.urandom(8).hex(),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "seed" if seed_uuid else "generated",
        }
        with open(self.device_file, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        self.uuid = d["uuid"]
        return d

    # ---------------- 会话状态 ----------------

    def _load_session(self):
        # 种子优先级：session.json（本客户端登录产物）> session_seed.json（设备 SP 导出）
        for p in (self.session_file, self.seed_file):
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    s = json.load(f)
                self.token = s.get("token") or ""
                if self.token:
                    self.proto.token = self.token
                    self.session_source = os.path.basename(p)
                    return
        self.session_source = ""

    def save_session(self):
        with open(self.session_file, "w", encoding="utf-8") as f:
            json.dump({
                "env": self.env_name,
                "token": self.token,
                "token_len": len(self.token),
                "sk": self.proto.sk,
                "uuid": self.uuid,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False, indent=2)

    # ---------------- 头组装 ----------------

    def headers(self, logged_in: bool = True, with_sk: bool = True) -> dict:
        h = dict(COMMON_HEADERS)
        h["uuid"] = h["cid"] = self.uuid
        if with_sk and self.proto.sk:
            h["sk"] = self.proto.sk
        if logged_in and self.token:
            h["authorization"] = self.token  # 裸 JWT，无 Bearer
        return h

    # ---------------- getsk ----------------

    def ensure_sk(self) -> str:
        if self.proto.sk:
            return self.proto.sk
        req = urllib.request.Request(self.env["getsk"], headers=self.headers(logged_in=False), method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        if resp.get("errcode") != "0" or not resp.get("data"):
            raise ChageeError(resp.get("errcode"), f"getsk failed: {resp.get('errmsg')}")
        self.proto.sk = resp["data"]
        return self.proto.sk

    # ---------------- 请求管线 ----------------

    def _prepare_body(self, body: dict, sign_fields=None, encrypt_fields=None) -> dict:
        """extra 配置管线：先字段加密，后子集签名（值为加密后的最终值）。"""
        body = dict(body or {})
        if encrypt_fields:
            self.ensure_sk()
            for f in encrypt_fields:
                if f in body:
                    body[f] = self.proto.encrypt_field(str(body[f]))
        if sign_fields:
            subset = {f: body[f] for f in sign_fields if f in body}
            body["sign"] = self.proto.generate_sign(subset, env=self.env["secret_key_env"])
        return body

    def _unwrap(self, resp: dict) -> dict:
        errcode = str(resp.get("errcode", ""))
        errmsg = str(resp.get("errmsg", ""))
        if errcode == "0":
            return resp
        if errcode in SESSION_EXPIRED_CODES:
            raise SessionExpiredError(errcode, errmsg, resp.get("thirdTraceId", ""))
        if "60s" in errmsg or "频繁" in errmsg:
            raise RateLimitError(errcode, errmsg, resp.get("thirdTraceId", ""))
        raise ChageeError(errcode, errmsg, resp.get("thirdTraceId", ""))

    def post(self, path: str, body: dict = None, logged_in: bool = True,
             sign_fields=None, encrypt_fields=None, response_decrypt_fields=None,
             timeout: int = 15) -> dict:
        body = self._prepare_body(body, sign_fields, encrypt_fields)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        h = self.headers(logged_in=logged_in)
        if data is None:
            h.pop("content-type", None)  # 空 body 请求 App 不带 content-type（cityList 实测）
        req = urllib.request.Request(self.env["gw"] + path, data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 401:
                raise SessionExpiredError(401, f"HTTP 401: {detail}")
            raise ChageeError(e.code, f"HTTP {e.code}: {detail}")
        resp = self._unwrap(resp)
        if response_decrypt_fields:
            self.ensure_sk()
            resp = self.proto.decrypt_fields_in_response(resp, set(response_decrypt_fields))
        return resp

    def get(self, path: str, logged_in: bool = True, timeout: int = 15) -> dict:
        req = urllib.request.Request(self.env["gw"] + path, headers=self.headers(logged_in=logged_in), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 401:
                raise SessionExpiredError(401, f"HTTP 401: {detail}")
            raise ChageeError(e.code, f"HTTP {e.code}: {detail}")
        return self._unwrap(resp)

    # ---------------- 便捷只读 ----------------

    def whoami(self) -> dict:
        """GET /user-client/customer/userInfo/query — 会话有效性自检（生产 errcode=0 已验证）。"""
        return self.get("/user-client/customer/userInfo/query")

    def mask(self, s: str, keep: int = 6) -> str:
        return (s[:keep] + f"...len={len(s)}") if s and len(s) > keep else (s or "")


if __name__ == "__main__":
    c = ChageeClient(env="release")
    print("device uuid:", c.uuid, "(source:", c.device.get("source"), ")")
    print("session token:", c.mask(c.token), "from", c.session_source or "none")
    sk = c.ensure_sk()
    print("sk:", c.mask(sk, 8), "plain_len:", len(c.proto.sk_plain))
    info = c.whoami()
    d = info.get("data", {})
    print("whoami errcode:", info.get("errcode"), "| customerId:", c.mask(str(d.get("customerId", "")), 4),
          "| nickName:", d.get("nickName", ""))
