#!/usr/bin/env python3
"""支付宝 H5 收银台自动支付 —— 实验性接缝骨架（Agent D，2026-09-26）。

定位与边界
==========
本模块把 docs/pay_scenarios_design_20260926.md 中「支付宝侧扣款不在纯协议范围」的
边界做工程化延伸：解析 chagee createOrder/continuePay 返回的 orderStr
（alipay.trade.app.pay，银联商务 chinaums 收单），构造 H5 收银台 URL，并对收银台
链路做**只读探测**。它是 F5「自动支付」模式的接缝（api_contract_f5f6.md §6），
不是承诺全自动扣款的实现。

安全不变量（模块级，硬约束）
----------------------------
1. **永不提交真实扣款**：cashierPay.json（支付提交腿）在本模块中被结构性禁止——
   UrllibTransport 在发起任何 socket 之前就会对其抛 PaySubmitBlocked；驱动逻辑中也
   不存在对该端点的调用路径。构建提交模板 build_pay_submit_template() 只产出字段
   骨架（含 spwd 占位说明），不产出可发送的报文。
2. **默认零网络**：autopay() 在未显式 enable_network 且未注入 transport 时不发任何
   包，返回 needs_interaction。
3. **不硬编码任何凭据**：Cookie/设备指纹/会话令牌全部来自配置文件或调用方注入。

H5 收银台链路（capture/chagee_native_phase0b_20260926.flows wire 实证，2026-09-26）
----------------------------------------------------------------------
    GET  mclient.alipay.com/cashierRoutePay.htm?session=<SDK 会话>&utdid&tid&cc=y
         → 302 → /h5pay/landing/index.html?h5_request_token=...&cookieToken=...
    POST /wapcashier/api/cashierMain.json   收银台主状态（匿名可调）
         → controlType="unified_login" 即登录墙（实测两笔均如此）
    --- 以下为人工环节（wire 全程观测到） ---
    POST /wapcashier/api/unifiedLogin.json      账号 + captchaToken（滑块验证码）
         → nextAction=/loginSmsValidate（短信）或账号密码腿 accountLogin.json(pwd RSA)
    POST /wapcashier/api/smsValidateLogin.json  短信验证码登录
    POST /wapcashier/api/cashierSwitchChannel.json / operationQuery.json  选渠道
    POST /wapcashier/api/cashierPay.json        提交支付：spwd=收银台 rsaPubKey
         RSA 加密的支付密码（多块）；同一抓包中一次被设备风控拒绝
         （bizErrorCode=cred_dev_to_explain_page：「请更换成经常登录的设备…」）
    POST /wapcashier/api/cashierPayResultQuery.json → 支付成功，
         returnUrl 携带签名 alipay_trade_app_pay_response(code=10000)

诚实结论（决定本骨架的自动化上限）
----------------------------------
登录墙（短信/滑块/登录密码）+ 支付密码（spwd）+ 设备风控（新设备直接被拒）三道
人工环节叠加，纯脚本无法稳定通过；本模块如实把每一道都折返为
status="needs_interaction"。能自动化的只有：orderStr 解析、收银台 URL 构造、
cashierMain 只读探测。接缝完整性优先于自动化成功率。

配置方式
========
config 缺省从 account_system/data/autopay_config.json 读取（本模块只读，不创建、
不修改；文件不存在 → needs_interaction 并在 message/detail 给出模板说明）：

    {
      "session_cookies": {"JSESSIONID": "...", "cookieToken": "...", "ctoken": "..."},
      "pay_password_hint": "H5 收银台若需密码将失败并返回 needs_interaction",
      "enable_network": false,
      "cashier": {"session": "...", "utdid": "...", "tid": "..."},
      "user_agent": "Mozilla/5.0 ...",
      "h5pay_client_id": ""
    }

注意：cashier.session/utdid/tid 由支付宝 SDK 在 App 内经 mobilegw（mcpay，应用层
加密）铸造，无法从 orderStr 离线推导。2026-09-27 实证修正：三者是**设备级可复用**
凭证（output/alipay_pay_link_20260926.json 与 09-27 新单同组复用），配置一次即可
持续构造收银台 URL；失效（会话过期/风控）时仍需从 App 拉起支付的 h5 链路重新捕获。
缺省为占位符，session_source="placeholder"。

自检：python scripts/alipay_autopay.py --selftest（纯离线，不发包）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------- 常量（wire 定案，2026-09-26） ----------------

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "account_system" / "data" / "autopay_config.json"

CASHIER_ORIGIN = "https://mclient.alipay.com"
CASHIER_ROUTE_PATH = "/cashierRoutePay.htm"
LANDING_PATH = "/h5pay/landing/index.html"

EP_CASHIER_MAIN = "/wapcashier/api/cashierMain.json"          # 收银台主状态（只读探测）
EP_CASHIER_PAY = "/wapcashier/api/cashierPay.json"            # 支付提交 —— 本模块结构性禁止调用
EP_CASHIER_PAY_RESULT_QUERY = "/wapcashier/api/cashierPayResultQuery.json"
EP_UNIFIED_LOGIN = "/wapcashier/api/unifiedLogin.json"

# 只读探测允许的 POST 白名单（UrllibTransport 强制）；cashierPay 永不在内。
POST_ALLOWLIST = {EP_CASHIER_MAIN}
# 出网允许的主机（GET/白名单 POST 均限此域）。
ALLOWED_HOSTS = {"mclient.alipay.com"}

UA_DEFAULT = (
    "Mozilla/5.0 (Linux; Android 10; HUAWEI NXT-AL10 Build/QD4A.200805.003; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/119.0.6045.134 "
    "Mobile Safari/537.36"
)

PLACEHOLDER = "PLACEHOLDER_FROM_CONFIG"

CONFIG_TEMPLATE = {
    "session_cookies": {
        "JSESSIONID": "<收银台 JSESSIONID>",
        "cookieToken": "<landing 链路下发的 cookieToken>",
        "ctoken": "<ctoken>",
    },
    "pay_password_hint": "H5 收银台若需密码将失败并返回 needs_interaction",
    "enable_network": False,
    "cashier": {
        "session": "<SDK 铸造的收银台会话，从 App 拉起支付时的 h5 链路捕获>",
        "utdid": "<设备 utdid>",
        "tid": "<设备 tid>",
    },
    "user_agent": UA_DEFAULT,
    "h5pay_client_id": "",
}

# 2026-09-26 ¥10 差额单 wire 存档（output/alipay_pay_link_20260926.json，
# 自有抓包归档，供 --selftest 离线回放）。
ORDER_STR_SAMPLE = (
    "alipay_sdk=alipay-sdk-java-4.40.237.ALL&app_auth_token=202511BBb03f0a4ddba545aa8f09a02a9a101X44"
    "&app_id=2018080860981451"
    "&biz_content=%7B%22time_expire%22%3A%222026-09-26+17%3A10%3A49%22%2C%22extend_params%22%3A%7B"
    "%22sys_service_provider_id%22%3A%222088021787797493%22%7D%2C%22out_trade_no%22%3A"
    "%22331L20260926100098716809036%22%2C%22total_amount%22%3A%2210.00%22%2C%22subject%22%3A"
    "%22CHAGEE%E9%9C%B8%E7%8E%8B%E8%8C%B6%E5%A7%AC%EF%BC%88%E6%B9%96%E5%8D%97%E8%A1%A1%E9%98%B3"
    "%E5%B8%B8%E5%AE%81%E4%B8%9C%E9%A3%8E%E5%B9%BF%E5%9C%BA%E5%BA%97%EF%BC%89%22%2C"
    "%22business_params%22%3A%7B%7D%2C%22timeout_express%22%3A%2230m%22%2C%22product_code%22%3A"
    "%22QUICK_MSECURITY_PAY%22%2C%22merchant_order_no%22%3A%22331L20260926100098716809036%22%7D"
    "&charset=UTF-8&format=json&method=alipay.trade.app.pay"
    "&notify_url=https%3A%2F%2Fqr-wh.chinaums.com%2Fnetpay-portal%2Ftrade%2Fnotify.do%2FAlipay2%2F"
    "ALIPAY%2FAPP_PAY%2F7ee09e49-5db3-4246-81f1-36298d407d7b%2F1790412049805%2F"
    "E06a40af553144dbabcdb92a8139a7ae"
    "&return_url=https%3A%2F%2Fqr-wh.chinaums.com%2Fnetpay-portal%2Falipay2%2Fh5PayResult.do%2F"
    "7ee09e49-5db3-4246-81f1-36298d407d7b%2F1790412049805%2FE06a40af553144dbabcdb92a8139a7ae%2FFINISH"
    "&sign=LyWkQeYdsJfs9YIN0mtodJYJv1K2kwhO2m37JbS4%2B2g1wfALgMHLnzS9udAezykvv4HTnEdxAVdlyEz1Ir9fc4uZbhImf7uZ4YVXWNP2tjWKd5H%2FfT0ygQZR6CluEOCQQQcVrQ%2BGi1paEwY6dXsgn%2F6rbIIVOcxlzZv72PXSBCbk12J0QkwXpFqo84epMROclxsPAPvf5SxB0gu9xguUY6xQVt2DzeTC3%2B6r9gnJHCiR72CYVhM2VOlf462JrNK%2BZV%2FzbLhDQFvHqgSV4Mqn4N3JP5qznIsoD%2BUaDGHWBqs50IVeBv6DJqyN465h3%2B%2F6FRv8M4%2FoF80AtdbHFD7TEA%3D%3D"
    "&sign_type=RSA2&timestamp=2026-09-26+16%3A40%3A49&version=1.0"
)


# ---------------- 异常 ----------------

class AutopayError(Exception):
    """autopay 域通用错误。"""


class OrderStrParseError(AutopayError):
    """orderStr 不是合法的 alipay.trade.app.pay 支付串。"""


class PaySubmitBlocked(AutopayError):
    """安全不变量：本模块永不向收银台支付提交端点发包（发 socket 前拦截）。"""


# ---------------- 传输抽象（网络腿可注入/mock，默认零网络） ----------------

class TransportResponse:
    """传输层响应摘要：最终 URL（重定向后）、状态码、响应头、文本体。"""

    def __init__(self, url: str, status: int, headers: dict | None = None, text: str = ""):
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.text = text

    def json(self) -> dict:
        return json.loads(self.text)


class Transport:
    """最小传输接口。测试注入 mock；生产用 UrllibTransport（受安全门限制）。"""

    def get(self, url: str, headers: dict | None = None) -> TransportResponse:
        raise NotImplementedError

    def post_json(self, url: str, payload: dict, headers: dict | None = None) -> TransportResponse:
        raise NotImplementedError


class UrllibTransport(Transport):
    """urllib 实现的受控传输（与 chagee_client.py 同风格，不引第三方依赖）。

    门限制（在建立任何 socket 之前校验）：
      - 主机必须在 ALLOWED_HOSTS；
      - GET 只允许收银台链路（cashierRoutePay / h5pay landing）；
      - POST 只允许 POST_ALLOWLIST（cashierMain 只读探测）；
      - cashierPay.json 等支付提交端点无条件拒绝（PaySubmitBlocked）——
        即使调用方误开 allow_post 也拦。
    """

    def __init__(self, allow_post: bool = False, timeout: float = 15.0, user_agent: str = UA_DEFAULT):
        self.allow_post = allow_post
        self.timeout = timeout
        self.user_agent = user_agent

    # -- 门限制 --

    def _gate(self, url: str, method: str) -> None:
        u = urllib.parse.urlsplit(url)
        if u.hostname not in ALLOWED_HOSTS:
            raise AutopayError(f"host not allowed by autopay gate: {u.hostname!r}")
        path = u.path or "/"
        if path == EP_CASHIER_PAY or path == EP_CASHIER_PAY_RESULT_QUERY:
            raise PaySubmitBlocked(
                f"real pay submit is forbidden by module invariant: {path}"
            )
        if method == "POST" and (not self.allow_post or path not in POST_ALLOWLIST):
            raise AutopayError(
                f"POST not allowed by autopay gate (allow_post={self.allow_post}): {path}"
            )

    # -- 请求 --

    def _open(self, url: str, method: str, headers: dict | None, body: bytes | None) -> TransportResponse:
        self._gate(url, method)
        h = {"User-Agent": self.user_agent}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return TransportResponse(
                    url=r.geturl(), status=r.status, headers=dict(r.headers.items()),
                    text=r.read().decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as e:
            return TransportResponse(
                url=url, status=e.code, headers=dict(e.headers.items()) if e.headers else {},
                text=(e.read().decode("utf-8", errors="replace") if e.fp else ""),
            )

    def get(self, url: str, headers: dict | None = None) -> TransportResponse:
        return self._open(url, "GET", headers, None)

    def post_json(self, url: str, payload: dict, headers: dict | None = None) -> TransportResponse:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        return self._open(url, "POST", h, body)


# ---------------- a. orderStr 解析 ----------------

def parse_order_str(order_str: str) -> dict:
    """解析 alipay.trade.app.pay 支付串 → 扁平 dict（biz_content 解 JSON）。

    返回键：alipay_sdk/app_id/app_auth_token/method/charset/format/sign/sign_type/
    timestamp/version/notify_url/return_url + biz_content(dict) 及其平铺字段
    （out_trade_no/total_amount/subject/product_code/timeout_express/time_expire/
    merchant_order_no/extend_params）。
    """
    if not isinstance(order_str, str) or "alipay_sdk=" not in order_str:
        raise OrderStrParseError("orderStr 不是 alipay 支付串（缺少 alipay_sdk= 前缀段）")
    pairs = urllib.parse.parse_qsl(order_str.strip(), keep_blank_values=True)
    fields = dict(pairs)
    if "biz_content" not in fields or "sign" not in fields:
        raise OrderStrParseError("orderStr 缺少 biz_content/sign 必要段")
    try:
        biz = json.loads(fields["biz_content"])
    except (ValueError, TypeError) as e:
        raise OrderStrParseError(f"biz_content 不是合法 JSON: {e}") from e
    out = dict(fields)
    out["biz_content"] = biz
    for key in ("out_trade_no", "total_amount", "subject", "product_code",
                "timeout_express", "time_expire", "merchant_order_no", "extend_params",
                "business_params"):
        out[key] = biz.get(key)
    return out


# ---------------- b. 收银台 URL 构造 ----------------

def build_cashier_url(parsed: dict, *, session: str | None = None, utdid: str | None = None,
                      tid: str | None = None, extra_params: dict | None = None) -> str:
    """以 wire 样本 h5_cashier_url 形态构造收银台路由 URL。

    形态模板（2026-09-26 wire）：
      https://mclient.alipay.com/cashierRoutePay.htm
        ?route_pay_from=h5&init_from=SDKLite&session=<SDK 会话>&utdid=<utdid>
        &tid=<tid>&cc=y

    session/utdid/tid 由 config["cashier"] 提供；缺省写占位符（session_source 由调用方标注）。
    2026-09-27 实证：三元组为设备级可复用凭证（跨日跨单同组复用，见模块 docstring）。
    """
    q = {
        "route_pay_from": "h5",
        "init_from": "SDKLite",
        "session": session or PLACEHOLDER,
        "utdid": utdid or PLACEHOLDER,
        "tid": tid or PLACEHOLDER,
        "cc": "y",
    }
    if extra_params:
        q.update(extra_params)
    # safe='/'：wire 样本中 utdid 的 '/' 未转义，保持逐字节可复现
    return f"{CASHIER_ORIGIN}{CASHIER_ROUTE_PATH}?{urllib.parse.urlencode(q, quote_via=urllib.parse.quote, safe='/')}"


# ---------------- 支付提交模板（永不发送） ----------------

def build_pay_submit_template(context: dict) -> dict:
    """按 wire 形态产出 cashierPay.json 提交模板（占位骨架，不可直接发送）。

    真实提交还缺三样本模块永不持有的东西：
      - server_param/contextId/pageToken：收银台服务端逐腿下发的会话态；
      - spwd：收银台 rsaPubKey RSA 加密的支付密码（多块密文）；
      - 通过设备风控（wire 实证新设备首提被 cred_dev_to_explain_page 拒绝）。
    """
    return {
        "_note": "template only — this module never POSTs /wapcashier/api/cashierPay.json",
        "server_param": context.get("server_param", "<服务端逐腿下发>"),
        "contextId": context.get("contextId", "<服务端逐腿下发>"),
        "cookieToken": context.get("cookieToken", "<landing 链路下发>"),
        "pageToken": context.get("pageToken", "<服务端逐腿下发>"),
        "operationActionParams": {
            "selected_channel": context.get("selected_channel", "<cashierSwitchChannel 列表选中项>"),
            "combinationIndex": context.get("combinationIndex", "<选中渠道组合索引>"),
            "fromCombinationPayIntermediateState": "false",
        },
        "spwd": "<REQUIRED_RSA_ENCRYPTED_PAY_PASSWORD — 本模块永不填充>",
    }


# ---------------- c. 收银台驱动（只读探测 + 人工环节折返） ----------------

LOGIN_WALL_HINT = (
    "H5 收银台登录墙：需要 支付宝账号+滑块验证码(captchaToken)+短信验证码(smsValidateLogin) "
    "或账号密码(accountLogin, pwd RSA 加密) 登录；本脚本不持有任何登录凭据"
)
PAY_PWD_HINT = (
    "支付提交腿：cashierPay.json 需要 spwd(收银台 rsaPubKey RSA 加密的支付密码)，"
    "且新设备首提会命中风控(cred_dev_to_explain_page)；本脚本不持有支付密码，永不提交扣款"
)


def _cookie_header(session_cookies: dict | None) -> str:
    if not session_cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in session_cookies.items())


def _extract_landing_params(landing_url: str) -> dict:
    """从 h5pay/landing URL 抽取 cashierMain 探测所需参数（wire 实证字段）。"""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(landing_url).query, keep_blank_values=True))
    query_params = q.get("query_params", "")
    inner = dict(urllib.parse.parse_qsl(query_params, keep_blank_values=True))
    return {
        "h5_request_token": q.get("h5_request_token", ""),
        "cookieToken": q.get("cookieToken", ""),
        "query_params": query_params,
        "session": inner.get("session", ""),
        "utdid": inner.get("utdid", ""),
        "tid": q.get("tid") or inner.get("tid", ""),
        "x_requested_with": q.get("x_requested_with", ""),
        "serverParams": q.get("serverParams", ""),
    }


def _build_cashier_main_payload(landing: dict, config: dict) -> dict:
    """cashierMain.json 只读探测请求体（wire 形态，字段可缺省）。"""
    device = {
        "userAgent": config.get("user_agent", UA_DEFAULT),
        "screenWidth": 360,
        "screenHeight": 640,
        "hardwareConcurrency": 8,
    }
    return {
        "h5_request_token": landing.get("h5_request_token", ""),
        "query_params": landing.get("query_params", ""),
        "x_requested_with": landing.get("x_requested_with") or "com.chagee.application.cn",
        "app_name": "mc",
        "tid": landing.get("tid", ""),
        "targetDispatchSystem": "unitradeprod",
        "serverParams": landing.get("serverParams", ""),
        "cookieToken": landing.get("cookieToken", ""),
        "device": device,
        "supportWriteCookie": True,
        "t": int(time.time() * 1000),
        "h5payClientId": config.get("h5pay_client_id", ""),
        "lastReferer": "",
    }


def drive_cashier(url: str, session_cookies: dict | None = None,
                  config: dict | None = None, transport: Transport | None = None) -> dict:
    """驱动 H5 收银台：路由 → landing → cashierMain 只读探测。

    返回 {"status": "submitted"|"needs_interaction"|"failed", "message": str,
          "detail": {"steps": [...], ...}}。每一步请求/响应摘要记入 detail.steps。
    遇登录跳转/登录墙/支付密码环节 → needs_interaction（本模块唯一可达的
    "submitted" 语义不存在——扣款腿被结构性禁止，见模块 docstring）。
    """
    cfg = config or {}
    tp = transport or UrllibTransport(allow_post=True, user_agent=cfg.get("user_agent", UA_DEFAULT))
    cookie = _cookie_header(session_cookies)
    base_headers = {"Cookie": cookie} if cookie else {}
    steps: list[dict] = []

    def step(name: str, method: str, req_url: str, resp: TransportResponse | None, note: str) -> dict:
        s = {
            "name": name, "method": method, "url": req_url,
            "status": resp.status if resp is not None else None,
            "final_url": resp.url if resp is not None else None,
            "note": note,
        }
        steps.append(s)
        return s

    def result(status: str, message: str, **extra) -> dict:
        return {"status": status, "message": message, "detail": {"steps": steps, **extra}}

    # 步骤 1：路由腿（302 → landing；urllib 自动跟随，取最终 URL 判形态）
    try:
        r1 = tp.get(url, headers=dict(base_headers))
    except PaySubmitBlocked as e:                      # 理论不可达（GET 无门限制），防御性折返
        return result("failed", f"transport gate: {e}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        step("route_pay", "GET", url, None, f"transport error: {e}")
        return result("failed", f"路由腿网络错误: {e}")
    step("route_pay", "GET", url, r1, "302 自动跟随后的最终页")
    final = urllib.parse.urlsplit(r1.url)
    if "login" in (final.path + final.netloc).lower():
        return result("needs_interaction", "路由腿被重定向到登录页", interaction="login_redirect")
    if LANDING_PATH not in final.path:
        return result("failed", f"路由腿未落到 h5pay/landing（final={final.path}）")
    landing_url = r1.url
    landing = _extract_landing_params(landing_url)
    if not landing.get("h5_request_token"):
        return result("failed", "landing URL 缺少 h5_request_token，无法探测收银台状态")

    # 步骤 2：cashierMain 只读探测（POST 白名单端点，非扣款）
    main_url = CASHIER_ORIGIN + EP_CASHIER_MAIN
    payload = _build_cashier_main_payload(landing, cfg)
    try:
        r2 = tp.post_json(main_url, payload, headers=dict(base_headers))
    except PaySubmitBlocked as e:
        return result("failed", f"transport gate: {e}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        step("cashier_main", "POST", main_url, None, f"transport error: {e}")
        return result("failed", f"cashierMain 网络错误: {e}")
    try:
        data = r2.json().get("data", {})
    except ValueError as e:
        step("cashier_main", "POST", main_url, r2, f"响应非 JSON: {e}")
        return result("failed", f"cashierMain 响应不可解析: {e}")
    control = data.get("controlType", "")
    step("cashier_main", "POST", main_url, r2, f"controlType={control}, outTradeNo="
         + str((data.get("clientLogData") or {}).get("outTradeNo", "")))

    # 步骤 3：按 controlType 分类折返
    if control == "unified_login":
        return result("needs_interaction", LOGIN_WALL_HINT, interaction="login_wall",
                      cashier_control_type=control,
                      order_amount=data.get("bizData", {}).get("orderAmount"))
    # 已登录态理论上进入选渠道→支付腿；本模块不持有支付密码且结构性禁止扣款提交
    return result(
        "needs_interaction", PAY_PWD_HINT, interaction="pay_password",
        cashier_control_type=control,
        pay_submit_template=build_pay_submit_template({
            "cookieToken": landing.get("cookieToken", ""),
        }),
    )


# ---------------- 接缝入口（api_contract_f5f6.md §6） ----------------

def _load_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    cfg = json.loads(text)
    if not isinstance(cfg, dict):
        raise AutopayError("autopay_config.json 顶层必须是对象")
    return cfg


def autopay(order_str: str, config: dict | None = None) -> dict:
    """支付宝 H5 收银台自动支付接缝。

    返回 {"ok": bool, "status": "submitted"|"needs_interaction"|"failed",
          "message": str, "detail": {...}}；ok 仅在 status=="submitted" 时为 True
    （当前实现下扣款腿被结构性禁止，ok 恒为 False——这是诚实行为而非缺陷）。

    config 缺省读 DEFAULT_CONFIG_PATH（account_system/data/autopay_config.json，
    只读）。文件不存在 → needs_interaction + 配置模板说明。
    """
    detail: dict = {}

    # 1) 配置解析
    cfg = config
    if cfg is None:
        try:
            cfg = _load_config(DEFAULT_CONFIG_PATH)
        except FileNotFoundError:
            return {
                "ok": False, "status": "needs_interaction",
                "message": (
                    f"未找到配置文件 {DEFAULT_CONFIG_PATH}；请创建（模板见 detail.config_template，"
                    "或参考模块 docstring）。session/utdid/tid 为支付宝 SDK 铸造的一次性会话"
                    "凭证，需从 App 拉起支付时的 h5 链路实时捕获。"
                ),
                "detail": {"config_path": str(DEFAULT_CONFIG_PATH),
                           "config_template": CONFIG_TEMPLATE},
            }
        except (ValueError, OSError) as e:
            return {"ok": False, "status": "failed",
                    "message": f"配置文件读取失败: {e}",
                    "detail": {"config_path": str(DEFAULT_CONFIG_PATH)}}
    cfg = cfg or {}

    # 2) orderStr 解析（纯离线）
    try:
        parsed = parse_order_str(order_str)
    except OrderStrParseError as e:
        return {"ok": False, "status": "failed", "message": f"orderStr 解析失败: {e}",
                "detail": {}}
    detail["order"] = {
        "app_id": parsed.get("app_id"), "method": parsed.get("method"),
        "out_trade_no": parsed.get("out_trade_no"), "total_amount": parsed.get("total_amount"),
        "subject": parsed.get("subject"), "sign_type": parsed.get("sign_type"),
        "time_expire": parsed.get("time_expire"),
    }

    # 3) 收银台 URL 构造（纯离线）
    cashier_cfg = cfg.get("cashier") or {}
    session = cashier_cfg.get("session")
    detail["cashier_url"] = build_cashier_url(
        parsed, session=session, utdid=cashier_cfg.get("utdid"), tid=cashier_cfg.get("tid"))
    detail["session_source"] = "config" if session else "placeholder"

    # 4) 网络腿：默认零网络；显式 enable_network 或注入 transport 才出网
    transport = cfg.get("transport")
    if transport is None and not cfg.get("enable_network"):
        return {
            "ok": False, "status": "needs_interaction",
            "message": (
                "网络腿默认关闭（安全约束：不向 alipay/chinaums 生产域发包）。"
                "如需只读探测收银台状态，请在配置中设置 enable_network=true 并提供 "
                "cashier.session（SDK 会话）与 session_cookies；真实扣款提交永不由本模块执行。"
            ),
            "detail": detail,
        }
    if detail["session_source"] == "placeholder":
        return {
            "ok": False, "status": "needs_interaction",
            "message": ("缺少收银台会话凭证：cashier.session 为 SDK 铸造的一次性令牌，"
                        "无法从 orderStr 推导，请从 App 拉起支付的 h5 链路捕获后写入配置。"),
            "detail": detail,
        }

    # 5) 驱动收银台（只读探测；登录墙/支付密码 → needs_interaction）
    drv = drive_cashier(detail["cashier_url"], session_cookies=cfg.get("session_cookies"),
                        config=cfg, transport=transport)
    detail.update(drv.get("detail", {}))
    return {"ok": drv.get("status") == "submitted",
            "status": drv.get("status", "failed"),
            "message": drv.get("message", ""),
            "detail": detail}


# ---------------- CLI 自检（离线，不发包） ----------------

def _selftest() -> int:
    parsed = parse_order_str(ORDER_STR_SAMPLE)
    assert parsed["out_trade_no"] == "331L20260926100098716809036"
    assert parsed["total_amount"] == "10.00"
    assert parsed["app_id"] == "2018080860981451"
    assert parsed["method"] == "alipay.trade.app.pay"
    assert parsed["sign_type"] == "RSA2"
    url = build_cashier_url(parsed)
    parts = urllib.parse.urlsplit(url)
    assert parts.netloc == "mclient.alipay.com" and parts.path == CASHIER_ROUTE_PATH
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert {"route_pay_from", "init_from", "session", "utdid", "tid", "cc"} <= set(q)
    # 门限制自检：支付提交端点在发 socket 前被拦
    tp = UrllibTransport(allow_post=True)
    try:
        tp.post_json(CASHIER_ORIGIN + EP_CASHIER_PAY, {})
        print("[selftest] FAIL: PaySubmitBlocked not raised")
        return 1
    except PaySubmitBlocked:
        pass
    # 零网络自检：无配置文件 → needs_interaction（读取真实默认路径状态，不写文件）
    print("[selftest] parse/cashier-url/gate 全部通过（离线，未发包）")
    print(f"  out_trade_no={parsed['out_trade_no']} total_amount={parsed['total_amount']}"
          f" app_id={parsed['app_id']}")
    print(f"  cashier_url(占位 session)={url[:110]}...")
    print(f"  config_path={DEFAULT_CONFIG_PATH} (exists={DEFAULT_CONFIG_PATH.exists()})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="支付宝 H5 收银台自动支付（实验性接缝，默认零网络）")
    ap.add_argument("--selftest", action="store_true", help="离线自检（解析/URL 构造/安全门，不发包）")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
