"""alipay_autopay 离线单测（不发任何网络包，transport 全部 mock/门限制路径）。

覆盖：
  - parse_order_str 对 wire 样本 orderStr 的解析断言（out_trade_no/total_amount/app_id 等）
  - build_cashier_url 与样本 h5_cashier_url 的形态比对（同参逐字节复现 + 形态字段）
  - autopay 无配置文件 → needs_interaction（含模板说明）
  - autopay 零网络（有配置但未 enable_network）→ needs_interaction，不发包
  - drive_cashier 登录墙折返（mock transport 重放 wire 形态：route→landing→unified_login）
  - UrllibTransport 安全门：支付提交端点在发 socket 前被 PaySubmitBlocked 拦截
  - orderStr 非法 → failed

wire 夹具（真实生产存档，只读回放）：output/alipay_pay_link_20260926.json。
"""
import json
import urllib.parse
from pathlib import Path

import pytest

from scripts.alipay_autopay import (
    CASHIER_ROUTE_PATH,
    EP_CASHIER_PAY,
    ORDER_STR_SAMPLE,
    PaySubmitBlocked,
    Transport,
    TransportResponse,
    UrllibTransport,
    autopay,
    build_cashier_url,
    drive_cashier,
    parse_order_str,
)

OUT = Path(__file__).resolve().parents[1] / "output"
WIRE = json.loads((OUT / "alipay_pay_link_20260926.json").read_text(encoding="utf-8"))
ORDER_STR = WIRE["orderStr"]
H5_CASHIER_URL = WIRE["h5_cashier_url"]


# ---------------- parse_order_str ----------------

def test_parse_order_str_wire_fields():
    parsed = parse_order_str(ORDER_STR)
    assert parsed["out_trade_no"] == "331L20260926100098716809036"
    assert parsed["total_amount"] == "10.00"
    assert parsed["app_id"] == "2018080860981451"
    assert parsed["method"] == "alipay.trade.app.pay"
    assert parsed["sign_type"] == "RSA2"
    assert parsed["product_code"] == "QUICK_MSECURITY_PAY"
    assert parsed["charset"] == "UTF-8"
    assert parsed["subject"] == "CHAGEE霸王茶姬（湖南衡阳常宁东风广场店）"


def test_parse_order_str_matches_archive_biz_content():
    """解析出的 biz_content 平铺字段须与存档 biz_content 逐字段一致。"""
    parsed = parse_order_str(ORDER_STR)
    for key, val in WIRE["biz_content"].items():
        if key == "time_expire":       # 存档对 + 做了还原（parse_qsl 解码 '+' 为空格），比对语义值
            assert parsed["time_expire"].replace(" ", "+") == val
        else:
            assert parsed[key] == val, key


def test_parse_order_str_notify_return_chinaums():
    parsed = parse_order_str(ORDER_STR)
    assert urllib.parse.urlsplit(parsed["notify_url"]).netloc == "qr-wh.chinaums.com"
    assert "/h5PayResult.do/" in parsed["return_url"]


def test_parse_order_str_garbage_raises():
    with pytest.raises(Exception):
        parse_order_str("not-an-orderstr")
    with pytest.raises(Exception):
        parse_order_str("")


# ---------------- build_cashier_url ----------------

def test_build_cashier_url_shape_matches_wire():
    """形态比对：scheme/host/path 一致，query 参数名集合一致。"""
    url = build_cashier_url(parse_order_str(ORDER_STR))
    got, want = urllib.parse.urlsplit(url), urllib.parse.urlsplit(H5_CASHIER_URL)
    assert (got.scheme, got.netloc, got.path) == (want.scheme, want.netloc, want.path)
    assert got.path == CASHIER_ROUTE_PATH
    gq = dict(urllib.parse.parse_qsl(got.query, keep_blank_values=True))
    wq = dict(urllib.parse.parse_qsl(want.query, keep_blank_values=True))
    assert set(gq) == set(wq)
    assert gq["route_pay_from"] == "h5" and gq["init_from"] == "SDKLite" and gq["cc"] == "y"


def test_build_cashier_url_reproduces_wire_with_same_session():
    """喂入 wire 的 session/utdid/tid → 与样本 h5_cashier_url 逐字节一致（含 utdid 的 '/' 不转义）。"""
    wq = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(H5_CASHIER_URL).query,
                                     keep_blank_values=True))
    url = build_cashier_url(parse_order_str(ORDER_STR),
                            session=wq["session"], utdid=wq["utdid"], tid=wq["tid"])
    assert url == H5_CASHIER_URL


def test_build_cashier_url_placeholder_marks():
    url = build_cashier_url(parse_order_str(ORDER_STR))
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["session"].startswith("PLACEHOLDER")


# ---------------- autopay：配置缺省路径 ----------------

def test_autopay_needs_interaction_without_config(tmp_path, monkeypatch):
    import scripts.alipay_autopay as mod
    missing = tmp_path / "nope" / "autopay_config.json"
    monkeypatch.setattr(mod, "DEFAULT_CONFIG_PATH", missing)
    r = mod.autopay(ORDER_STR)          # 不传 config → 走被替换的默认路径
    assert r["status"] == "needs_interaction"
    assert r["ok"] is False
    assert "autopay_config.json" in r["message"]
    tpl = r["detail"]["config_template"]
    assert "session_cookies" in tpl
    assert tpl["pay_password_hint"] == "H5 收银台若需密码将失败并返回 needs_interaction"


def test_autopay_zero_network_when_not_enabled():
    """有配置但未 enable_network 且未注入 transport → needs_interaction，绝不出网。"""
    r = autopay(ORDER_STR, config={"cashier": {"session": "RZZFB00..."}})
    assert r["status"] == "needs_interaction"
    assert "enable_network" in r["message"]
    assert r["detail"]["session_source"] == "config"
    assert r["detail"]["cashier_url"].startswith("https://mclient.alipay.com/cashierRoutePay.htm")


def test_autopay_missing_cashier_session_even_if_network_enabled():
    r = autopay(ORDER_STR, config={"enable_network": True})
    assert r["status"] == "needs_interaction"
    assert r["detail"]["session_source"] == "placeholder"
    assert "session" in r["message"]


def test_autopay_bad_order_str_failed():
    r = autopay("garbage", config={})
    assert r["status"] == "failed"
    assert r["ok"] is False


# ---------------- drive_cashier：mock transport 重放 wire 形态 ----------------

class MockTransport(Transport):
    """按 wire 形态回放：route GET 跟随 302 落 landing；cashierMain 返回 unified_login。"""

    def __init__(self, main_control="unified_login", landing_url=None):
        self.main_control = main_control
        self.landing_url = landing_url or WIRE["h5_landing_302"]
        self.calls: list[tuple[str, str]] = []

    def get(self, url, headers=None):
        self.calls.append(("GET", url))
        return TransportResponse(url=self.landing_url, status=200, text="<html>landing</html>")

    def post_json(self, url, payload, headers=None):
        self.calls.append(("POST", url))
        assert url.endswith("/wapcashier/api/cashierMain.json")
        assert payload["h5_request_token"]          # 取自 landing URL
        assert payload["cookieToken"]
        assert payload["device"]["userAgent"].startswith("Mozilla/5.0")
        body = {"data": {"controlType": self.main_control,
                         "bizData": {"orderAmount": "10.00"},
                         "clientLogData": {"outTradeNo": "331L..."}}}
        return TransportResponse(url=url, status=200, text=json.dumps(body))


def test_drive_cashier_login_wall():
    tp = MockTransport(main_control="unified_login")
    r = drive_cashier(H5_CASHIER_URL, session_cookies={"JSESSIONID": "x"}, transport=tp)
    assert r["status"] == "needs_interaction"
    assert r["detail"]["interaction"] == "login_wall"
    assert r["detail"]["order_amount"] == "10.00"
    names = [s["name"] for s in r["detail"]["steps"]]
    assert names == ["route_pay", "cashier_main"]
    # 每步都有请求/响应摘要
    for s in r["detail"]["steps"]:
        assert s["url"] and s["status"] and s["final_url"] and s["note"]


def test_drive_cashier_pay_password_terminal():
    """已登录态也折返：支付提交需要 spwd（RSA 支付密码），模板给出且永不发送。"""
    tp = MockTransport(main_control="cashier_home")
    r = drive_cashier(H5_CASHIER_URL, transport=tp)
    assert r["status"] == "needs_interaction"
    assert r["detail"]["interaction"] == "pay_password"
    tpl = r["detail"]["pay_submit_template"]
    assert "spwd" in tpl and "cashierPay" in tpl["_note"]
    assert tpl["spwd"].startswith("<REQUIRED_RSA_ENCRYPTED")


def test_drive_cashier_login_redirect():
    tp = MockTransport()
    tp.landing_url = "https://login.alipay.com/?redirect=1"
    r = drive_cashier(H5_CASHIER_URL, transport=tp)
    assert r["status"] == "needs_interaction"
    assert r["detail"]["interaction"] == "login_redirect"


def test_autopay_with_injected_transport_end_to_end():
    """接缝全景：注入 mock transport → 登录墙 → needs_interaction，detail 含订单摘要+步骤。"""
    tp = MockTransport(main_control="unified_login")
    r = autopay(ORDER_STR, config={"transport": tp, "cashier": {
        "session": "RZZFB000tXXHTyNxf0TbFfXfhu1xlVmobilecashierRZZFB00"}})
    assert r["status"] == "needs_interaction" and r["ok"] is False
    assert r["detail"]["order"]["out_trade_no"] == "331L20260926100098716809036"
    assert r["detail"]["order"]["total_amount"] == "10.00"
    assert r["detail"]["session_source"] == "config"
    assert [s["name"] for s in r["detail"]["steps"]] == ["route_pay", "cashier_main"]
    # mock 全程只触 mclient.alipay.com，且 POST 仅 cashierMain（只读探测）
    hosts = {urllib.parse.urlsplit(u).netloc for _, u in tp.calls}
    assert hosts == {"mclient.alipay.com"}
    posts = [u for m, u in tp.calls if m == "POST"]
    assert posts and all(u.endswith("/wapcashier/api/cashierMain.json") for u in posts)


# ---------------- 安全门（发 socket 前拦截） ----------------

def test_transport_blocks_pay_submit_before_socket():
    tp = UrllibTransport(allow_post=True)
    with pytest.raises(PaySubmitBlocked):
        tp.post_json("https://mclient.alipay.com" + EP_CASHIER_PAY, {"spwd": "x"})


def test_transport_blocks_foreign_host_and_unlisted_post():
    tp = UrllibTransport(allow_post=True)
    from scripts.alipay_autopay import AutopayError
    with pytest.raises(AutopayError):    # 非 allowlist 主机
        tp.get("https://qr-wh.chinaums.com/netpay-portal/anything")
    with pytest.raises(AutopayError):    # POST 白名单之外的 mclient 端点
        tp.post_json("https://mclient.alipay.com/wapcashier/api/unifiedLogin.json", {})
    tp2 = UrllibTransport(allow_post=False)   # 未开 allow_post 时 cashierMain 也拒
    with pytest.raises(AutopayError):
        tp2.post_json("https://mclient.alipay.com/wapcashier/api/cashierMain.json", {})


# ---------------- 内嵌样本与存档一致性 ----------------

def test_embedded_sample_matches_archive():
    """--selftest 内嵌样本须与 output 存档同源（防止脚本内样本漂移）。"""
    emb = parse_order_str(ORDER_STR_SAMPLE)
    assert emb["out_trade_no"] == WIRE["biz_content"]["out_trade_no"]
    assert emb["total_amount"] == WIRE["biz_content"]["total_amount"]
    assert emb["app_id"] == WIRE["app_id"]
    assert emb["sign"] == urllib.parse.unquote_plus(WIRE["sign"])   # 存档存原始编码形态
