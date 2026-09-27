#!/usr/bin/env python3
"""
Chagee 协议复刻骨架（业务逻辑协议化执行）

静态依据（docs/reverse_learning_log.md / docs/aot_crypto_flow.json）：
  - ChageeInterceptor.onRequest → _handleRequestPostBody → _generateSign / buildEncryptedDataFromOriginal
  - generateSign @0xb48da0: params 按 key 排序 → "k1=v1&k2=v2" → UTF-8 → HMAC(secretKey) → base64 → trim
  - 请求加密: extra.requestEncryptFields + extra.sk → base64Decode/Key → AES(encrypt pkg) → base64
  - 响应解密: extra.responseEncryptFields + extra.sk → decryptFieldsInResponse（Map/List 递归）
  - sk 经 GET /encrypt-server/enctrypt/api/getsk?code=CHAGEE_C_001 下发（2026-09-22 实测），
    响应 data = base64(16 位小写 hex 字符串)；缓存于 SP: <env>-chagee_encrypt_sp_key（存 base64 原串）
  - 运行环境 = test → 网关 https://test-gw.chagee.com，API https://test-gj-api.bwcj.com

运行时已定参数（2026-09-22 实测 + 反汇编，详见 docs/field_genealogy_20260922.md）：
  [OK1] airhub sign = base64(md5_hex("".join(groupKeys) + appId + "json" + timestamp + secret))
        secret 为各环境硬编码 JWT（见 AIRHUB_ENVS）；8778/8778 样本回归通过
  [OK2] getsk 为 GET，query code=CHAGEE_C_001；未登录期 /api/* 无 sign 头
  [OK3] 主 API sign（generateSign @0xb48da0）：
        msg = 按 key 排序的 signFields 子集拼 "k1=v1&k2=v2"（值为加密后的最终值）
        sign = base64( HMAC-SHA1( utf8(secretKey), utf8(msg) ) ).trim()
        secretKey = EncryptUtils.getSecretKey @0x8b35f0 硬编码：
          env=="release" → "9b83336464f74e148d8bd0bdcaa5f06e"
          否则（dev/test/uat）→ "686c9567b5b9e0a5ff6e0e4df88076bd"
        signFields / requestEncryptFields / responseEncryptFields 由各业务模块经
        MeService::getEncryptExtra 传入 extra（登录验证码请求 = ["sendObj","sid","timestamp"]）
  [OK4] 字段级 AES（buildEncryptedDataFromOriginal @0xb49234）：
        key = base64decode(sk_header值).encode()  → 16 字节 ASCII → AES-128
        模式 = ECB（或 CBC/IV=0，单块样本等价）；padding = PKCS7；输出 base64
        （验证样本：mobile "19900000000" ↔ "Eb4a8EnsqITU1KDkQ4YFCQ=="）
  [OK5] 端到端：message/send 请求体（加密+签名+字段序）从纯输入重建与 wire 字节一致
  [OK6] 登录态新增请求头：uuid / cid（安装标识，两值相同）/ sk（base64 形态的 sk）

运行时未定参数（TODO）：
  [RT6] AES ECB vs CBC(IV=0) 的区分（需 ≥2 块密文样本）
  [RT7] responseEncryptFields 响应解密样本（服务端 404，无响应体可验）
  [RT8] 主 API sign 的更多样本（当前 1 条 wire 精确命中 + 反汇编全链证据）
"""

import base64
import hashlib
import hmac
import json
import urllib.request
import urllib.error

# airhub 各环境凭据（chagee_global_app/init.dart @0x806644-0x806710 反汇编提取）
AIRHUB_ENVS = {
    "dev": {
        "url": "https://dev-gw.chagee.com/chagee-airhub-config-server/chagee-airhub-config-server/config/queryList",
        "app_id": "X7aWp2iie0hKGbYa",
        "secret": "eyJhbGciOiJIUzI1NiJ9.eyJhcHBTZWNyZXQiOiJbXCJnazFcIl0iLCJpYXQiOjE3NTk5OTI4MzksImV4cCI6NDkxMzU5MjgzOX0.jEnTjHXy9Zt9I1Lh8KiY91rHt0f_3FMIA9YxTxs0YBc",
        "group_key": "gk1",
    },
    "test": {
        "url": "https://test-gw.chagee.com/chagee-airhub-config-server/chagee-airhub-config-server/config/queryList",
        "app_id": "HVRk4cIj7puaOPAB",
        "secret": "eyJhbGciOiJIUzI1NiJ9.eyJhcHBTZWNyZXQiOiJbXCJnazI1XCJdIiwiaWF0IjoxNzU1NDk4MTQ3LCJleHAiOjQ5MDkwOTgxNDd9.LktnsFKf6SXAHc5-6_uBX-CHYLDRdBBfqGxhMntUFvU",
        "group_key": "gk30",
    },
    "uat": {
        "url": "https://uat-gw.chagee.com/chagee-airhub-config-server/chagee-airhub-config-server/config/queryList",
        "app_id": "wnd0cziRREY34jPw",
        "secret": "eyJhbGciOiJIUzI1NiJ9.eyJhcHBTZWNyZXQiOiJbXCJnazIzXCJdIiwiaWF0IjoxNzU1NDk4NDc0LCJleHAiOjQ5MDkwOTg0NzR9.DYT8Re8tVOzrrcoIYFKPoA3aEsshvuCeDWBW7Uo0oYc",
        "group_key": "gk26",
    },
    "prod": {
        "url": "https://api-cn.chagee.com/api/airhub-config/server/config/queryList",
        "app_id": "aeaqMI3kDgdPkFDe",
        "secret": "eyJhbGciOiJIUzI1NiJ9.eyJhcHBTZWNyZXQiOiJbXCJnazZcIl0iLCJpYXQiOjE3NTU1MDAwNjUsImV4cCI6NDkwOTEwMDA2NX0.Gpf024MfuwFcozI4672PV47NdQ8p6rfgCMbD404JLUI",
        "group_key": "gk6",
    },
}


def airhub_sign(group_keys, app_id: str, timestamp: str, secret: str) -> str:
    """AirHubSDK::_generateSign @0x5900b4 还原（2026-09-22 对拍验证）。

    parts = ["".join(groupKeys), appId, "json", timestamp, secret]
    sign  = base64(md5_hexdigest(utf8(concat(parts))))
    """
    raw = f"{''.join(group_keys)}{app_id}json{timestamp}{secret}"
    md5_hex = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return base64.b64encode(md5_hex.encode()).decode()


def verify_airhub_sign_from_flows(flows_path: str, env: str = "test") -> None:
    """用 mitmproxy flows 里的 queryList 样本回归 airhub_sign。"""
    from mitmproxy import io

    cfg = AIRHUB_ENVS[env]
    ok = bad = 0
    mism = []
    with open(flows_path, "rb") as f:
        for flow in io.FlowReader(f).stream():
            if flow.type != "http" or "queryList" not in flow.request.path:
                continue
            try:
                body = json.loads(flow.request.content)
            except Exception:
                continue
            if "sign" not in body or "timestamp" not in body:
                continue
            calc = airhub_sign(body.get("groupKeys") or [], cfg["app_id"],
                               body["timestamp"], cfg["secret"])
            if calc == body["sign"]:
                ok += 1
            else:
                bad += 1
                mism.append((body["timestamp"], body.get("groupKeys"), body["sign"], calc))
    print(f"airhub sign regression: {ok} ok / {bad} mismatch ({flows_path})")
    for m in mism[:5]:
        print("  MISMATCH", m)


class ChageeProtocol:
    BASE_API = "https://test-gj-api.bwcj.com"
    BASE_GW = "https://test-gw.chagee.com"
    GETSK_CODE = "CHAGEE_C_001"

    # 未登录启动期实测的 13 个固定头（docs/field_genealogy_20260922.md）
    BASE_HEADERS = {
        "ua": "Dart/2.12 (dart:io)",
        "user-agent": "Dart/3.6 (dart:io)",
        "avc": "638",
        "tcode": "CHAGEE",
        "channel": "APP",
        "os": "android",
        "aid": "100001",
        "language": "zh_CN",
        "region": "CN",
        "devicetimezoneregion": "Asia/Shanghai",
        "content-type": "application/json",
    }

    def __init__(self, sk: str = "", secret_key: str = ""):
        self.sk = sk                    # SP 值（base64 形态）或解出后的 16 字符 hex 串
        self.secret_key = secret_key    # 主 API HMAC 密钥（已定：环境硬编码，见 SECRET_KEYS）
        self.token = ""                 # login_userLoginToken（裸 JWT，Authorization 头原样携带）

    # ---------- 登录态请求（2026-09-22 生产实测） ----------
    # Authorization 头携带 SP 键 login_userLoginToken 的裸 JWT 值（无 Bearer 前缀），
    # 来源链：NetHeaderService.headers @0xb49c84 → MYLoginService::getLoginToken @0xb7f4c4
    # → SpUtil::getString("login_userLoginToken")。token 失效(401/12320120400401)仅踢回登录，无刷新。

    def auth_headers(self) -> dict:
        h = dict(self.BASE_HEADERS)
        h["uuid"] = h.setdefault("uuid", "")
        if self.sk:
            h["sk"] = self.sk
        if self.token:
            h["authorization"] = self.token
        return h

    def query_user_info(self, uuid: str, base_gw: str = "https://gw.chagee.com") -> dict:
        """GET /user-client/customer/userInfo/query（只读）。uuid/cid 为安装标识。"""
        h = self.auth_headers()
        h["uuid"] = h["cid"] = uuid
        req = urllib.request.Request(f"{base_gw}/user-client/customer/userInfo/query",
                                     headers=h, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"http_error": e.code, "body": e.read().decode(errors="replace")[:300]}

    # ---------- sk 获取（2026-09-22 实测形态） ----------
    def get_sk(self) -> dict:
        """GET {BASE_API}/encrypt-server/enctrypt/api/getsk?code=CHAGEE_C_001
        → {"errcode":"0","data":"<base64(16hex)>"}；data 解 base64 即 sk 明文。"""
        url = f"{self.BASE_API}/encrypt-server/enctrypt/api/getsk?code={self.GETSK_CODE}"
        req = urllib.request.Request(url, headers=self.BASE_HEADERS, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
                if resp.get("errcode") == "0" and resp.get("data"):
                    self.sk = resp["data"]  # 保持 base64 形态（与 SP 落盘一致）
                return resp
        except urllib.error.HTTPError as e:
            return {"error": e.code, "body": e.read().decode(errors="replace")}

    @property
    def sk_plain(self) -> str:
        """16 字符 hex 串（如 f7346022c2d57c81）。"""
        return base64.b64decode(self.sk).decode() if self.sk else ""

    # ---------- 签名（主 API，已验证：HMAC-SHA1） ----------
    SECRET_KEYS = {
        "release": "9b83336464f74e148d8bd0bdcaa5f06e",
        "default": "686c9567b5b9e0a5ff6e0e4df88076bd",  # dev/test/uat
    }

    def generate_sign(self, fields: dict, env: str = "test") -> str:
        """generateSign @0xb48da0 还原（2026-09-22 wire 精确验证）。

        msg = sorted kv join "&"（fields 为 signFields 子集，值用加密后的最终值）
        sign = base64(HMAC-SHA1(secretKey, msg)).trim()
        """
        keys = sorted(fields.keys())
        msg = "&".join(f"{k}={fields[k]}" for k in keys)
        secret = self.SECRET_KEYS["release"] if env == "release" else self.SECRET_KEYS["default"]
        mac = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha1)
        return base64.b64encode(mac.digest()).decode().strip()

    # ---------- 字段级 AES（已验证：AES-128-ECB + PKCS7） ----------
    def _key(self):
        return self.sk_plain.encode("utf-8")  # base64decode(sk) → 16 字节 ASCII

    def encrypt_field(self, plain: str) -> str:
        """buildEncryptedDataFromOriginal @0xb49234（wire 验证：无 IV 前缀，单块=ECB）。"""
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        c = AES.new(self._key(), AES.MODE_ECB)
        return base64.b64encode(c.encrypt(pad(plain.encode(), 16))).decode()

    def decrypt_field(self, b64: str) -> str:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        c = AES.new(self._key(), AES.MODE_ECB)
        return unpad(c.decrypt(base64.b64decode(b64)), 16).decode()

    def build_login_sms_body(self, phone: str, sid: str, ts: int, scene: str = "login") -> dict:
        """端到端重建 message/send 请求体（2026-09-22 与 wire 字节一致）。"""
        enc_phone = self.encrypt_field(phone)
        body = {
            "scene": scene,
            "mobile": enc_phone,
            "sendObj": enc_phone,
            "phoneCode": "86",
            "blockParam": "不验证",
            "sid": sid,
            "timestamp": ts,
        }
        body["sign"] = self.generate_sign({"sendObj": enc_phone, "sid": sid, "timestamp": ts})
        return body

    def decrypt_fields_in_response(self, obj, fields: set):
        """decryptFieldsInResponse @0xb21ba8：对 Map/List 递归，命中字段名则解密。"""
        if isinstance(obj, dict):
            return {k: (self.decrypt_field(v) if k in fields and isinstance(v, str) else self.decrypt_fields_in_response(v, fields)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.decrypt_fields_in_response(x, fields) for x in obj]
        return obj


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "verify-airhub":
        verify_airhub_sign_from_flows(sys.argv[2])
    else:
        p = ChageeProtocol()
        demo = {"b": "2", "a": "1", "sign": "x"}
        print("主 API sign 拼接顺序:", "&".join(f"{k}={demo[k]}" for k in sorted(demo)))
        print("airhub sign（test 环境，样本 ts=1790062348564）:",
              airhub_sign([], AIRHUB_ENVS["test"]["app_id"], "1790062348564", AIRHUB_ENVS["test"]["secret"]))
        print("待补运行时参数: RT1-RT5（见文件头注释）；验证命令: python chagee_protocol.py verify-airhub <flows>")
