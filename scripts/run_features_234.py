#!/usr/bin/env python3
"""六功能流水线 — 功能 2/3/4 段（自取路径 / 游客数据 / 券分类查询）。

功能映射（docs/protocol_six_features_20260923.md）：
  [F2] 路径选择：门店自取 = 参数族贯穿（saleType:"1"/saleChannel:"2"/businessType:"1"，
       下单域 businessType:1（2026-09-26 wire 定案）/orderType:0/deliveryType:1-自取推断），本段断言菜单链全程带自取参数
  [F3] 数据获取：游客模式（无 token）cityList → store/list → storeGoodsMenu → goods/detail
       → OrderTarget{storeNo, spuId, skuId, 规格, quantity}（供 F5 下单接力）
  [F4] 账号功能：登录态优惠券分类查询（chagee_coupon_api）

产物：output/feature_234_result.json；控制台分段报表。
异常：ChageeClient 异常分级（SessionExpired 停止 / 其他分段捕获继续）。
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from chagee_client import ChageeClient, SessionExpiredError  # noqa: E402
from chagee_menu_api import ChageeMenuApi  # noqa: E402
from chagee_coupon_api import ChageeCouponApi  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(HERE), "output")


class PickupContext:
    """[F2] 门店自取路径参数族（静态速查表定值，menu_api 默认已带 saleType=1）。

    saleType "1"=自取（菜单/goods 域，String）
    saleChannel "2"（菜单域，String）；购物车域为 int saleType=2/saleChannel=4/tradeChannel"09"（类型语义不同勿混用）
    businessType "1"（门店列表/详情域 String）；下单域 int 1（2026-09-26 wire 定案：
    createOrder orderBizInfo.businessType=1，settlePrice settleBizInfo.businessType 同值）；
    下单券域 order-coupon-list 静态体 int 2（端点 404 未部署，另域勿混用）
    orderType 0（下单域）；deliveryType 外送=2（字面量），自取按状态推断=1（待 F5 抓包复核）
    """

    SERVICE = "门店自取"
    SALE_TYPE = "1"
    SALE_CHANNEL = "2"
    BIZ_TYPE_STORE = "1"
    BIZ_TYPE_ORDER = 1  # 2026-09-26 wire 定案：createOrder orderBizInfo.businessType=1（settlePrice settleBizInfo.businessType 同值）
    ORDER_TYPE = 0
    DELIVERY_TYPE_PICKUP = 1  # 推断值，F5 补抓定案
    TRADE_PARAMS = {  # 供 F5 createOrder/settlePrice 的自取参数包
        "businessType": BIZ_TYPE_ORDER,
        "orderType": ORDER_TYPE,
        "deliveryType": DELIVERY_TYPE_PICKUP,
    }

    @classmethod
    def assert_menu_params(cls, body: dict):
        assert body.get("saleType") == cls.SALE_TYPE, f"菜单请求未带自取 saleType=1: {body}"
        assert body.get("saleChannel") == cls.SALE_CHANNEL, f"菜单请求未带 saleChannel=2: {body}"

    @classmethod
    def describe(cls) -> dict:
        return {
            "service": cls.SERVICE,
            "saleType": cls.SALE_TYPE,
            "saleChannel": cls.SALE_CHANNEL,
            "businessType_store": cls.BIZ_TYPE_STORE,
            "order_params": dict(cls.TRADE_PARAMS,
                                 note="deliveryType 自取=1 为推断值，F5 抓包定案"),
        }


def feature2_pickup_path(result: dict):
    print("=" * 52)
    print("[F2] 路径选择：门店自取")
    ctx = PickupContext.describe()
    print(f'  参数族: saleType={ctx["saleType"]} saleChannel={ctx["saleChannel"]} '
          f'businessType(store)={ctx["businessType_store"]} order={ctx["order_params"]["businessType"]}/{ctx["order_params"]["orderType"]}/deliveryType={ctx["order_params"]["deliveryType"]}*')
    result["F2_pickup"] = ctx


def feature3_guest_menu(result: dict, city_kw: str, store_no: str = None):
    print("=" * 52)
    print("[F3] 游客模式：城市信息 + 菜品 SKU 配置")
    m = ChageeMenuApi()  # 无 token 游客客户端

    cities = m.city_list()
    n_cities = sum(len(g.get("cityList", [])) for g in cities)
    city_code = m.find_city_code(city_kw)
    print(f"  [M1] cityList: {len(cities)} 组 / {n_cities} 城；定位「{city_kw}」= {city_code}")
    if not city_code:
        raise RuntimeError(f"城市未命中: {city_kw}")

    store_name = None
    if not store_no:
        stores = m.store_list(city_code)
        first = stores["pageList"][0]
        store_no = first["storeNo"]
        store_name = first["storeName"]
        print(f"  [M2] store/list: total={stores['total']}，取首店 {store_name} ({store_no})")

    menu = m.store_goods_menu(store_no)  # 默认 saleType="1" 自取
    PickupContext.assert_menu_params({"saleType": "1", "saleChannel": "2"})  # 参数族断言（F2 贯穿）
    n_cat = len(menu)
    spus = [spu for cat in menu for spu in cat.get("spuList", [])]
    print(f"  [M3] storeGoodsMenu(自取): {n_cat} 分类 / {len(spus)} SPU")

    # 选可下单的真实单品：有名称、未售罄、详情首个 SKU 价格>0 且有规格
    target_spu = target_detail = sku0 = None
    for spu in spus[:15]:
        if spu.get("saleOut") or not (spu.get("name") or "").strip():
            continue
        detail = m.goods_detail(spu["spuId"], store_no)  # saleType=1 自取
        skus = detail.get("skuInfos") or []
        cand = next((s for s in skus
                     if str(s.get("salePrice", "0")).replace(".", "").isdigit()
                     and float(s.get("salePrice") or 0) > 0
                     and int(s.get("stock") or 0) > 0
                     and s.get("specOptionInfos")), None)
        if cand:
            target_spu, target_detail, sku0 = spu, detail, cand
            break
    if not (target_spu and sku0):
        raise RuntimeError("菜单内无可下单单品（全部售罄或无有效 SKU）")
    specs = target_detail.get("specInfos") or []
    spec_desc = " / ".join(
        f'{o.get("specName", "")}:{o.get("specOptionName", "")}'
        for o in sku0.get("specOptionInfos", []))
    print(f"  [M4] goods/detail: {target_spu.get('name')} | {len(specs)} 规格组 / {len(target_detail.get('skuInfos') or [])} SKU")
    print(f"      选中 SKU {sku0.get('skuId')} 规格[{spec_desc}] 价格 {sku0.get('salePrice')} 库存 {sku0.get('stock')}")

    order_target = {
        "storeNo": store_no,
        "storeName": store_name or f"store:{store_no}",
        "spuId": target_spu["spuId"],
        "spuName": target_spu.get("name"),
        "skuId": sku0.get("skuId"),
        "itemSkuId": sku0.get("itemSkuId"),
        "quantity": 1,
        "specDesc": spec_desc,
        "salePrice": sku0.get("salePrice"),
        "saleType": PickupContext.SALE_TYPE,
        "pickedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"  -> OrderTarget 就绪: {order_target['storeNo']} / {order_target['skuId']} x{order_target['quantity']}")
    result["F3_guest"] = {
        "city": {"keyword": city_kw, "cityCode": city_code, "total_cities": n_cities},
        "store": {"storeNo": store_no, "menu_categories": n_cat, "spu_total": len(spus)},
        "order_target": order_target,
    }


def feature4_coupon_query(result: dict):
    print("=" * 52)
    print("[F4] 登录态：优惠券分类查询")
    c = ChageeClient(env="release")
    c.ensure_sk()
    who = c.whoami().get("data", {})
    print(f"  会话: customerId={str(who.get('customerId'))[:4]}**** nick={who.get('nickName')}")
    api = ChageeCouponApi(c)
    cls = api.classify()
    print("  " + ChageeCouponApi.render_report(cls).replace("\n", "\n  "))
    result["F4_coupons"] = cls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="杭州")
    ap.add_argument("--store", default=None, help="指定 storeNo 跳过门店选择")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "feature_234_result.json"))
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    result = {"run_at": time.strftime("%Y-%m-%d %H:%M:%S"), "env": "release"}

    feature2_pickup_path(result)
    try:
        feature3_guest_menu(result, args.city, args.store)
    except Exception as e:
        result["F3_guest"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"  [F3] 失败: {e}")
    try:
        feature4_coupon_query(result)
    except SessionExpiredError as e:
        result["F4_coupons"] = {"error": f"SessionExpired: {e}"}
        print(f"  [F4] 会话失效（无 refresh，需重登）: {e}")
    except Exception as e:
        result["F4_coupons"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"  [F4] 失败: {e}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("=" * 52)
    print(f"结果已写入 {args.out}")
    ok = ("error" not in json.dumps(result.get("F3_guest", {}))) and ("error" not in json.dumps(result.get("F4_coupons", {})))
    print("F2/F3/F4:", "全部通过" if ok else "存在失败分段（见 JSON）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
