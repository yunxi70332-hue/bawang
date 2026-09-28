#!/usr/bin/env python3
"""ChageeTradeApi — 支付场景引擎（Phase 5 核心，2026-09-26 三场景 wire 定案）。

三种支付场景（全部 2026-09-26 生产 wire 实证）：
  零元支付  settlePrice.buyerRealPrice == "0"
            → createOrder paymentInfo{payType:null, channelCode:null, payAmount:"0"}
            → 响应 data 仅 {orderNo}，无支付腿，下一跳即 s3 制作中
  差额支付  buyerRealPrice > 0
            → paymentInfo{payType:60, channelCode:"UnionPay", payAmount:<差额>}
            → 响应 data {orderNo, payUrl, payNo}，payUrl 内嵌支付宝 orderStr
  待支付    差额单 createOrder 后未付款的中间态：orderStatus=1，
            paymentExpiryTime(下单+10min)/paymentExpiryType="autoCancel" 仅此态出现；
            续付 = continuePay{tcode,userId,orderNo,channelCode,payType} 重铸支付串

关键锚点：
  - 券标识三处统一：券列表 couponCode == 折扣行 discountId == 订单 orderPromotions.promotionId
  - 下单可用券入口 = settlePrice 响应 assetInfo.userCouponInfo.availableCouponList
    （order-coupon-list 生产 404 未部署）
  - createOrder 回传 settlePrice 的 confirmOrderKey 与 orderGroupList，选券用 currentSelect:true
  - 金额一律字符串；mobile 为 AES 密文（从 whoami 的 mobileEncrypt 直取，免本地加密）

依赖：chagee_client.ChageeClient（请求管线/会话/异常分级）。
生产写操作（cart_add/create_order/cancel）为外向动作，调用方需自行确认门控。
"""

import json
import re
import time
import urllib.parse
import uuid as uuidlib
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

HERE = __file__

TRADE = "/chagee-biz-trade-web/trade-web/order"
NAV = "/chagee-navigation-web/api/navigation"

EP_SETTLE = f"{TRADE}/settlePrice"
EP_CREATE = f"{TRADE}/createOrder"
EP_CONTINUE_PAY = f"{TRADE}/continuePay"
EP_CANCEL = f"{TRADE}/cancelOrder"          # 静态已知，无 wire 样本（待实测）
EP_COMMIT_PAY = f"{TRADE}/commitPay"        # 支付宝渠道未观测到调用，保留常量备查
EP_ORDER_LIST = f"{TRADE}/getOrderList"
EP_ORDER_DETAIL = f"{TRADE}/getOrderDetail"
EP_ORDER_STATUS = f"{TRADE}/getOrderStatus"
EP_WAITING = f"{TRADE}/getWaitingInfo"
EP_CART_CHANGE = f"{NAV}/shoppingCart/change"
EP_CALC_PRICE = f"{NAV}/goods/sku/calculatePrice"   # App 选规格后服务端算价（2026-09-26 抓包定案）
EP_CART_GET = f"{NAV}/shoppingCart/get"


# ---------------- 异常 ----------------

class TradeError(Exception):
    """trade 域业务错误（构造/流程不满足前置条件）。"""


class ConsistencyError(TradeError):
    """一致性断言失败：提交前金额/抵扣核对或成单后核销核对不通过。"""


class OrderHangError(TradeError):
    """createOrder 结果不确定（超时/异常），订单可能已创建——必须先查单再决定重试。"""


# ---------------- 枚举与数据类 ----------------

class OrderStatus(Enum):
    PENDING = 1     # 待支付（paymentExpiryTime/autoCancel 仅此态下发）
    MAKING = 3      # 制作中（支付成功/零元直通；pickupNo 同步下发）
    DONE = 6        # 已完成（取餐码 T0xxx）
    CANCELLED = 7   # 已取消（10 分钟 autoCancel / 手动）——终态不可救单


class PayScenario(Enum):
    ZERO = "zero"       # 券面额 >= 订单金额，实付 0 元，免支付腿
    PARTIAL = "partial" # 券面额 < 订单金额，差额走支付宝（payType=60）


@dataclass
class SettleResult:
    confirm_order_key: str
    total_trade_price: str        # tradeFundInfo.totalTradePrice
    buyer_real_price: str         # tradeFundInfo.buyerRealPrice（场景判据）
    available_coupons: list       # assetInfo.userCouponInfo.availableCouponList
    order_group_list: list        # 回传 createOrder 的订单组
    trade_fund_info: dict
    discount_list: list = field(default_factory=list)   # 服务端已回填金额的折扣行
    raw: dict = field(default_factory=dict)

    @property
    def scenario(self) -> PayScenario:
        return ChageeTradeApi.classify(self.buyer_real_price)

    def discount_rows_for(self, coupon_code: str) -> list:
        """从服务端回填的 discountList 中取指定券的抵扣行（下单行的唯一权威来源：
        discountAmount 是茶姬按券型算好的真实抵扣，折扣率券不再依赖本地换算）。
        返回行的拷贝并强制 currentSelect=True；无匹配返回 []。"""
        code = str(coupon_code or "")
        rows = [dict(r) for r in (self.discount_list or [])
                if str(r.get("discountId")) == code]
        for r in rows:
            r["currentSelect"] = True
        return rows


@dataclass
class PayLink:
    order_no: str
    pay_no: str
    order_str: str            # 支付宝 alipay.trade.app.pay 签名串
    out_trade_no: str         # 商户流水（每次 continuePay 重铸均变化）
    total_amount: str         # 支付宝侧金额（"10.00"）
    expire_at: str            # 支付宝侧 time_expire（茶姬侧 10 分钟窗口更短，取 min）


@dataclass
class OrderOutcome:
    order_no: str
    status: int
    status_text: str
    pay_amount: str
    pay_type_text: str
    pickup_no: str
    promotions: list


# ---------------- API ----------------

class ChageeTradeApi:
    def __init__(self, client):
        self.c = client

    # ---------- 静态纯函数（离线可测） ----------

    @staticmethod
    def classify(buyer_real_price: str) -> PayScenario:
        """场景判据：settlePrice 的 buyerRealPrice（字符串金额，"0"/"0.00" 均为零元）。"""
        if Decimal(str(buyer_real_price or "0")) == 0:
            return PayScenario.ZERO
        return PayScenario.PARTIAL

    # benefitText 券型文案：固定面额 "20元"、折扣率 "7折"/"7.8折"（2026-09-28 折扣率券误当
    # ¥7 固定面额事故后引入，docs/rate_coupon_fix_20260928.md）
    RE_RATE = re.compile(r"(\d+(?:\.\d+)?)折")
    RE_FACE = re.compile(r"(\d+(?:\.\d+)?)元")

    @classmethod
    def coupon_kind(cls, entry: dict) -> str:
        """benefitText 券型：rate=折扣率券（"7折"，抵扣=总价×(10−n)/10）；
        fixed=固定面额（"20元"）；other=不可解析（免次卡/兑换券等，本地不计抵扣）。"""
        text = str(entry.get("benefitText") or "")
        if cls.RE_RATE.search(text):
            return "rate"
        if cls.RE_FACE.search(text):
            return "fixed"
        return "other"

    @classmethod
    def expected_deduction(cls, entry: dict, total_trade_price) -> Decimal:
        """按券型估算抵扣额：rate=总价×(10−n)/10（2位小数）；fixed=min(面额,总价)；
        other=0。仅用于本地预估与 settle 请求行/兜底，成单金额以服务端回填为准。"""
        total = Decimal(str(total_trade_price))
        text = str(entry.get("benefitText") or "")
        kind = cls.coupon_kind(entry)
        if kind == "rate":
            n = Decimal(cls.RE_RATE.search(text).group(1))
            ded = (total * (Decimal(10) - n) / Decimal(10)).quantize(Decimal("0.01"))
            return min(ded, total)
        if kind == "fixed":
            face = Decimal(cls.RE_FACE.search(text).group(1))
            return min(face, total)
        return Decimal(0)

    @staticmethod
    def coupon_face(entry: dict) -> Decimal:
        """benefitText "20元" → Decimal(20)。仅对 fixed 券有元语义；
        rate 券返回的是折扣率数字（非面额），消费方应改用 expected_deduction。"""
        m = re.search(r"(\d+(?:\.\d+)?)", str(entry.get("benefitText", "")))
        return Decimal(m.group(1)) if m else Decimal(0)

    @staticmethod
    def coupon_threshold_ok(entry: dict, total: Decimal) -> bool:
        """thresholdTips 形如 "满20元可用"；空串=无门槛。"""
        tips = str(entry.get("thresholdTips") or "").strip()
        if not tips:
            return True
        m = re.search(r"(\d+(?:\.\d+)?)", tips)
        return bool(m) and Decimal(m.group(1)) <= total

    @classmethod
    def pick_coupon(cls, available: list, total_trade_price: str):
        """券匹配：可用(canDiscount/有效期内/门槛满足) 中——
        优先能全额抵扣且面额最小的券（零元场景），否则预估抵扣最大（差额最小）。
        折扣率券（"7折"）永不计入全额抵扣集合（抵扣恒 = 总价×折扣比例 < 总价）。
        返回 (券条目, 预期抵扣)；无可用券返回 (None, Decimal(0))。"""
        total = Decimal(str(total_trade_price))
        now_ms = int(time.time() * 1000)
        usable = [
            e for e in available or []
            if e.get("canDiscount") is not False
            and (not e.get("useEndTime") or int(e["useEndTime"]) > now_ms)
            and cls.coupon_threshold_ok(e, total)
        ]
        if not usable:
            return None, Decimal(0)
        # 覆盖判断/排序按券型感知的预估抵扣：fixed 券行为与旧版面额比较完全一致，
        # rate 券不再被"首数字当面额"误判成可覆盖/¥N 固定抵扣
        covering = [e for e in usable
                    if cls.coupon_kind(e) == "fixed"
                    and cls.expected_deduction(e, total) >= total]
        if covering:
            best = min(covering, key=cls.coupon_face)
            return best, total
        best = max(usable, key=lambda e: (cls.expected_deduction(e, total), cls.coupon_face(e)))
        return best, cls.expected_deduction(best, total)

    @staticmethod
    def build_discount_row(coupon_entry: dict, deduction: Decimal, selected: bool = True) -> dict:
        """构造 settlePrice/createOrder 的折扣行（wire 模板：discountId=券 couponCode）。"""
        return {
            "deductionType": None,
            "discountAmount": str(int(deduction)) if deduction == deduction.to_integral_value() else str(deduction),
            "discountId": coupon_entry["couponCode"],
            "currentSelect": selected,
            "discountName": coupon_entry.get("templateName", ""),
            "discountSource": 1,
            "discountType": 1,
            "ruleId": None,
            "scopeType": None,
        }

    @staticmethod
    def parse_pay_payload(data: dict) -> PayLink:
        """createOrder/continuePay 响应 data → PayLink（payUrl 为内嵌 JSON 字符串）。"""
        pay_url = data.get("payUrl")
        if not pay_url:
            raise TradeError(f"响应无 payUrl（零元单应走直通分支）: data keys={sorted(data)}")
        order_str = json.loads(pay_url)["requestJson"]["orderStr"]
        kv = dict(p.split("=", 1) for p in order_str.split("&") if "=" in p)
        biz = json.loads(urllib.parse.unquote(kv["biz_content"]))
        return PayLink(
            order_no=str(data["orderNo"]),
            pay_no=str(data.get("payNo", "")),
            order_str=order_str,
            out_trade_no=biz["out_trade_no"],
            total_amount=biz["total_amount"],
            expire_at=biz.get("time_expire", ""),
        )

    # ---------- 一致性断言（离线可测） ----------

    @staticmethod
    def assert_settle_consistency(settle: SettleResult, discount_rows: list):
        """提交前三向核对：buyerRealPrice 判据一致 + ΣdiscountAmount == totalDiscountAmount。"""
        brp = Decimal(settle.buyer_real_price)
        group_fund = (settle.order_group_list or [{}])[0].get("tradeFundInfo") or {}
        gbrp = Decimal(str(group_fund.get("buyerRealPrice", settle.buyer_real_price)))
        if brp != gbrp:
            raise ConsistencyError(
                f"buyerRealPrice 不一致: settle={brp} orderGroup={gbrp}")
        if discount_rows:
            total_disc = Decimal(str(settle.trade_fund_info.get("totalDiscountAmount") or "0"))
            sum_rows = sum(Decimal(str(r.get("discountAmount") or 0)) for r in discount_rows)
            if sum_rows != total_disc:
                raise ConsistencyError(
                    f"抵扣合计不符: ΣdiscountList={sum_rows} totalDiscountAmount={total_disc}")

    @staticmethod
    def assert_zero_response(data: dict):
        """零元单响应必须仅含 orderNo（无 payUrl/payNo）——wire 定案的免支付形态。"""
        if "payUrl" in data or "payNo" in data:
            raise ConsistencyError(
                f"零元单响应不应含支付字段: data keys={sorted(data)}")
        if "orderNo" not in data:
            raise ConsistencyError(f"createOrder 响应缺 orderNo: {data}")

    # ---------- 请求体构造（离线可测，wire 模板） ----------

    @classmethod
    def build_create_body(cls, settle: SettleResult, mobile_cipher: str,
                          discount_rows: list = None, pay_type: int = 60) -> dict:
        """构造 createOrder 请求（2026-09-26 两分支 wire 模板：差额单 + 零元单）。

        mobile_cipher：AES 密文手机号（生产从 whoami 的 mobileEncrypt 直取）。
        discount_rows：选中的折扣行（currentSelect:true）；None/[] = 不用券。
        """
        rows = discount_rows or []
        cls.assert_settle_consistency(settle, rows)
        scenario = settle.scenario
        pay_amount = str(settle.buyer_real_price)
        if scenario is PayScenario.ZERO:
            payment_info = {"payerId": None, "payType": None, "channelCode": None,
                            "currencyType": 156, "payAmount": pay_amount}
        else:
            payment_info = {"payerId": None, "payType": pay_type, "channelCode": "UnionPay",
                            "currencyType": 156, "payAmount": pay_amount}
        group = json.loads(json.dumps(settle.order_group_list))  # 深拷贝回传
        group[0]["discountList"] = rows
        for goods in group[0].get("goodsList", []):
            goods["discountList"] = rows
        return {
            "tcode": "CHAGEE",
            "orderInfo": {
                "buyerRemark": None,
                "orderBizInfo": {"businessType": 1, "orderType": 0},   # wire 定案（非静态注释的 2）
                "storeInfo": {"storeNo": None, "storeName": None},     # 由 _fill_store_info 注入
                "userInfo": {"userId": None, "userType": 3,
                             "mobile": mobile_cipher, "areaCode": "86"},
                "paymentInfo": payment_info,
                "deliveryInfo": {"deliveryType": 1, "packageFeeSelected": None},
            },
            "orderGroupList": group,
            "discountList": rows,
            "confirmOrderKey": settle.confirm_order_key,
        }

    def fill_identity(self, body: dict, store_no: str, store_name: str) -> dict:
        """注入运行时身份（userId/payerId/storeInfo）——与 App 组装点一致。"""
        user_id = self._user_id()
        oi = body["orderInfo"]
        oi["userInfo"]["userId"] = user_id
        oi["paymentInfo"]["payerId"] = user_id
        oi["storeInfo"] = {"storeNo": store_no, "storeName": store_name}
        for goods in body["orderGroupList"][0].get("goodsList", []):
            pass  # goodsList 无 userId 字段（wire 样本）
        return body

    # ---------- 网络方法（生产外向动作，调用方门控） ----------

    def _user_id(self) -> str:
        d = self.c.whoami().get("data") or {}
        uid = d.get("customerId")
        if not uid:
            raise TradeError("whoami 无 customerId，无法下单")
        return str(uid)

    def mobile_cipher(self) -> str:
        """服务端已加密的手机号密文（userInfo/query 的 mobileEncrypt，免本地 AES）。"""
        d = self.c.whoami().get("data") or {}
        cipher = d.get("mobileEncrypt")
        if not cipher:
            raise TradeError("whoami 无 mobileEncrypt")
        return cipher

    # ---------------- 客户端指纹：currentGoodId ----------------

    @staticmethod
    def generate_current_good_id(spu_id, sku_id, spec_list=None, attribute_list=None) -> str:
        """「当前商品ID」（App: ShoppingCartBizManager::generateCurrentGoodId @0x898ec4 形态复刻）。

        wire 实证（2026-09-26）：32 位十六进制 MD5；cart/get 原样回显 → 服务端语义为
        「当前查看商品配置」的客户端关联键（invalidCurrentGoodIds 比对用），非空且按配置
        稳定即可；为空时服务端拒绝 [99997] 当前商品ID不能为空（生产实测）。
        公式：md5("{skuId}_{spuId}" + 排序后逐项 "_{specOptionId}" + "_{attributeOptionId}")。
        """
        import hashlib
        parts = [f"{sku_id}_{spu_id}"]
        for s in sorted(spec_list or [], key=lambda x: str(x.get("specOptionId") or "")):
            if s.get("specOptionId"):
                parts.append(str(s["specOptionId"]))
        for a in sorted(attribute_list or [], key=lambda x: str(x.get("attributeOptionId") or "")):
            if a.get("attributeOptionId"):
                parts.append(str(a["attributeOptionId"]))
        return hashlib.md5("_".join(parts).encode("utf-8")).hexdigest()

    def cart_clear(self, store_no: str) -> int:
        """清空门店购物车：逐行以 operationType=0 回传删除（App removeFromCart 语义，
        2026-09-26 生产验证 13→12；批量回传部分行缺 spuType 会触发 [99997] 商品类型不能为空）。
        注：空 skuList 的 change 是 no-op 而非清空；clearAll=true 触发 [8101000200010] 购物车清空非法。"""
        cart = self.c.post(EP_CART_GET, {
            "userId": self._user_id(), "storeNo": store_no,
            "saleChannel": 2, "saleType": 1, "storeBusinessType": 1,
            "tradeBusinessType": 1, "tradeOrderType": 0, "tradeChannel": "09",
            "shoppingCartType": "DirectShopping", "storeChannelCode": "android",
        }).get("data") or {}
        uid = self._user_id()
        removed = 0
        for r in (cart.get("skuList") or []):
            row = dict(r)
            row["operationType"] = 0        # 0=删除（1=新增；2/3=更新变体）
            row["sequence"] = row.get("sequence") or 0
            row["belongUserId"] = uid
            row.setdefault("spuType", "stand")
            self.c.post(EP_CART_CHANGE, {
                "skuList": [row], "storeBusinessType": 1, "shoppingCartType": "DirectShopping",
                "storeChannelCode": "android", "storeNo": store_no,
                "userId": uid, "clearAll": False,
            })
            removed += 1
        return removed

    def cart_add(self, target: dict, clear_all: bool = False) -> dict:
        """shoppingCart/change 加购（target=run_features OrderTarget 同构）→ 校验失效列表。

        clear_all=True 时先以 wire 语义清空购物车（skuList=[]）再写入本行——settle 草稿
        的单商品语义：防止重试/换品在服务端堆积残留行（堆积会卡死失效列表校验）。
        """
        body = {
            "skuList": [{
                "attributeList": target.get("attributeList", []),
                "belongUserId": self._user_id(),
                "buyerRealPrice": None, "clientUniqKey": "-", "comboCardImageUrl": None,
                "currentInventory": None,
                "goodsLimit": {"minOrderQuantity": None,
                               "purchaseLimit": {"limitQuantity": None, "purchaseLimitType": None},
                               "salePeriod": None},
                "groupId": None, "groupName": None,
                "imageUrl": target.get("imageUrl", ""),
                "num": int(target.get("quantity", 1)),
                "saleOut": None, "selected": True,
                "skuId": target["skuId"], "skuName": target.get("skuName", target.get("spuName", "")),
                "specList": target.get("specList", []),
                "spuId": target["spuId"], "spuType": target.get("spuType", "stand"),
                "subSkuList": None, "tagImageUrl": None, "extraList": None,
                "totalTradePrice": "0", "uniqKey": None, "unitBuyerPrice": "0", "unitTradePrice": None,
                "currentGoodId": target.get("currentGoodId") or self.generate_current_good_id(
                    target["spuId"], target["skuId"],
                    target.get("specList"), target.get("attributeList")),
                "operationType": 1,
                "cartExtraLimitDTOList": None, "currentNutritionInfo": None,
                "extraGroupLimitList": None, "extraPurchaseLimit": None, "sequence": 0,
            }],
            "storeBusinessType": 1, "shoppingCartType": "DirectShopping",
            "storeChannelCode": "android",
            "storeNo": target["storeNo"],
            "userId": self._user_id(),
            "clearAll": False,
        }
        if clear_all:
            self.cart_clear(target["storeNo"])
        self.c.post(EP_CART_CHANGE, body)
        cart = self.c.post(EP_CART_GET, {
            "userId": self._user_id(), "storeNo": target["storeNo"],
            "saleChannel": 2, "saleType": 1, "storeBusinessType": 1,
            "tradeBusinessType": 1, "tradeOrderType": 0, "tradeChannel": "09",
            "shoppingCartType": "DirectShopping",
            "storeChannelCode": "android",   # wire 必带，缺失触发 [99997] 门店渠道no不存在（生产实测）
        }).get("data") or {}
        invalid = {k: v for k, v in {
            "saleOutSkuIds": cart.get("saleOutSkuIds"),
            "invalidSkus": cart.get("invalidSkus"),
            "stockChangeSkus": cart.get("stockChangeSkus"),
            "outSalePeriodSkuIds": cart.get("outSalePeriodSkuIds"),
        }.items() if v}
        # invalidCurrentGoodIds 为信息性"当前商品失效指纹"列表（真实 App 会话携带它继续结算，
        # wire 实证 2026-09-26），不阻断；行级失效仅在命中本次加购 SKU 时阻断。
        if invalid:
            target_sku = str(target.get("skuId"))
            hit = any(
                str(e.get("skuId") if isinstance(e, dict) else e) == target_sku
                for v in invalid.values() for e in (v or []))
            if hit:
                raise TradeError(f"本次加购 SKU 已失效（售罄/下架/库存变更），请换品: {invalid}")
        return cart

    # ---------------- App 立即购买路径（2026-09-26 抓包定案：全程无购物车端点） ----------------

    def calculate_price(self, target: dict) -> dict:
        """POST navigation/goods/sku/calculatePrice —— 选定 SKU+规格+属性后服务端算价。
        返回 totalGoodsItemPrice（折后单价，如 20 元券后 15）/ totalTradePrice / 折扣明细等；
        settle 直发行的 salePrice/totalItemAmount 必须用这些真实值（占位 0.00 会被
        [9105050200005] 拒绝——2026-09-26 生产实测）。"""
        sku_info = {
            "spuId": target["spuId"],
            "spuType": target.get("spuType") or "stand",
            "skuId": target["skuId"],
            "num": int(target.get("quantity", 1)),
            "salePrice": float(target.get("salePrice") or 0),   # SKU 原价（数值型，wire 实证 20.0）
            "specList": [{"specId": s["specId"], "specOptionId": s["specOptionId"]}
                         for s in (target.get("specList") or []) if s.get("specId")],
            "attributeList": [{"attributeId": a["attributeId"], "attributeOptionId": a["attributeOptionId"]}
                              for a in (target.get("attributeList") or []) if a.get("attributeId")],
        }
        return self.c.post(EP_CALC_PRICE, {
            "skuInfo": sku_info,
            "saleChannel": "2", "saleType": "1", "storeNo": target["storeNo"],
            "storeBusinessType": 1, "storeChannelCode": "android",
            "userId": self._user_id(),
        }).get("data") or {}

    def build_extra_entry(self, option: dict) -> dict:
        """goods.extraInfos[].extraOptions[i] → settle extraList 行（wire 模板）。"""
        return {
            "uniqueKey": str(uuidlib.uuid4()),
            "spuType": "extra",
            "spuId": option.get("spuId"),
            "skuId": option.get("skuId"),
            "skuName": option.get("name") or "",
            "skuImage": None,
            "num": None,
            "buyNum": 1,
            "price": None,
            "extraId": option.get("extraId"),
            "salePrice": str(option.get("salePrice") or "0.0"),
            "totalItemAmount": None,
            "totalItemDiscountedAmount": None,
        }

    def settle_direct(self, target: dict, price: dict, coupon_entry: dict = None,
                      extra_entries: list = None, no_recommend: bool = False) -> SettleResult:
        """立即购买路径的 settlePrice 直发（跳过购物车；wire：goods/detail→calculatePrice→settle）。

        行价格映射（wire 实证）：salePrice=totalGoodsItemPrice（折后）、marketPrice=SKU 原价、
        totalItemAmount=totalTradePrice、totalItemDiscountedAmount=totalGoodsItemPrice；
        金额去尾零（App 发 "15" 而非 "15.00"）；currentGoodId 恒 null（仅加购 change 需要）；
        extraList 必选加料组必须携带。

        券语义（2026-09-27 实弹定案）：coupon_entry=None 且 no_recommend=False 时
        recommendCoupon=true——服务端会自荐最优券并把抵扣算进 buyerRealPrice（此时直接
        create 会因「金额含券抵扣但 discountList 为空」被校验拒绝）。要真正的无券原价
        试算，必须显式 no_recommend=True（recommendCoupon=false + 空 discountList）。
        """
        def _money(v):
            d = Decimal(str(v or "0")).normalize()
            return str(d.quantize(Decimal(1))) if d == d.to_integral_value() else str(d)

        unit = _money(price.get("totalGoodsItemPrice") or price.get("totalSalePrice") or "0")
        total = _money(price.get("totalTradePrice") or "0")
        row = {
            "uniqueKey": str(uuidlib.uuid4()),
            "currentGoodId": None,
            "itemId": None,
            "orderItemNo": None,
            "buyNum": int(target.get("quantity", 1)),
            "salePrice": unit,
            "realPrice": None,
            "marketPrice": _money(target.get("salePrice") or "0"),
            "totalItemAmount": total,
            "totalItemDiscountedAmount": unit,
            "spuId": target["spuId"],
            "skuId": target["skuId"],
            "skuName": target.get("skuName") or "",
            "spuType": target.get("spuType") or "stand",
            "skuImage": target.get("imageUrl") or "",
            "clientUniqKey": None,
            "spuCategory": None,
            "tagImageUrl": None,
            "refunded": None,
            "specList": [{"specId": s.get("specId"), "specOptionId": s.get("specOptionId"),
                          "specOptionName": s.get("specOptionName")}
                         for s in (target.get("specList") or [])],   # wire 3 键模板（无 specName）
            "attributeList": target.get("attributeList") or [],
            "extraList": extra_entries or None,
            "comboGroupId": None,
            "isGift": None,
            "ruleId": None,
            "comboGroupType": None,
            "tagList": None,
            "comboItemList": None,
            "discountList": None,
            "tradeGoodsType": None,
            "nutritionInfo": target.get("nutritionInfo"),
            "promotionDiscountId": None,
        }
        discount = []
        if coupon_entry is not None:
            _, ded = self.pick_coupon([coupon_entry], total)
            discount = [self.build_discount_row(coupon_entry, ded)]
        body = {
            "settleBizInfo": {
                "businessType": 1, "orderType": 0, "storeNo": target["storeNo"],
                "userId": self._user_id(), "userType": 3,
                "deliveryType": 1, "packageFeeSelected": None,
                # 无券原价：显式 recommendCoupon=false（服务端不再自荐券，buyerRealPrice=全额）
                "recommendCoupon": coupon_entry is not None or not no_recommend,
            },
            "goodsList": [row],
            "discountList": discount,
        }
        data = self.c.post(EP_SETTLE, body).get("data") or {}
        fund = data.get("tradeFundInfo") or {}
        asset = ((data.get("assetInfo") or {}).get("userCouponInfo") or {})
        settle = SettleResult(
            confirm_order_key=str(data.get("confirmOrderKey") or ""),
            total_trade_price=str(fund.get("totalTradePrice") or "0"),
            buyer_real_price=str(fund.get("buyerRealPrice") or "0"),
            available_coupons=asset.get("availableCouponList") or [],
            order_group_list=data.get("orderGroupList") or [],
            trade_fund_info=fund,
            discount_list=data.get("discountList") or [],
            raw=data,
        )
        if not settle.confirm_order_key:
            raise TradeError("settlePrice 未返回 confirmOrderKey")
        return settle

    def settle_with_cart(self, cart: dict, store_no: str, coupon_entry: dict = None) -> SettleResult:
        """以购物车 get 快照组装 settlePrice。goodsList 行 = wire 模板（2026-09-26 实测定案）：
        购物车行改名透传（num→buyNum / imageUrl→skuImage / currentNutritionInfo→nutritionInfo，
        specList/attributeList 原样全量），其余字段显式置空——此前自造键序会触发 [91010009]。"""
        sku_rows = cart.get("skuList") or []
        if not sku_rows:
            raise TradeError("购物车为空，先 cart_add")
        goods = []
        for s in sku_rows:
            goods.append({
                "uniqueKey": str(uuidlib.uuid4()),
                "currentGoodId": s.get("currentGoodId"),
                "itemId": None,
                "orderItemNo": None,
                "buyNum": s.get("num") or 1,
                "salePrice": "0.00",                       # wire 字面值（服务端按 SKU 重算）
                "realPrice": None,
                "marketPrice": None,
                "totalItemAmount": s.get("totalTradePrice") or "0.00",
                "totalItemDiscountedAmount": "0.00",
                "spuId": s.get("spuId"),
                "skuId": s.get("skuId"),
                "skuName": s.get("skuName") or "",
                "spuType": s.get("spuType") or "stand",
                "skuImage": s.get("imageUrl") or "",
                "clientUniqKey": None,
                "spuCategory": None,
                "tagImageUrl": None,
                "refunded": None,
                "specList": s.get("specList") or [],
                "attributeList": s.get("attributeList") or [],
                "extraList": None,
                "comboGroupId": None,
                "isGift": None,
                "ruleId": None,
                "comboGroupType": None,
                "tagList": None,
                "comboItemList": None,
                "discountList": None,
                "tradeGoodsType": None,
                "nutritionInfo": s.get("currentNutritionInfo"),
                "promotionDiscountId": None,
            })
        discount = []
        if coupon_entry is not None:
            total = Decimal(str(cart.get("totalTradePrice") or "0"))
            _, ded = self.pick_coupon([coupon_entry], str(total))
            discount = [self.build_discount_row(coupon_entry, ded)]
        body = {
            "settleBizInfo": {
                "businessType": 1, "orderType": 0, "storeNo": store_no,
                "userId": self._user_id(), "userType": 3,
                "deliveryType": 1, "packageFeeSelected": None,
                "recommendCoupon": coupon_entry is None,
            },
            "goodsList": goods,
            "discountList": discount,
        }
        data = self.c.post(EP_SETTLE, body).get("data") or {}
        fund = data.get("tradeFundInfo") or {}
        asset = ((data.get("assetInfo") or {}).get("userCouponInfo") or {})
        settle = SettleResult(
            confirm_order_key=str(data.get("confirmOrderKey") or ""),
            total_trade_price=str(fund.get("totalTradePrice") or "0"),
            buyer_real_price=str(fund.get("buyerRealPrice") or "0"),
            available_coupons=asset.get("availableCouponList") or [],
            order_group_list=data.get("orderGroupList") or [],
            trade_fund_info=fund,
            discount_list=data.get("discountList") or [],
            raw=data,
        )
        if not settle.confirm_order_key:
            raise TradeError("settlePrice 未返回 confirmOrderKey")
        return settle

    def create_order(self, settle: SettleResult, store_no: str, store_name: str,
                     discount_rows: list = None):
        """下单（写操作）。零元 → OrderOutcome；差额 → PayLink。
        网络异常/超时向上抛 OrderHangError——调用方必须先 getOrderList 查单防悬挂。"""
        body = self.build_create_body(settle, self.mobile_cipher(), discount_rows)
        self.fill_identity(body, store_no, store_name)
        try:
            data = self.c.post(EP_CREATE, body, timeout=20).get("data") or {}
        except Exception as e:
            raise OrderHangError(
                f"createOrder 结果不确定（{e}）：先 getOrderList(orderNo 前缀/最新单) 查单，"
                f"确认未创建后才可重试（createOrder 无显式幂等键）") from e
        scenario = settle.scenario
        if scenario is PayScenario.ZERO:
            self.assert_zero_response(data)
            detail = self.order_detail(str(data["orderNo"]))
            return self._outcome(detail)
        return self.parse_pay_payload(data)

    def continue_pay(self, order_no: str, pay_type: int = 60) -> PayLink:
        """待支付单续付：重铸全新支付串（10 分钟窗口内，免走购物车）。"""
        body = {"tcode": "CHAGEE", "userId": self._user_id(),
                "orderNo": order_no, "channelCode": "UnionPay", "payType": pay_type}
        data = self.c.post(EP_CONTINUE_PAY, body, timeout=20).get("data") or {}
        return self.parse_pay_payload(data)

    def order_status(self, order_no: str) -> int:
        body = {"userId": self._user_id(), "orderNo": order_no}
        return int(self.c.post(EP_ORDER_STATUS, body).get("data"))

    def order_detail(self, order_no: str) -> dict:
        return self.c.post(EP_ORDER_DETAIL, {
            "userId": self._user_id(), "orderNo": order_no,
            "channelCode": "android"}).get("data") or {}

    def order_list(self, tab_type: str = "today", page: int = 1, size: int = 10) -> list:
        data = self.c.post(EP_ORDER_LIST, {
            "userId": self._user_id(), "pageSize": size, "pageNum": page,
            "orderType": "1", "tabType": tab_type, "channelCode": "android"},
        ).get("data") or {}
        return data.get("orderList") or data.get("pageList") or []

    def waiting_info(self, store_no: str, order_no: str, unique_pos_order_no: str) -> dict:
        """取餐等待信息（F6）。wire 实证（2026-09-26 抓包）：
        请求体 {storeNo, orderNo, uniquePosOrderNo}（无 userId），
        响应 data = {waitingCups, waitingTime(秒), queueLimit}。
        uniquePosOrderNo 须先从 getOrderList/getOrderDetail 行获取。
        """
        return self.c.post(EP_WAITING, {
            "storeNo": store_no, "orderNo": order_no,
            "uniquePosOrderNo": unique_pos_order_no}).get("data") or {}

    def wait_status(self, order_no: str, until=(3, 6, 7), timeout: int = 660,
                    poll: float = 2.0) -> OrderOutcome:
        """轮询到终态（默认 11 分钟 ≈ 10 分钟支付窗 + 余量）。s7 视为合法终态（autoCancel）。"""
        deadline = time.time() + timeout
        delay = poll
        while time.time() < deadline:
            try:
                st = self.order_status(order_no)
                if st in until:
                    return self._outcome(self.order_detail(order_no))
            except Exception:
                pass  # 单次轮询失败继续（服务端抖动）
            time.sleep(delay)
            delay = min(delay * 1.5, 6.0)
        raise TradeError(f"wait_status 超时 {timeout}s（orderNo={order_no}）")

    def verify(self, order_no: str, expect_pay: str, coupon_code: str = None) -> dict:
        """成单一致性校验：payAmount == 期望 && 券核销 promotionId == couponCode。"""
        d = self.order_detail(order_no)
        report = {"orderNo": order_no, "status": d.get("orderStatus"),
                  "statusText": d.get("orderStatusText")}
        actual = Decimal(str(d.get("payAmount") or "0"))
        if actual != Decimal(str(expect_pay)):
            raise ConsistencyError(
                f"payAmount 不符: 期望 {expect_pay} 实际 {actual} (orderNo={order_no})")
        if coupon_code:
            promos = d.get("orderPromotions") or []
            hit = [p for p in promos if str(p.get("promotionId")) == str(coupon_code)]
            if not hit:
                raise ConsistencyError(
                    f"券未核销: couponCode={coupon_code} promotions={promos}")
            report["coupon"] = {"promotionId": coupon_code,
                                "name": hit[0].get("promotionName"),
                                "discountAmount": hit[0].get("discountAmount")}
        report["pickupNo"] = d.get("pickupNo", "")
        report["payTypeText"] = d.get("payTypeText", "")
        return report

    def cancel(self, order_no: str) -> dict:
        """兜底取消（cancelOrder 静态端点，无 wire 样本——请求体按同族接口推断，标注待实测）。"""
        return self.c.post(EP_CANCEL, {
            "userId": self._user_id(), "orderNo": order_no,
            "channelCode": "android"})

    def pay_deadline(self, detail: dict):
        """s1 专属：paymentExpiryTimestamp（毫秒）→ 秒级截止；无则返回 None。"""
        ts = detail.get("paymentExpiryTimestamp")
        return int(ts) / 1000 if ts else None

    # ---------- 内部 ----------

    @staticmethod
    def _outcome(detail: dict) -> OrderOutcome:
        return OrderOutcome(
            order_no=str(detail.get("orderNo", "")),
            status=int(detail.get("orderStatus") or 0),
            status_text=str(detail.get("orderStatusText", "")),
            pay_amount=str(detail.get("payAmount") or ""),
            pay_type_text=str(detail.get("payTypeText", "")),
            pickup_no=str(detail.get("pickupNo") or ""),
            promotions=detail.get("orderPromotions") or [],
        )


if __name__ == "__main__":
    print("离线自检：以 2026-09-26 wire 存档回放三场景分类")
    for label, brp in (("零元", "0"), ("差额-10", "10"), ("差额-8", "8.00")):
        print(f"  {label}: buyerRealPrice={brp} -> {ChageeTradeApi.classify(brp).value}")
