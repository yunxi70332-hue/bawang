# -*- coding: utf-8 -*-
"""alipay_msp_client 离线测试：组帧/加密/解析 roundtrip + 抓包实证比对。

运行：python tests/test_msp_client_offline.py（不触网）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from alipay_msp_client import (  # noqa: E402
    DEFAULT_DEVICE, MspError, RSA_PUBKEY_B64, build_msp_param, build_request_bytes,
    build_user_agent, des3_cbc_decrypt, des3_cbc_encrypt, extract_cashier_url,
    extract_tid_ops, frame, parse_response, probe_url, unframe, _gen_key,
    _gzip_like_java, rsa_encrypt_key,
)

WIRE_ENVELOPE = (
    '{"data":{"api_name":"com.alipay.mcpay","namespace":"com.alipay.mobilecashier",'
    '"api_version":"4.9.0","device":"HUAWEI NXT-AL10","params":{}}}'
)
SAMPLE_ORDER_STR = (
    'alipay_sdk=alipay-sdk-java-4.40.237.ALL&app_id=2018080860981451'
    '&biz_content=%7B%22out_trade_no%22%3A%22331L20260926100098716809036%22%2C'
    '%22total_amount%22%3A%2210.00%22%7D&charset=UTF-8&method=alipay.trade.app.pay'
    '&sign=AbCdEf&sign_type=RSA2&timestamp=2026-09-26+16%3A40%3A49'
)
WIRE_MSP_PARAM_PREFIX = "trade_no=%7B%22out_trade_no%22"


def test_gzip_java_compat():
    data = '{"hello":"世界","n":1}'.encode("utf-8")
    gz = _gzip_like_java(data)
    assert gz[:10] == b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x00"
    import gzip
    assert gzip.decompress(gz) == data


def test_des3_roundtrip():
    key = _gen_key(24)
    data = b"x" * 137
    enc = des3_cbc_encrypt(key, data)
    assert len(enc) % 8 == 0 and len(enc) > len(data)
    assert des3_cbc_decrypt(key, enc) == data


def test_des3_iv_zero_first_block_stable():
    # 同密钥+零IV+相同明文首块 → 密文首块相同（抓包自证规律）
    key = _gen_key(24)
    a = des3_cbc_encrypt(key, b"AAAAAAAA" + b"1" * 16)
    b = des3_cbc_encrypt(key, b"AAAAAAAA" + b"2" * 16)
    assert a[:8] == b[:8]


def test_rsa_block_128():
    blk = rsa_encrypt_key(_gen_key(24))
    assert len(blk) == 128
    blk2 = rsa_encrypt_key(_gen_key(24))
    assert blk != blk2  # PKCS1 v1.5 随机填充


def test_frame_unframe():
    framed = frame([b"ab", b"", b"12345"])
    assert framed == b"00002ab00000" + b"0000512345"
    assert unframe(framed) == [b"ab", b"", b"12345"]


def test_build_request_structure():
    body, key = build_request_bytes(SAMPLE_ORDER_STR, DEFAULT_DEVICE)
    parts = unframe(body)
    assert len(parts) == 3
    assert parts[0].decode() == WIRE_ENVELOPE  # 与抓包 flow[50] 逐字节一致
    assert len(parts[1]) == 128
    assert len(parts[2]) % 8 == 0


def test_response_roundtrip():
    key = _gen_key(24)
    inner = '{"end_code":"200","data":{"form":{"onload":["openWeb(\'https://mclient.alipay.com/cashierRoutePay.htm?session=RZZFB00test\')"]}}}'
    cipher = des3_cbc_encrypt(key, _gzip_like_java(inner.encode("utf-8")))
    env = '{"data":{"api_name":"com.alipay.mcpay","api_version":"4.9.0","code":"0","namespace":"com.alipay.mobilecashier"}}'
    raw = frame([env.encode(), cipher])
    envelope, body_str = parse_response(raw, key)
    assert envelope["data"]["code"] == "0"
    assert json.loads(body_str)["end_code"] == "200"


def test_msp_param_from_order_str():
    param = build_msp_param(SAMPLE_ORDER_STR)
    assert param.startswith(WIRE_MSP_PARAM_PREFIX)


def test_user_agent_shape():
    import re

    ua = build_user_agent(dict(DEFAULT_DEVICE))
    assert ua.startswith("Msp/15.8.35 (Android 10;Linux ")
    assert "HUAWEI NXT-AL10" in ua and ua.endswith(")")
    # AT 后缀形态为 ");;;<token>"（o/b.java:195 紧跟右括号），空指纹段产生的 ;; 不是 AT
    assert not re.search(r"\);;;", ua)
    ua_at = build_user_agent(dict(DEFAULT_DEVICE, at_token="TOKEN123"))
    assert ua_at.endswith(");;;TOKEN123")


def test_extract_cashier_url_and_tid():
    body = {
        "data": {"form": {"onload": [
            "tid('2e80dd74abc','clientkey123')",
            "openWeb('https://mclient.alipay.com/cashierRoutePay.htm?route_pay_from=h5&session=RZZFB000tXXHTmobilecashierRZZFB00&cc=y')",
        ]}}
    }
    url = extract_cashier_url(body)
    assert url and url.startswith("https://mclient.alipay.com/cashierRoutePay.htm")
    ops = extract_tid_ops(body)
    assert ops and ops[0]["tid"] == "2e80dd74abc" and ops[0]["client_key"] == "clientkey123"


def test_msp_error_on_bad_frame():
    try:
        parse_response(b"99999xxxxx", _gen_key(24))
        raise AssertionError("should raise")
    except MspError:
        pass


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
