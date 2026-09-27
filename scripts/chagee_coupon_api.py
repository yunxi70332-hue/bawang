#!/usr/bin/env python3
"""ChageeCouponApi — 登录态优惠券分类查询（六功能之「账号功能」，Phase 4）。

静态依据（Blutter，2026-09-23 勘探）：
  - POST /chagee-promotion-web/user-coupon/effective-list
      体 {pageNum, pageSize:20}；调用点 my_vouchers_bloc.dart:256 @0x9d2aac
  - POST /chagee-promotion-web/user-coupon/historical-list
      体 {pageNum, pageSize:20, status:<int tab 筛选>}；vouchers_history_list_bloc.dart:260 @0x9d9c54
      （生产实测：status 传 0/1/2 服务端均返回同一份，分类以列表归属为准）
  - POST /chagee-promotion-web/user-coupon/order-coupon-list
      体 {pageNum,pageSize,channelType:4,storeNo:"",businessType:2,orderType:2}
      —— 生产全变体 404（gw/gj-api × 两种前缀 × 空/真实 storeNo 均验），本版本未部署；
         下单页实际券入口待 Phase 0b 补抓观察。
  - 三端点均无 getEncryptExtra → 免签免加密，仅需常规登录态头。
  - usableScenes 取值映射（voucher_utils.dart scenesArray @0x992d40）：2=自取 6=外卖 62=团餐自提。

2026-09-23 生产实测：effective total=3 / historical total=3，
样本均为「霸王茶姬20元代金券-DN」bizType=1 status=10 benefit2Text=代金；
该账号当前无饮品兑换券（Phase 5 的 0 元策略需先经 coupon-exchange 兑换或换券，已如实标注）。
"""

import re
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from chagee_client import ChageeClient, ChageeError  # noqa: E402

PROMO = "/chagee-promotion-web/user-coupon"

# 响应字段 → 语义（观察值 + 静态模型，字段全集见 my_vouchers_entity.dart:1686-2241）
BIZ_TYPE_LABELS = {
    1: "代金券",       # 实测：20元代金券-DN
    # 兑换券的 bizType 观察值待样本补充；识别同时使用 benefit2Text/templateName 关键词兜底
}
STATUS_LABELS = {
    10: "正常",        # effective/historical 列表内均为 10；已使用/已过期细分待样本
}
USABLE_SCENES = {2: "自取", 6: "外卖", 62: "团餐自提"}

EXCHANGE_KEYWORDS = ("兑换", "换购")

# 多次卡可用次数解析（按优先级）：
#   免10杯 / 兑N杯类权益 → N 次；名称「10次卡」→ N 次；免N次 → N 次。
# 刻意不匹配裸「N杯」（如「第2杯半价」实为单次券），未命中按普通单次券计 1 次。
BENEFIT_TIMES_PATTERNS = (
    re.compile(r"免\s*(\d+)\s*杯"),
    re.compile(r"(\d+)\s*次卡"),
    re.compile(r"免\s*(\d+)\s*次"),
)


def parse_benefit_times(*texts) -> int | None:
    """从权益文本/券名解析可用次数；无法判定返回 None（按单次券计）。"""
    for t in texts:
        if not t:
            continue
        for pat in BENEFIT_TIMES_PATTERNS:
            m = pat.search(str(t))
            if m:
                return int(m.group(1))
    return None


def _fmt_ts(ms) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD HH:mm'（生产 useStartTime/useEndTime 实测为时间戳，2026-09-23）。"""
    try:
        ms = int(ms)
        if ms <= 0:
            return ""
        import time as _t
        return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ms / 1000))
    except (TypeError, ValueError):
        return ""


def _scene_label(scenes):
    if not scenes:
        return ""
    if isinstance(scenes, str):
        scenes = [s.strip() for s in scenes.split(",") if s.strip()]
    return "/".join(USABLE_SCENES.get(int(s), f"场景{s}") for s in scenes if str(s).isdigit() or str(s).lstrip("-").isdigit())


class ChageeCouponApi:
    def __init__(self, client: ChageeClient):
        self.c = client

    # ---------- 列表 ----------

    def effective_list(self, page: int = 1, page_size: int = 20):
        """可用券列表（我的-优惠券-可用 tab）。"""
        d = self.c.post(f"{PROMO}/effective-list",
                        body={"pageNum": page, "pageSize": page_size}).get("data") or {}
        return d.get("pageList") or [], d.get("total", 0)

    def historical_list(self, page: int = 1, page_size: int = 20, status: int = 0):
        """历史券列表（已使用/已过期；status 筛选生产实测未生效，分类以列表归属为准）。"""
        d = self.c.post(f"{PROMO}/historical-list",
                        body={"pageNum": page, "pageSize": page_size, "status": status}).get("data") or {}
        return d.get("pageList") or [], d.get("total", 0)

    def fetch_all(self):
        """拉全两列表（按 total 翻页）。order-coupon-list 生产 404，不入表。"""
        out = {"effective": [], "historical": []}
        for key, fetcher in (("effective", self.effective_list), ("historical", self.historical_list)):
            items, total = fetcher(1)
            pages = max(1, -(-total // 20)) if total else 1
            for p in range(2, pages + 1):
                more, _ = fetcher(p)
                items.extend(more)
            out[key] = items
        return out

    # ---------- 分类与识别 ----------

    @staticmethod
    def classify_item(item: dict, bucket: str) -> dict:
        """单券归一化：bucket 列表归属 + bizType/benefit 关键词 + 场景映射。"""
        name = item.get("templateName") or ""
        b2 = item.get("benefit2Text") or ""
        biz = item.get("bizType")
        is_exchange = any(k in name for k in EXCHANGE_KEYWORDS) or any(k in b2 for k in EXCHANGE_KEYWORDS)
        return {
            "bucket": bucket,                                  # effective=可用 / historical=历史
            "couponCode": item.get("couponCode"),
            "templateName": name,
            "bizType": biz,
            "bizLabel": BIZ_TYPE_LABELS.get(biz, "兑换券" if is_exchange else f"type{biz}"),
            "benefitText": item.get("benefitText"),
            "benefit2Text": b2,
            # 可用次数：多次卡按权益次数（免10杯→10），普通单次券为 None（汇总按 1 计）
            "benefitTimes": parse_benefit_times(item.get("benefitText"), b2, name),
            "status": item.get("status"),
            "statusLabel": STATUS_LABELS.get(item.get("status"), str(item.get("status"))),
            "amount": item.get("amount"),
            "currency": item.get("currency"),
            "thresholdType": item.get("thresholdType"),
            "thresholdValue": item.get("thresholdValue"),
            "usableScenes": _scene_label(item.get("usableScenes")),
            # 生产实测：useStartTime/useEndTime 为毫秒时间戳；*_Str 字段不存在（此前显示 "? ~ ?" 的根因）
            "useStartTimeStr": item.get("useStartTimeStr") or _fmt_ts(item.get("useStartTime")),
            "useEndTimeStr": item.get("useEndTimeStr") or _fmt_ts(item.get("useEndTime")),
            "receiveTime": _fmt_ts(item.get("receiveTime")),
            "templateNo": item.get("templateNo"),
            "isExchangeVoucher": is_exchange,
        }

    def classify(self):
        """分类查询主入口：全量拉取 → 归一化 → 汇总统计 + 兑换券清单。

        张数（*_total）与次数（effective_usable_times）分开统计：
        10 次卡一张即 10 次可用，「可用数量」以次数为准（2 张免10杯 = 20 次）。
        """
        raw = self.fetch_all()
        classified = ([self.classify_item(i, "可用") for i in raw["effective"]]
                      + [self.classify_item(i, "历史") for i in raw["historical"]])
        by_biz = {}
        for it in classified:
            by_biz.setdefault(f'{it["bizLabel"]}(bizType={it["bizType"]})', {"可用": 0, "历史": 0})
            by_biz[f'{it["bizLabel"]}(bizType={it["bizType"]})'][it["bucket"]] += 1
        effective_usable_times = sum(i["benefitTimes"] or 1 for i in classified if i["bucket"] == "可用")
        return {
            "summary": {
                "effective_total": len(raw["effective"]),
                "effective_usable_times": effective_usable_times,
                "historical_total": len(raw["historical"]),
                "by_type": by_biz,
                "order_coupon_list": "生产 404 未部署（2026-09-23 实测，下单券入口待补抓）",
            },
            "coupons": classified,
            "exchange_vouchers": [i for i in classified if i["isExchangeVoucher"]],
        }

    @staticmethod
    def render_report(result: dict) -> str:
        s = result["summary"]
        lines = [
            "== 优惠券分类查询 ==",
            f'可用(effective-list): {s["effective_total"]} 张 / 可用次数 {s["effective_usable_times"]} 次'
            f' | 历史(historical-list): {s["historical_total"]} 张',
        ]
        for t, c in s["by_type"].items():
            lines.append(f'  {t}: 可用 {c["可用"]} / 历史 {c["历史"]}')
        lines.append(f'  order-coupon-list: {s["order_coupon_list"]}')
        lines.append(f'饮品兑换券: {len(result["exchange_vouchers"])} 张')
        for i in result["coupons"]:
            scenes = f' 场景:{i["usableScenes"]}' if i["usableScenes"] else ""
            lines.append(f'  [{i["bucket"]}] {i["templateName"]} benefit={i["benefitText"]}{i["benefit2Text"] or ""}'
                         f' times={i["benefitTimes"] or 1} status={i["statusLabel"]}{scenes}'
                         f' 有效期:{i["useStartTimeStr"] or "?"}~{i["useEndTimeStr"] or "?"}'
                         f' code={str(i["couponCode"])[:6]}...')
        return "\n".join(lines)


if __name__ == "__main__":
    c = ChageeClient(env="release")
    c.ensure_sk()
    api = ChageeCouponApi(c)
    print(ChageeCouponApi.render_report(api.classify()))
