# -*- coding: utf-8 -*-
"""支付宝 msp/mcpay 通道纯协议客户端 —— 脱离云手机设备铸造 H5 收银台链接。

原理（逆向自 APK 内嵌支付宝 SDK 15.8.35，全部纯 Java 无 native 参与）：
  PayTask.pay(orderStr) → POST https://mobilegw.alipay.com/mgw.htm
    Operation-Type: alipay.msp.cashier.dispatch.bytes (User-Agent: msp)
    报文三段式，5 位十进制 ASCII 长度前缀（m/s/c.java:253）：
      [envelope 明文 JSON] [RSA-1024/PKCS1v1.5 加密的 24 位会话密钥(128B)]
      [3DES-CBC(PKCS5, IV=8×0x00) 加密的 gzip(body JSON)]
    会话密钥每次客户端自生成（m/s/c.java:13 → m/x/o.java:579），RSA 公钥
    硬编码于 SDK（m/n/a.java:15），响应用同一把密钥回加密 → 自发自解。
    响应 body.data.form.onload[] 为 JS 片段数组，openWeb('<cashierRoutePay
    URL>') 即收银台入口链接，tid('t','k') 下发设备凭据。

证据链：docs/cashier_link_assembly_20260927.md、capture/
chagee_native_phase0b_20260926.flows flow[50]、decompiled/jadx/
sources/com/alipay/sdk/m/**。设备凭据默认值取自该云手机抓包（设备级恒定）。

用法：
  from alipay_msp_client import mint_cashier_link
  result = mint_cashier_link(order_str)
  result["ok"] → result["url"] = https://mclient.alipay.com/cashierRoutePay.htm?...
CLI：python alipay_msp_client.py --order-str "..." [--probe]
"""
from __future__ import annotations

import gzip as _gzip
import io
import json
import random
import re
import string
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from Crypto.Cipher import DES3, PKCS1_v1_5
from Crypto.PublicKey import RSA

ROOT = Path(__file__).resolve().parent.parent

MGW_URL = "https://mobilegw.alipay.com/mgw.htm"
OPERATION_TYPE = "alipay.msp.cashier.dispatch.bytes"
API_VERSION = "4.9.0"
NAMESPACE = "com.alipay.mobilecashier"
API_NAME = "com.alipay.mcpay"
APP_ID_HEADER = "TAOBAO"
SDK_VERSION = "15.8.35"
APP_KEY = "2014052600006128"  # m/n/a.java:16

# SDK 硬编码 RSA-1024 公钥（m/n/a.java:15），服务端可经响应 params.public_key 轮换
RSA_PUBKEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDENksAVqDoz5SMCZq0bsZwE+I3NjrANyTTwUVS"
    "f1+ec1PfPB4tiocEpYJFCYju9MIbawR8ivECbUWjpffZq5QllJg+19CB7V5rYGcEnb/M7CS3lFF2s"
    "NcRFJUtXUUAqyR3/l7PmpxTwObZ4DLG258dhE2vFlVGXjnuLs+FI2hg4QIDAQAB"
)

DEFAULT_DEVICE: Dict[str, Any] = {
    # —— 抓包实证的设备级恒定凭据（capture flow[48]/[50]）——
    "utdid": "ard6vj24ouoDAPwfC0RN0/hz",
    "tid": "2e80dd74198790910ee488e446e945e319c838fd490f3d80dcddeae2fe3827aa",
    "client_key": "",
    "apdid": "eYOIkpU3MXLR9+/AHWo7FtdL1shtHBWf9oP8gMDZIIQy168sgQrlYTcg",
    "apdid_token": "7EKcSbYoH7g4XxfN2tqLneszNQVrhba0InMBCjjg/do+eLfcoAEAAA==",
    "at_token": "",
    # —— 机型指纹（WebView UA: Android 10 / HUAWEI NXT-AL10）——
    "model": "HUAWEI NXT-AL10",
    "manufacturer": "HUAWEI",
    "android_release": "10",
    "kernel": "4.14.180",
    "locale": "zh_CN",
    "screen": "1080*1812",
    "text_size": "42.0",
    "package": "com.chagee.application.cn",
    "app_version_code": "103",
}


class MspError(Exception):
    """纯协议铸造失败（网络/加密/协议/服务端拒绝）。"""


# ---------------------------------------------------------------- 加密原语
def _gen_key(n: int = 24) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def _rsa_pubkey(pem_b64: str):
    import base64
    try:
        der = base64.b64decode(pem_b64)
    except Exception as exc:  # pragma: no cover
        raise MspError(f"bad pubkey base64: {exc}") from exc
    return RSA.import_key(der)


def rsa_encrypt_key(session_key: str, pubkey_b64: str = RSA_PUBKEY_B64) -> bytes:
    cipher = PKCS1_v1_5.new(_rsa_pubkey(pubkey_b64))
    return cipher.encrypt(session_key.encode("ascii"))


def _gzip_like_java(data: bytes) -> bytes:
    """Java GZIPOutputStream 兼容 gzip（mtime=0, XFL=0, OS=0, level 6）。"""
    co = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = co.compress(data) + co.flush()
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x00"
    trailer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    return header + body + trailer


def des3_cbc_encrypt(key: str, data: bytes, iv: bytes = b"\x00" * 8) -> bytes:
    cipher = DES3.new(key.encode("ascii"), DES3.MODE_CBC, iv=iv)
    pad = 8 - len(data) % 8
    return cipher.encrypt(data + bytes([pad]) * pad)


def des3_cbc_decrypt(key: str, data: bytes, iv: bytes = b"\x00" * 8) -> bytes:
    cipher = DES3.new(key.encode("ascii"), DES3.MODE_CBC, iv=iv)
    raw = cipher.decrypt(data)
    if raw and 1 <= raw[-1] <= 8:
        raw = raw[: -raw[-1]]
    return raw


def frame(parts) -> bytes:
    out = io.BytesIO()
    for p in parts:
        out.write(b"%05d" % len(p))
        out.write(p)
    return out.getvalue()


def unframe(raw: bytes):
    """拆 [5位长度][payload]...，返回 payload 列表。"""
    parts, pos = [], 0
    while pos + 5 <= len(raw):
        n = int(raw[pos : pos + 5])
        pos += 5
        if pos + n > len(raw):
            raise MspError(f"frame length {n} overruns buffer at {pos}")
        parts.append(raw[pos : pos + n])
        pos += n
    return parts


# ---------------------------------------------------------------- 报文构造
def build_user_agent(device: Dict[str, Any]) -> str:
    """复刻 m/o/b.java:132-197 的 msp user_agent（';' 分段设备指纹）。

    段序固定：Msp/<sdk> (Android;Linux-kernel;locale;scheme;screen;textSize);
    imsiCarrier;-1;-1;1;imsi;imei;client_key;manufacturer;model;hasWallet;
    deviceId;-1;-1;sdk-and-lite;vimsi;vimei;-1;?;;;AT)
    """
    ck = device.get("client_key") or _fallback_client_key()
    vimsi = device.get("vimsi") or _fallback_virtual_id()
    vimei = device.get("vimei") or _fallback_virtual_id()
    segments = [
        # this.a（o/b.java:136）：Msp/<sdk> ( 后直接接 6 段，"(" 不参与分隔
        "Android " + device.get("android_release", "10"),
        "Linux " + device.get("kernel", "4.14.180"),
        device.get("locale", "zh_CN"),
        "https",
        device.get("screen", "1080*1812"),
        device.get("text_size", "42.0"),
        # 尾部 16 段（o/b.java:154-187）
        device.get("imsi_carrier", "-1;-1"),
        "-1;-1",
        "1",
        device.get("imsi", ""),
        device.get("imei", ""),
        ck,
        device.get("manufacturer", "HUAWEI").replace(";", " "),
        device.get("model", "HUAWEI NXT-AL10").replace(";", " "),
        "false",
        device.get("device_id", ""),
        "-1;-1",
        "sdk-and-lite",
        vimsi,
        vimei,
        "-1",
        "?",
    ]
    ua = f"Msp/{SDK_VERSION} (" + ";".join(segments) + ")"
    at = device.get("at_token") or ""
    if at:
        ua += ";;;" + at
    return ua


def _fallback_client_key() -> str:
    """w/a.java:45-47 本地降级值：hex(当前毫秒) + 4 位随机。"""
    return format(int(time.time() * 1000), "x") + str(random.randint(1000, 9999))


def _fallback_virtual_id() -> str:
    """o/b.java:47-49：hex(当前毫秒) + 4 位随机。"""
    return format(int(time.time() * 1000), "x") + str(random.randint(1000, 9999))


def build_body(order_str: str, device: Dict[str, Any]) -> Dict[str, Any]:
    """加密体内层 body（m/s/e.java:75-94 组装序）。"""
    body = {
        "action": {"type": "cashier", "method": "main"},
        "external_info": order_str,
        "tid": device.get("tid", ""),
        "user_agent": build_user_agent(device),
        "has_alipay": False,
        "has_msp_app": False,
        "app_key": APP_KEY,
        "utdid": device.get("utdid", ""),
        "new_client_key": device.get("client_key") or "",
        "pa": "{%s#%s}" % (device.get("package", "com.chagee.application.cn"),
                           device.get("app_version_code", "103")),
    }
    return body


def build_envelope(device: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "data": {
            "api_name": API_NAME,
            "namespace": NAMESPACE,
            "api_version": API_VERSION,
            "device": device.get("model", "HUAWEI NXT-AL10"),
            "params": {},
        }
    }


def build_msp_param(order_str: str) -> str:
    """公共头 Msp-Param（m/s/a.java + 抓包实证）：trade_no=<urlencoded biz_content>。"""
    biz = ""
    for kv in order_str.split("&"):
        if kv.startswith("biz_content="):
            biz = kv.split("=", 1)[1]
            break
    if not biz:
        raise MspError("order_str missing biz_content")
    return "trade_no=" + biz


def _json_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def build_request_bytes(order_str: str, device: Dict[str, Any],
                        pubkey_b64: str = RSA_PUBKEY_B64,
                        ) -> Tuple[bytes, str]:
    """返回 (HTTP body, 会话密钥)。"""
    key = _gen_key(24)
    envelope = _json_compact(build_envelope(device)).encode("utf-8")
    plain = _json_compact(build_body(order_str, device)).encode("utf-8")
    payload = des3_cbc_encrypt(key, _gzip_like_java(plain))
    body = frame([envelope, rsa_encrypt_key(key, pubkey_b64), payload])
    return body, key


def parse_response(raw: bytes, session_key: str) -> Tuple[Dict[str, Any], str]:
    """解响应帧：[envelope JSON][3DES 密文] → gunzip → body JSON 字符串。"""
    parts = unframe(raw)
    if len(parts) < 2:
        raise MspError(f"response frame parts={len(parts)} (expect 2)")
    try:
        envelope = json.loads(parts[0].decode("utf-8"))
    except Exception as exc:
        raise MspError(f"envelope decode failed: {exc}") from exc
    plain = des3_cbc_decrypt(session_key, parts[1])
    try:
        body = _gzip_decompress(plain).decode("utf-8")
    except Exception as exc:
        raise MspError(f"body decrypt/gunzip failed: {exc}") from exc
    return envelope, body


def _gzip_decompress(data: bytes) -> bytes:
    try:
        return _gzip.decompress(data)
    except OSError:
        return zlib.decompress(data, 16 + zlib.MAX_WBITS)


# ---------------------------------------------------------------- HTTP 调用
def _post(raw_body: bytes, msp_param: str, timeout: float = 20.0):
    import requests

    headers = {
        "User-Agent": "msp",
        "Accept-Charset": "UTF-8",
        "Connection": "Keep-Alive",
        "Keep-Alive": "timeout=180, max=100",
        "AppId": APP_ID_HEADER,
        "Version": "2.0",
        "content-type": "application/octet-stream",
        "msp-gzip": "true",
        "des-mode": "CBC",
        "Operation-Type": OPERATION_TYPE,
        "Msp-Param": msp_param,
        "Accept-Encoding": "gzip",
    }
    resp = requests.post(MGW_URL, data=raw_body, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise MspError(f"http {resp.status_code}: {resp.text[:200]}")
    result_status = resp.headers.get("Result-Status", "")
    if result_status and result_status != "1000":
        raise MspError(
            f"Result-Status {result_status} memo={resp.headers.get('Memo', '')} "
            f"msp_param={resp.headers.get('Msp-Param', '')}"
        )
    return resp.content, dict(resp.headers)


def _check_pubkey_rotation(envelope: Dict[str, Any]) -> Optional[str]:
    try:
        pk = envelope.get("data", {}).get("params", {}).get("public_key")
        return pk or None
    except Exception:
        return None


def dispatch_cashier(order_str: str, device: Optional[Dict[str, Any]] = None,
                     timeout: float = 20.0, allow_retry: bool = True,
                     ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """发起一次 mcpay cashier/main 铸造调用，返回 (envelope, body_json_dict)。

    支持公钥轮换：响应 envelope 带 params.public_key 时换钥整包重发（m/s/e.java:170）。
    """
    device = _merge_device(device)
    msp_param = build_msp_param(order_str)
    pubkey = load_runtime_pubkey()
    body, key = build_request_bytes(order_str, device, pubkey)
    raw, _hdrs = _post(body, msp_param, timeout)
    envelope, body_str = parse_response(raw, key)
    new_pk = _check_pubkey_rotation(envelope)
    if new_pk and allow_retry:
        save_runtime_pubkey(new_pk)
        return dispatch_cashier(order_str, device, timeout, allow_retry=False)
    try:
        body_json = json.loads(body_str)
    except Exception as exc:
        raise MspError(f"body json decode failed: {exc}; raw[:200]={body_str[:200]!r}") from exc
    return envelope, body_json


# ---------------------------------------------------------------- 链接提取
CASHIER_URL_RE = re.compile(r"https?://mclient\.alipay\.com/[^\s'\"<>()]+")
TID_OP_RE = re.compile(r"tid\('([^']*)'\s*,\s*'([^']*)'\)")


def extract_cashier_url(body_json: Dict[str, Any]) -> Optional[str]:
    """从 data.form.onload[] JS 片段中提取 cashierRoutePay / mclient 收银台 URL。"""
    try:
        onload = body_json["data"]["form"]["onload"]
    except (KeyError, TypeError):
        return None
    items = onload if isinstance(onload, list) else [onload]
    for item in items:
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        m = CASHIER_URL_RE.search(text)
        if m:
            return m.group(0)
    return None


def extract_tid_ops(body_json: Dict[str, Any]):
    """服务端下发的 tid/client_key（u/b.java:19-25），首次铸造后落 profile 复用。"""
    try:
        onload = body_json["data"]["form"]["onload"]
    except (KeyError, TypeError):
        return []
    items = onload if isinstance(onload, list) else [onload]
    ops = []
    for item in items:
        text = item if isinstance(item, str) else ""
        for m in TID_OP_RE.finditer(text):
            ops.append({"tid": m.group(1), "client_key": m.group(2)})
    return ops


def build_cashier_route_url(session: str, device: Dict[str, Any]) -> str:
    """已知 session 时按 SDK 常量拼装入口（m/t/d.java:44-46 + 抓包定案）。"""
    from urllib.parse import urlencode

    q = urlencode(
        {
            "route_pay_from": "h5",
            "init_from": "SDKLite",
            "session": session,
            "utdid": device.get("utdid", ""),
            "tid": device.get("tid", ""),
            "cc": "y",
        }
    )
    return f"https://mclient.alipay.com/cashierRoutePay.htm?{q}"


# ---------------------------------------------------------------- 设备档案
PROFILE_PATH = ROOT / "account_system" / "data" / "msp_device_profile.json"
_PUBKEY_PATH = ROOT / "account_system" / "data" / "msp_runtime_pubkey.txt"


def _merge_device(device: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_DEVICE)
    profile = load_device_profile()
    if profile:
        merged.update({k: v for k, v in profile.items() if v})
    if device:
        merged.update({k: v for k, v in device.items() if v})
    return merged


def load_device_profile() -> Optional[Dict[str, Any]]:
    try:
        if PROFILE_PATH.exists():
            return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def save_device_profile(patch: Dict[str, Any]) -> None:
    profile = load_device_profile() or {}
    profile.update({k: v for k, v in patch.items() if v})
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_runtime_pubkey() -> str:
    try:
        if _PUBKEY_PATH.exists():
            pk = _PUBKEY_PATH.read_text(encoding="utf-8").strip()
            if pk:
                return pk
    except Exception:
        pass
    return RSA_PUBKEY_B64


def save_runtime_pubkey(pk: str) -> None:
    _PUBKEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PUBKEY_PATH.write_text(pk, encoding="utf-8")


# ---------------------------------------------------------------- 顶层接口
def mint_cashier_link(order_str: str, device: Optional[Dict[str, Any]] = None,
                      timeout: float = 20.0, probe: bool = False) -> Dict[str, Any]:
    """orderStr → 官方 H5 收银台链接（纯协议，无需云手机）。

    返回 {ok, url, session?, error?, result_status, body?}。
    probe=True 时 GET 校验链接 302→h5pay/landing 判活。
    """
    result: Dict[str, Any] = {"ok": False, "url": None}
    try:
        dev = _merge_device(device)
        envelope, body_json = dispatch_cashier(order_str, dev, timeout)
        result["body"] = body_json
        result["control_type"] = body_json.get("control_type")
        url = extract_cashier_url(body_json)
        if not url and body_json.get("session"):
            # need_phonelogin 等形态直接下发 session（在线实证 2026-09-27）
            url = build_cashier_route_url(body_json["session"], dev)
        tid_ops = extract_tid_ops(body_json)
        if tid_ops:
            save_device_profile(tid_ops[0])
        if not url:
            end_code = body_json.get("end_code")
            result["error"] = f"no cashier url in response (end_code={end_code})"
            return result
        result["url"] = url
        result["session"] = _extract_session(url)
        if probe:
            result["probe"] = probe_url(url)
            if not result["probe"].get("alive"):
                result["error"] = "session not alive: " + str(result["probe"])
                return result
        result["ok"] = True
        return result
    except MspError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:  # 网络/库异常统一包装
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def _extract_session(url: str) -> Optional[str]:
    m = re.search(r"[?&]session=([^&]+)", url)
    return m.group(1) if m else None


def probe_url(url: str, timeout: float = 10.0) -> Dict[str, Any]:
    """判活标准与 extract_cashier_link.probe_url 一致：302 落 h5pay/landing=活。"""
    import requests

    ua = (
        "Mozilla/5.0 (Linux; Android 10; HUAWEI NXT-AL10 Build/QD4A.200805.003; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/119.0.6045.134 "
        "Mobile Safari/537.36"
    )
    try:
        resp = requests.get(url, headers={"User-Agent": ua}, timeout=timeout,
                            allow_redirects=True)
        final = resp.url
        alive = "h5pay/landing" in final
        return {"alive": alive, "status": resp.status_code, "final_url": final[:300]}
    except Exception as exc:
        return {"alive": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- CLI
def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="纯协议铸造支付宝 H5 收银台链接")
    parser.add_argument("--order-str", help="完整 orderStr（alipay_sdk=...&sign=...）")
    parser.add_argument("--order-no", help="从系统库 PaySession 取 order_str")
    parser.add_argument("--probe", action="store_true", help="铸造后判活")
    parser.add_argument("--dump-body", action="store_true")
    args = parser.parse_args()

    order_str = args.order_str
    if not order_str and args.order_no:
        import sqlite3

        db_path = ROOT / "account_system" / "data" / "app.db"
        con = sqlite3.connect(db_path)
        row = con.execute(
            "select order_str from pay_sessions where order_no=? and order_str is not null "
            "order by id desc limit 1", (args.order_no,)).fetchone()
        con.close()
        order_str = row[0] if row else None
    if not order_str:
        parser.error("need --order-str or --order-no")
        return 2

    t0 = time.time()
    result = mint_cashier_link(order_str, probe=args.probe)
    result["elapsed_s"] = round(time.time() - t0, 2)
    body = result.pop("body", None)
    if args.dump_body and body is not None:
        result["body_dump"] = body
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if body is not None and not args.dump_body:
        print("--- body keys:", list(body.keys()) if isinstance(body, dict) else type(body))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(_cli())
