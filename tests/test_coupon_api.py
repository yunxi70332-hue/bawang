#!/usr/bin/env python3
"""ChageeCouponApi 可用次数统计逻辑测试（离线，不访问生产）。

背景（2026-09-26 生产实测 bug）：账号 19926070332 持有 2 张「10次卡」
（benefitText=免10杯），仪表盘「可用券数量」按张数显示 2，应为次数总和 20。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from chagee_coupon_api import ChageeCouponApi, parse_benefit_times  # noqa: E402


def _card(code="1310040328640544769", name="【D】10次卡轻因不扰眠系列7选1-直播",
          benefit="免10杯", bucket="可用"):
    return {
        "couponCode": code,
        "templateName": name,
        "bizType": 9,
        "benefitText": benefit,
        "benefit2Text": "",
        "status": 10,
        "useStartTime": 1761038400000,
        "useEndTime": 1769318340000,
        "bucket": bucket,
    }


def _cash(name="霸王茶姬20元代金券-DN", benefit="20元"):
    return {
        "couponCode": "C" + name[-2:],
        "templateName": name,
        "bizType": 1,
        "benefitText": benefit,
        "benefit2Text": "代金",
        "status": 10,
        "useStartTime": 1761038400000,
        "useEndTime": 1769318340000,
        "bucket": "可用",
    }


class _FakeCouponApi(ChageeCouponApi):
    """离线桩：跳过协议构造，仅覆盖 fetch_all。"""

    def __init__(self, effective, historical):  # noqa: D107（不调用父类 __init__）
        self._effective, self._historical = effective, historical

    def fetch_all(self):
        return {"effective": self._effective, "historical": self._historical}


# ---------- 权益次数解析 ----------

def test_parse_benefit_times_patterns():
    assert parse_benefit_times("免10杯") == 10
    assert parse_benefit_times("免 3 杯") == 3
    assert parse_benefit_times("", "免5杯") == 5          # benefitText 缺失时取 benefit2Text
    assert parse_benefit_times(None, None, "【D】10次卡系列7选1") == 10  # 名称兜底
    assert parse_benefit_times("免6次") == 6


def test_parse_benefit_times_single_use_coupons():
    # 普通单次券：无次数信息 → None（汇总按 1 计）
    assert parse_benefit_times("20元", "代金") is None
    assert parse_benefit_times("第2杯半价") is None      # 裸「N杯」不得误判为多次
    assert parse_benefit_times("轻因不扰眠系列7选1-直播") is None  # 「7选1」是 SKU 范围非次数
    assert parse_benefit_times("") is None
    assert parse_benefit_times(None, None, None) is None


# ---------- 单券归一化 ----------

def test_classify_item_marks_times():
    it = ChageeCouponApi.classify_item(_card(), "可用")
    assert it["benefitTimes"] == 10
    assert it["bucket"] == "可用"
    it2 = ChageeCouponApi.classify_item(_cash(), "可用")
    assert it2["benefitTimes"] is None


# ---------- 汇总统计：修复目标场景 ----------

def test_two_ten_use_cards_sum_to_20():
    """生产 bug 复现用例：2 张免10杯卡 → 张数 2、可用次数 20。"""
    api = _FakeCouponApi([_card(), _card(code="1310040224399572992")], [])
    r = api.classify()
    s = r["summary"]
    assert s["effective_total"] == 2
    assert s["effective_usable_times"] == 20


def test_single_card_shows_10_not_1():
    api = _FakeCouponApi([_card()], [])
    s = api.classify()["summary"]
    assert s["effective_total"] == 1
    assert s["effective_usable_times"] == 10


def test_mixed_cards_and_single_use_coupons():
    """1 张免10杯 + 2 张单次代金券 + 1 张免5杯 → 10+1+1+5=17 次 / 4 张。"""
    api = _FakeCouponApi(
        [_card(), _cash(), _cash(name="霸王茶姬10元代金券-DN", benefit="10元"),
         _card(code="X5", name="【D】5次卡系列", benefit="免5杯")],
        [])
    s = api.classify()["summary"]
    assert s["effective_total"] == 4
    assert s["effective_usable_times"] == 17


def test_historical_coupons_do_not_count_into_usable_times():
    """历史桶（已使用/过期）不计入可用次数，张数照常统计。"""
    api = _FakeCouponApi([_card()], [_card(code="H1"), _cash(name="历史代金券")])
    s = api.classify()["summary"]
    assert s["effective_total"] == 1
    assert s["effective_usable_times"] == 10
    assert s["historical_total"] == 2


def test_empty_account():
    api = _FakeCouponApi([], [])
    s = api.classify()["summary"]
    assert s["effective_total"] == 0
    assert s["effective_usable_times"] == 0


def test_missing_benefit_text_falls_back_to_name():
    """benefitText 缺失时从券名「10次卡」解析，不再退化成 1。"""
    raw = _card(benefit=None)
    it = ChageeCouponApi.classify_item(raw, "可用")
    assert it["benefitTimes"] == 10
    api = _FakeCouponApi([raw], [])
    assert api.classify()["summary"]["effective_usable_times"] == 10


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        fn()
        print(f"PASS {name}")
    print("all coupon summary tests passed")
