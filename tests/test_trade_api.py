"""chagee_trade_api 离线单测（支付场景引擎纯函数，回放 2026-09-26 wire 存档，不发网络）。

覆盖：场景分类 / 券匹配与折扣行构造 / 支付串解析 / 提交前三向核对 /
零元响应形态 / createOrder 两分支请求模板 / 状态机与 _outcome / 零元 wire 全链券核销锚点。

wire 夹具（真实生产存档，只读回放）：
  output/trade_samples_20260926.json    settlePrice ×2（20 元券→0 元 / 10 元券→10 元）
  output/pay_lifecycle_20260926.json    continuePay 差额 8 元（内嵌支付宝 orderStr）
  output/pay_zero_capture_20260926.json 0 元 createOrder + getOrderDetail(制作中/免支付)
"""
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.chagee_trade_api import (
    ChageeTradeApi,
    ConsistencyError,
    OrderStatus,
    PayScenario,
    SettleResult,
    TradeError,
)

OUT_DIR = Path(__file__).resolve().parents[1] / "output"
TRADE_SAMPLES = json.loads((OUT_DIR / "trade_samples_20260926.json").read_text(encoding="utf-8"))
PAY_LIFECYCLE = json.loads((OUT_DIR / "pay_lifecycle_20260926.json").read_text(encoding="utf-8"))
PAY_ZERO = json.loads((OUT_DIR / "pay_zero_capture_20260926.json").read_text(encoding="utf-8"))

FUTURE_MS = 4102444800000   # 2100-01-01：远未过期（测试不依赖运行日期）
PAST_MS = 1000              # 1970-01-01：早已过期


def _resp_data(entry: dict) -> dict:
    """wire 存档条目 → 响应 data（resp_body 为 JSON 字符串）。"""
    return json.loads(entry["resp_body"])["data"]


def _req_body(entry: dict) -> dict:
    """wire 存档条目 → 请求体（req_body 为 JSON 字符串）。"""
    return json.loads(entry["req_body"])


def _settle_entries() -> list:
    return [e for e in TRADE_SAMPLES if e["path"].endswith("/settlePrice")]


def _settle_zero_entry() -> dict:
    """选中 20 元券全额抵扣 → buyerRealPrice="0" 的零元试算样本。"""
    for e in _settle_entries():
        d = _resp_data(e)
        if (d["tradeFundInfo"].get("buyerRealPrice") == "0"
                and d.get("discountList") and d["discountList"][0]["discountAmount"] == "20"):
            return e
    raise AssertionError("trade_samples 缺少零元 settlePrice 样本")


def _settle_from(data: dict) -> SettleResult:
    """settlePrice 响应 data → SettleResult（与 settle_with_cart 的组装同构）。"""
    fund = data["tradeFundInfo"]
    asset = (data.get("assetInfo") or {}).get("userCouponInfo") or {}
    return SettleResult(
        confirm_order_key=str(data["confirmOrderKey"]),
        total_trade_price=str(fund["totalTradePrice"]),
        buyer_real_price=str(fund["buyerRealPrice"]),
        available_coupons=asset.get("availableCouponList") or [],
        order_group_list=data["orderGroupList"],
        trade_fund_info=fund,
        discount_list=data["discountList"],
        raw=data,
    )


def _coupon(code: str, face: str, threshold: str = "", end: int = FUTURE_MS,
            can_discount: bool = True) -> dict:
    return {
        "couponCode": code,
        "templateName": f"茶姬{face}元代金券-T",
        "benefitText": f"{face}元",
        "thresholdTips": threshold,
        "useEndTime": end,
        "canDiscount": can_discount,
    }


def _rate_coupon(code: str, rate: str, threshold: str = "", end: int = FUTURE_MS,
                 can_discount: bool = True) -> dict:
    return {
        "couponCode": code,
        "templateName": f"茶姬单杯{rate}折券-T",
        "benefitText": f"{rate}折",
        "thresholdTips": threshold,
        "useEndTime": end,
        "canDiscount": can_discount,
    }


COUPON_20 = _coupon("C20", "20")
COUPON_10 = _coupon("C10", "10")
COUPON_20_T25 = _coupon("C25T", "20", threshold="满25元可用")
COUPON_EXPIRED = _coupon("CEXP", "20", end=PAST_MS)
COUPON_7Z = _rate_coupon("C7Z", "7")      # 全品类单杯7折券（2026-09-28 事故券型）
COUPON_78Z = _rate_coupon("C78Z", "7.8")  # 【摇优惠】单杯78折券
COUPON_5Z = _rate_coupon("C5Z", "5")


# ---------------- classify：场景判据 ----------------

def test_classify_zero_from_settle_wire():
    """wire 回放：20 元券选中抵扣 → buyerRealPrice="0" → 零元场景。"""
    data = _resp_data(_settle_zero_entry())
    assert data["discountList"][0]["discountAmount"] == "20"
    assert data["tradeFundInfo"]["buyerRealPrice"] == "0"
    assert ChageeTradeApi.classify(data["tradeFundInfo"]["buyerRealPrice"]) is PayScenario.ZERO
    assert _settle_from(data).scenario is PayScenario.ZERO


def test_classify_partial_and_decimal_semantics():
    """"8"（continuePay 差额场景）→ PARTIAL；"0.00"/空串/None 按 Decimal 语义归零。"""
    assert ChageeTradeApi.classify("8") is PayScenario.PARTIAL
    partial_settles = [e for e in _settle_entries()
                       if _resp_data(e)["tradeFundInfo"]["buyerRealPrice"] != "0"]
    assert ChageeTradeApi.classify(
        _resp_data(partial_settles[0])["tradeFundInfo"]["buyerRealPrice"]) is PayScenario.PARTIAL
    assert ChageeTradeApi.classify("0.00") is PayScenario.ZERO
    assert ChageeTradeApi.classify("") is PayScenario.ZERO
    assert ChageeTradeApi.classify(None) is PayScenario.ZERO


# ---------------- 券面额 / 门槛 ----------------

def test_coupon_face_extracts_decimal():
    """benefitText "20元" → Decimal(20)；无法解析时为 0。"""
    assert ChageeTradeApi.coupon_face(COUPON_20) == Decimal("20")
    assert ChageeTradeApi.coupon_face({"benefitText": "10元"}) == Decimal("10")
    assert ChageeTradeApi.coupon_face({"benefitText": ""}) == Decimal("0")


def test_coupon_threshold_ok():
    """thresholdTips "满20元可用"：总额达标放行、不达标拦截；空串=无门槛。"""
    entry = {"thresholdTips": "满20元可用"}
    assert ChageeTradeApi.coupon_threshold_ok(entry, Decimal("20")) is True
    assert ChageeTradeApi.coupon_threshold_ok(entry, Decimal("19.9")) is False
    assert ChageeTradeApi.coupon_threshold_ok({"thresholdTips": ""}, Decimal("1")) is True


# ---------------- pick_coupon：券匹配 ----------------

def test_pick_coupon_prefers_covering_face():
    """total=20：无门槛 20 元券覆盖总额（优先于 10 元券），预期抵扣 20。"""
    entry, ded = ChageeTradeApi.pick_coupon(
        [COUPON_20, COUPON_10, COUPON_20_T25, COUPON_EXPIRED], "20")
    assert entry["couponCode"] == "C20"
    assert ded == Decimal("20")


def test_pick_coupon_caps_deduction_at_total():
    """total=12：选 20 元券但抵扣封顶为总额 12。"""
    entry, ded = ChageeTradeApi.pick_coupon([COUPON_20, COUPON_10], "12")
    assert entry["couponCode"] == "C20"
    assert ded == Decimal("12")


def test_pick_coupon_no_coverage_takes_max_face():
    """total=30 无可覆盖券 → 取面额最大（20 元），差额 10 由 createOrder payAmount 体现。"""
    entry, ded = ChageeTradeApi.pick_coupon([COUPON_20, COUPON_10], "30")
    assert entry["couponCode"] == "C20"
    assert ded == Decimal("20")


def test_pick_coupon_excludes_threshold_expired_and_empty():
    """满 25 门槛券在 total=20 被排除；过期券被排除；无可用 → (None, 0)。"""
    entry, ded = ChageeTradeApi.pick_coupon([COUPON_20_T25], "20")
    assert entry is None and ded == Decimal("0")
    entry, ded = ChageeTradeApi.pick_coupon([COUPON_EXPIRED], "20")
    assert entry is None and ded == Decimal("0")
    entry, ded = ChageeTradeApi.pick_coupon(
        [_coupon("CDIS", "20", can_discount=False)], "20")
    assert entry is None and ded == Decimal("0")
    assert ChageeTradeApi.pick_coupon([], "20") == (None, Decimal("0"))


# ---------------- 折扣率券（"7折"）券型感知（2026-09-28 修复回归） ----------------
# 事故：coupon_face 首数字正则把 "7折" 当 ¥7 固定面额 → ΣdiscountList=7 ≠
# totalDiscountAmount=6.3/6.6/4.84，assert_settle_consistency 提交前拦截

def test_coupon_kind_classifies_rate_fixed_other():
    """"7折"/"7.8折"=rate；"20元"=fixed；"免10杯"/空=other。"""
    assert ChageeTradeApi.coupon_kind(COUPON_7Z) == "rate"
    assert ChageeTradeApi.coupon_kind(COUPON_78Z) == "rate"
    assert ChageeTradeApi.coupon_kind(COUPON_20) == "fixed"
    assert ChageeTradeApi.coupon_kind({"benefitText": "免10杯"}) == "other"
    assert ChageeTradeApi.coupon_kind({"benefitText": ""}) == "other"


def test_expected_deduction_rate_math_matches_incident():
    """事故数值精确复现：7折@21→6.30、7折@22→6.60、7.8折@22→4.84（均 2 位小数）。"""
    assert ChageeTradeApi.expected_deduction(COUPON_7Z, "21") == Decimal("6.30")
    assert ChageeTradeApi.expected_deduction(COUPON_7Z, "22") == Decimal("6.60")
    assert ChageeTradeApi.expected_deduction(COUPON_78Z, "22") == Decimal("4.84")
    # fixed 券语义不变：min(面额, 总价)；other 不计抵扣
    assert ChageeTradeApi.expected_deduction(COUPON_20, "12") == Decimal("12")
    assert ChageeTradeApi.expected_deduction(COUPON_20, "30") == Decimal("20")
    assert ChageeTradeApi.expected_deduction({"benefitText": "免10杯"}, "20") == Decimal("0")


def test_pick_coupon_rate_never_covers_total():
    """total=6 + 7折券：旧缺陷 face=7≥6 误判可覆盖（ded=6 零元）；现按折率 ded=1.80 差额单。"""
    entry, ded = ChageeTradeApi.pick_coupon([COUPON_7Z], "6")
    assert entry["couponCode"] == "C7Z"
    assert ded == Decimal("1.80")
    assert ded < Decimal("6")


def test_pick_coupon_mixed_rate_vs_fixed_by_deduction():
    """混合券池按预估抵扣统一比较：22 元下单 7折(ded 6.6) 不敌 10元券(ded 10)；
    5折(ded 11) 胜 10元券；fixed 券覆盖行为不受影响。"""
    entry, ded = ChageeTradeApi.pick_coupon([COUPON_7Z, COUPON_10], "22")
    assert (entry["couponCode"], ded) == ("C10", Decimal("10"))
    entry, ded = ChageeTradeApi.pick_coupon([COUPON_5Z, COUPON_10], "22")
    assert (entry["couponCode"], ded) == ("C5Z", Decimal("11"))
    entry, ded = ChageeTradeApi.pick_coupon([COUPON_20, COUPON_10], "20")   # fixed 覆盖优先不变
    assert (entry["couponCode"], ded) == ("C20", Decimal("20"))


def _rate_settle() -> SettleResult:
    """事故单 SettleResult：22 元 + 7折券，服务端回填 discountList/totalDiscountAmount=6.6。"""
    return SettleResult(
        confirm_order_key="K1",
        total_trade_price="22", buyer_real_price="15.4",
        available_coupons=[COUPON_7Z, COUPON_10], order_group_list=[{}],
        trade_fund_info={"totalTradePrice": "22", "buyerRealPrice": "15.4",
                         "totalDiscountAmount": "6.6"},
        discount_list=[{
            "discountId": "C7Z", "discountName": "茶姬单杯7折券-T",
            "discountSource": 1, "discountType": 1, "scopeType": 2,
            "discountAmount": "6.6", "currentSelect": None,
        }],
        raw={},
    )


def test_discount_rows_for_uses_server_backfilled_row():
    """下单行取服务端回填：discount_rows_for 返回该券行（currentSelect 强制 True），
    过三向一致性；本地旧口径（7 元）构造的行必须被断言拦截——事故的两种走向。"""
    settle = _rate_settle()
    rows = settle.discount_rows_for("C7Z")
    assert len(rows) == 1
    assert rows[0]["discountAmount"] == "6.6"
    assert rows[0]["currentSelect"] is True
    ChageeTradeApi.assert_settle_consistency(settle, rows)          # 服务端行 → 通过
    with pytest.raises(ConsistencyError):                            # 本地旧猜 7 元 → 拦截
        ChageeTradeApi.assert_settle_consistency(
            settle, [ChageeTradeApi.build_discount_row(COUPON_7Z, Decimal("7"))])
    assert settle.discount_rows_for("UNKNOWN") == []                 # 无回填 → 空，走兜底


# ---------------- build_discount_row：折扣行模板 ----------------

def test_build_discount_row_wire_template():
    """discountId==couponCode、currentSelect 传参；键集与真实 wire 折扣行一致。"""
    row = ChageeTradeApi.build_discount_row(COUPON_20, Decimal("20"))
    assert row["discountId"] == "C20"
    assert row["discountAmount"] == "20"
    assert row["currentSelect"] is True
    assert row["discountName"] == COUPON_20["templateName"]
    assert row["discountSource"] == 1 and row["discountType"] == 1
    # 键集对齐 pay_zero wire createOrder 的 discountList[0]
    assert set(row) == set(_req_body(PAY_ZERO[0])["discountList"][0])
    # 非整数面额 & currentSelect=False 透传
    row_off = ChageeTradeApi.build_discount_row(COUPON_20, Decimal("12.5"), selected=False)
    assert row_off["currentSelect"] is False
    assert row_off["discountAmount"] == "12.5"


# ---------------- parse_pay_payload：支付串解析 ----------------

def test_parse_pay_payload_from_continuepay_wire():
    """continuePay 响应 → PayLink（alipay orderStr 内嵌 biz_content 解码）。"""
    data = PAY_LIFECYCLE["continuePay"]["response"]["data"]
    link = ChageeTradeApi.parse_pay_payload(data)
    assert link.order_no == "202609260910110029049258250"
    assert link.pay_no == "CHP2026092610100109872701022"
    assert link.order_str.startswith("alipay_sdk=")
    assert link.out_trade_no == "331L20260926100099830101022"
    assert link.total_amount == "8.00"
    assert "17:27:33" in link.expire_at   # time_expire=2026-09-26 17:27:33（URL 解码后）


def test_parse_pay_payload_missing_payurl_raises():
    """缺 payUrl（零元单形态）走 parse 应抛 TradeError。"""
    data = dict(PAY_LIFECYCLE["continuePay"]["response"]["data"])
    data.pop("payUrl")
    with pytest.raises(TradeError):
        ChageeTradeApi.parse_pay_payload(data)


# ---------------- assert_zero_response：零元响应形态 ----------------

def test_assert_zero_response_shape_rules():
    """零元单 data 必须含 orderNo 且不得含 payUrl/payNo。"""
    ChageeTradeApi.assert_zero_response({"orderNo": "202609260910110023151918250"})
    with pytest.raises(ConsistencyError):
        ChageeTradeApi.assert_zero_response(
            {"orderNo": "X", "payUrl": "{\"requestJson\":{}}"})
    with pytest.raises(ConsistencyError):
        ChageeTradeApi.assert_zero_response({"orderNo": "X", "payNo": "P1"})
    with pytest.raises(ConsistencyError):
        ChageeTradeApi.assert_zero_response({"payUrl": "x"})


# ---------------- assert_settle_consistency：三向核对 ----------------

def test_assert_settle_consistency_wire_pass():
    """真实 settlePrice 响应 + 服务端回填的 discountList 行 → 核对通过。"""
    settle = _settle_from(_resp_data(_settle_zero_entry()))
    ChageeTradeApi.assert_settle_consistency(settle, settle.discount_list)
    assert settle.confirm_order_key == "202609260834270010085848250"


def test_assert_settle_consistency_tamper_raises():
    """篡改 buyer_real_price → 不一致抛 ConsistencyError；ΣdiscountAmount 不符同样抛。"""
    settle = _settle_from(_resp_data(_settle_zero_entry()))
    with pytest.raises(ConsistencyError):
        ChageeTradeApi.assert_settle_consistency(
            replace(settle, buyer_real_price="5"), settle.discount_list)
    bad_rows = [dict(settle.discount_list[0], discountAmount="10")]   # Σ10 != total 20
    with pytest.raises(ConsistencyError):
        ChageeTradeApi.assert_settle_consistency(settle, bad_rows)


# ---------------- build_create_body：createOrder 请求模板 ----------------

def test_build_create_body_zero_branch():
    """零元分支：paymentInfo 全空 + payAmount"0"；根体五键齐全；券行回传。"""
    settle = _settle_from(_resp_data(_settle_zero_entry()))
    coupon = next(c for c in settle.available_coupons
                  if c["couponCode"] == "1309482592713252864")
    rows = [ChageeTradeApi.build_discount_row(coupon, Decimal("20"))]
    body = ChageeTradeApi.build_create_body(settle, "YUUjd1xuMwOdN/NgckaJ6Q==",
                                            discount_rows=rows)
    assert body["orderInfo"]["paymentInfo"] == {
        "payerId": None, "payType": None, "channelCode": None,
        "currencyType": 156, "payAmount": "0"}
    assert set(body) == {"tcode", "orderInfo", "orderGroupList",
                         "discountList", "confirmOrderKey"}
    assert body["tcode"] == "CHAGEE"
    assert body["orderInfo"]["orderBizInfo"]["businessType"] == 1
    assert body["confirmOrderKey"] == "202609260834270010085848250"
    assert body["discountList"][0]["discountId"] == "1309482592713252864"
    assert body["orderGroupList"][0]["discountList"] == rows
    assert body["orderInfo"]["userInfo"]["mobile"] == "YUUjd1xuMwOdN/NgckaJ6Q=="


def test_build_create_body_zero_wire_cross_check():
    """wire 交叉验证：0 元 createOrder 实发请求的 paymentInfo/payAmount 与模板一致。"""
    req = _req_body(PAY_ZERO[0])
    pi = req["orderInfo"]["paymentInfo"]
    assert pi["payAmount"] == "0"
    assert pi["payType"] is None and pi["channelCode"] is None
    assert req["orderInfo"]["orderBizInfo"]["businessType"] == 1
    assert set(req) == {"tcode", "orderInfo", "orderGroupList",
                        "discountList", "confirmOrderKey"}
    assert req["discountList"][0]["discountId"] == "1309482592713252864"


def test_build_create_body_partial_branch():
    """差额分支：payType=60/channelCode=UnionPay/payAmount=差额（8 元）。"""
    partial = SettleResult(
        confirm_order_key="KEY-8", total_trade_price="28", buyer_real_price="8",
        available_coupons=[],
        order_group_list=[{"goodsList": [], "tradeFundInfo": {"buyerRealPrice": "8"}}],
        trade_fund_info={"totalDiscountAmount": "20"}, discount_list=[], raw={})
    rows = [{"deductionType": None, "discountAmount": "20", "discountId": "C20",
             "currentSelect": True, "discountName": "x", "discountSource": 1,
             "discountType": 1, "ruleId": None, "scopeType": None}]
    assert partial.scenario is PayScenario.PARTIAL
    body = ChageeTradeApi.build_create_body(partial, "CIPHER", discount_rows=rows)
    pi = body["orderInfo"]["paymentInfo"]
    assert pi["payType"] == 60
    assert pi["channelCode"] == "UnionPay"
    assert pi["payAmount"] == "8"
    assert pi["currencyType"] == 156


# ---------------- 状态机 / _outcome ----------------

def test_order_status_enum_values():
    """OrderStatus 状态机取值：1 待支付 / 3 制作中 / 6 已完成 / 7 已取消。"""
    assert OrderStatus.PENDING.value == 1
    assert OrderStatus.MAKING.value == 3
    assert OrderStatus.DONE.value == 6
    assert OrderStatus.CANCELLED.value == 7


def test_outcome_from_zero_order_detail():
    """getOrderDetail(wire) → _outcome：制作中/免支付/取餐码/券核销。"""
    detail = _resp_data(PAY_ZERO[1])
    oc = ChageeTradeApi._outcome(detail)
    assert oc.status == OrderStatus.MAKING.value == 3
    assert oc.status_text == "制作中"
    assert oc.pay_amount == "0"
    assert oc.pay_type_text == "免支付"
    assert oc.pickup_no == "TA0001"
    assert oc.order_no == "202609260910110023151918250"
    assert oc.promotions[0]["promotionId"] == "1309482592713252864"


# ---------------- 零元 wire 全链一致性 ----------------

def test_zero_wire_chain_coupon_anchor():
    """0 元全链：createOrder 响应仅 orderNo；订单详情券核销 promotionId == 请求 discountId。"""
    create_entry, detail_entry = PAY_ZERO[0], PAY_ZERO[1]
    resp_data = _resp_data(create_entry)
    ChageeTradeApi.assert_zero_response(resp_data)   # 无 payUrl/payNo，仅 orderNo
    detail = _resp_data(detail_entry)
    assert detail["orderNo"] == resp_data["orderNo"]
    assert detail["orderStatus"] == 3
    # 券标识三处统一锚点：discountId(请求) == promotionId(成单)
    assert (detail["orderPromotions"][0]["promotionId"]
            == _req_body(create_entry)["discountList"][0]["discountId"]
            == "1309482592713252864")


# ---------------- waiting_info：F6 取餐等待（wire 回放，2026-09-26 新增） ----------------

class _RecordingClient:
    """离线假客户端：记录请求，回放固定响应。"""

    def __init__(self, resp):
        self._resp = resp
        self.captured = None
        self.calls = []          # 全部 POST 记录（按序）

    def post(self, path, body, **kw):
        self.captured = (path, dict(body))
        self.calls.append(self.captured)
        return self._resp


def test_waiting_info_wire_shape():
    """getWaitingInfo 请求体与 wire 逐字段一致（storeNo/orderNo/uniquePosOrderNo，无 userId）。"""
    resp = PAY_LIFECYCLE["getWaitingInfo"]
    fake = _RecordingClient(resp)
    api = ChageeTradeApi(fake)
    out = api.waiting_info("CN11372", "202609260910110029049258250", "D00296327163991478272")
    path, body = fake.captured
    assert path.endswith("/getWaitingInfo")
    assert body == {"storeNo": "CN11372",
                    "orderNo": "202609260910110029049258250",
                    "uniquePosOrderNo": "D00296327163991478272"}   # wire 原样，无 userId
    assert out == {"waitingCups": 0, "waitingTime": 300, "queueLimit": 61}


# ---------------- generate_current_good_id：F5 加购指纹（2026-09-26 生产 99997 修复） ----------------

def test_generate_current_good_id_rules():
    """非空 32hex、按配置稳定、规格/属性变化即变化、多规格排序无关性。"""
    g = ChageeTradeApi.generate_current_good_id
    specs = [{"specId": "1", "specOptionId": "111"}, {"specId": "2", "specOptionId": "222"}]
    attrs = [{"attributeId": "3", "attributeOptionId": "333"}]
    a = g("625339451983278080", "653632618000097282", specs, attrs)
    b = g("625339451983278080", "653632618000097282", list(reversed(specs)), attrs)  # 排序无关
    assert a == b and len(a) == 32 and int(a, 16) >= 0        # 稳定 + 32hex
    assert g("625339451983278080", "653632618000097282", specs, []) != a   # 属性参与指纹
    assert g("625339451983278080", "999", specs, attrs) != a              # sku 参与指纹
    bare = g("625339451983278080", "653632618000097282")                  # 无规格属性也可生成
    assert len(bare) == 32 and bare != a


def test_cart_add_fills_current_good_id():
    """cart_add 在 target 未带 currentGoodId 时自动生成（99997 根因修复）。"""
    wire = _req_body(next(e for e in TRADE_SAMPLES if e["path"].endswith("/shoppingCart/change")))
    row = wire["skuList"][0]
    fake = _RecordingClient({"errcode": "0", "data": {"addErrorFlag": False}})
    fake.whoami = lambda: {"errcode": "0", "data": {"customerId": "1190018250"}}
    api = ChageeTradeApi(fake)
    target = {"storeNo": wire["storeNo"], "spuId": row["spuId"], "skuId": row["skuId"],
              "quantity": 1, "specList": row["specList"], "attributeList": row["attributeList"]}
    try:
        api.cart_add(target)
    except Exception:
        pass   # 购物车 get 未回放完整，只关心 change 请求体
    change = next((c for c in fake.calls if c[0].endswith("/shoppingCart/change")), None)
    assert change, "未发出 shoppingCart/change 请求"
    _, body = change
    sent = body["skuList"][0]["currentGoodId"]
    assert sent and len(sent) == 32 and sent != wire["skuList"][0]["currentGoodId"]  # 非空即可（服务端回显式关联键）


# ---------------- App 立即购买路径：calculatePrice + settle 直发（2026-09-26 抓包回放） ----------------

CART_CAPTURE = json.loads((OUT_DIR / "cart_capture_settle_20260926.json").read_text(encoding="utf-8"))
_CALC = next(e for e in CART_CAPTURE if "calculatePrice" in e["path"])
_SETTLE_APP = next(e for e in CART_CAPTURE if "settlePrice" in e["path"])


def test_calculate_price_wire_shape():
    """calculate_price 请求体与 App wire 逐字段一致（skuInfo 数值型原价/规格属性仅 ID）。"""
    fake = _RecordingClient(_CALC["resp"])
    api = ChageeTradeApi(fake)
    target = {
        "storeNo": _CALC["req"]["storeNo"],
        "spuId": _CALC["req"]["skuInfo"]["spuId"], "spuType": _CALC["req"]["skuInfo"]["spuType"],
        "skuId": _CALC["req"]["skuInfo"]["skuId"], "quantity": _CALC["req"]["skuInfo"]["num"],
        "salePrice": _CALC["req"]["skuInfo"]["salePrice"],
        "specList": [{"specId": "653599312273510400", "specOptionId": "653599312273510402"}],
        "attributeList": [{"attributeId": "745317722624679942", "attributeOptionId": "745317722624679945"},
                          {"attributeId": "623882672850116609", "attributeOptionId": "623882672850116614"}],
    }
    fake.whoami = lambda: {"errcode": "0", "data": {"customerId": _CALC["req"]["userId"]}}
    out = api.calculate_price(target)
    path, body = fake.calls[0]
    assert path.endswith("/goods/sku/calculatePrice")
    assert body == _CALC["req"]                       # 与 App wire 完全一致
    assert out["totalGoodsItemPrice"] == "15.00"      # 折后单价解析正确


def test_settle_direct_row_wire_shape():
    """settle_direct 行构造与 App wire 一致：真实价格映射/currentGoodId=null/extraList 行/营养。"""
    fake = _RecordingClient({"errcode": "0", "data": {"confirmOrderKey": "K", "tradeFundInfo": {
        "totalTradePrice": "15", "buyerRealPrice": "15"}, "orderGroupList": []}})
    fake.whoami = lambda: {"errcode": "0", "data": {"customerId": _SETTLE_APP["req"]["settleBizInfo"]["userId"]}}
    api = ChageeTradeApi(fake)
    wire_row = _SETTLE_APP["req"]["goodsList"][0]
    target = {
        "storeNo": _SETTLE_APP["req"]["settleBizInfo"]["storeNo"],
        "spuId": wire_row["spuId"], "skuId": wire_row["skuId"], "skuName": wire_row["skuName"],
        "quantity": wire_row["buyNum"], "salePrice": 20.0,
        "specList": wire_row["specList"], "attributeList": wire_row["attributeList"],
        "imageUrl": wire_row["skuImage"], "nutritionInfo": wire_row["nutritionInfo"],
    }
    price = _CALC["resp"]["data"]
    extra_opt = {"spuId": "1246792400057700352", "skuId": "1246792400066088961", "name": "无气泡",
                 "extraId": "1271779784066506753", "salePrice": "0.00"}
    settle = api.settle_direct(target, price, extra_entries=[api.build_extra_entry(extra_opt)])
    _, body = fake.calls[0]
    row = body["goodsList"][0]
    for k in ("salePrice", "marketPrice", "totalItemAmount", "totalItemDiscountedAmount",
              "buyNum", "skuName", "skuImage", "spuType", "currentGoodId",
              "specList", "attributeList", "nutritionInfo"):
        assert row[k] == wire_row[k], f"{k}: {row[k]!r} != wire {wire_row[k]!r}"
    assert row["extraList"][0]["spuId"] == wire_row["extraList"][0]["spuId"]
    assert row["extraList"][0]["extraId"] == wire_row["extraList"][0]["extraId"]
    assert row["extraList"][0]["spuType"] == "extra"
    assert settle.total_trade_price == "15"
