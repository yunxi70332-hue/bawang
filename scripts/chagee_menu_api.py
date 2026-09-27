#!/usr/bin/env python3
"""
Chagee 菜单浏览链 API（无 token 游客接口封装）

适用范围：门店 + 商品 SKU 菜单浏览（chagee-navigation-web 服务全系）。
鉴权结论（2026-09-22 生产 gw.chagee.com 实测，docs/field_genealogy_20260922.md）：
  - 全部接口为明文 JSON 浏览接口：不需要 authorization(token) / sk / sign / 字段加密
  - 仅需 13 个公共头 + uuid/cid（安装标识；游客接口未观测到对其真实性的校验）
  - App 自身未登录即可浏览菜单；queryMenuData 的 userId 参数在未登录时传 NULL

参数静态依据（Blutter AOT 反汇编）：
  [M2] store/list        capture flows 实测 body（chagee_dart_reverse2_20260922.flows）
  [M3] storeGoodsMenu    ChageeMenuPageCubit::queryMenuData  @0x5d91a4
                         body = {storeNo, saleType:"1", saleChannel:"2", userId: getUserInfo()?..field_7}
  [M4] goods/detail      ProductDetailCubit                  @0x7d7fd4
                         body = {spuId, storeNo, saleChannel:"2", saleType:"1"}
  [M5] store/detail      StoreDetailCubit                    @0x98a1ec
                         body = {latitude, longitude, storeNo, businessType:"1", userId, channelCode:"android"}
  [M6] getNearestStoreDetail  ChageeMenuPageCubit            @0x5da304  body 含 {longitude, latitude}
  [M7] reverseGeo        capture flows 实测 body = {"longitude":"<str>","latitude":"<str>"}

接口清单（生产 https://gw.chagee.com；test 同路由前缀）：
  [M1] POST /chagee-navigation-web/api/navigation/store/cityList         城市列表（空 body，空 body 时无 content-type 头）
  [M2] POST /chagee-navigation-web/api/navigation/store/list             门店列表（分页；data = {total, pageList}）
  [M3] POST /chagee-navigation-web/api/navigation/goods/storeGoodsMenu   门店菜单（分类[] → spuList[]，仅 SPU 层）
  [M4] POST /chagee-navigation-web/api/navigation/goods/detail           商品 SKU 详情（specInfos/skuInfos）
  [M5] POST /chagee-navigation-web/api/navigation/store/detail           门店详情（未实测，参数静态还原）
  [M6] POST /chagee-navigation-web/api/navigation/store/getNearestStoreDetail  坐标最近门店（未实测）
  [M7] POST /chagee-navigation-web/api/navigation/store/reverseGeo       坐标反解城市（test flows 捕获，生产未实测）

实测记录（2026-09-22 生产）：
  cityList → 22 城市分组；store/list(3301) → total=198，首店 CN00529 浙江杭州国大城市广场店；
  storeGoodsMenu(CN00529) → 14 分类 / 82 SPU；goods/detail → specInfos(杯型 大/中杯) +
  skuInfos（skuId/itemSkuId/barCode/salePrice/stock/specOptionInfos 规格组合）。

CLI 用法：
  python scripts/chagee_menu_api.py --city 3301 --out output/menu-cn00529          # 城市→首店→菜单
  python scripts/chagee_menu_api.py --store CN00529 --with-sku --out output/menu  # 指定门店+全量 SKU
产物：raw JSON（各接口原始响应）+ goods_flat.csv（商品行）+ sku_flat.csv（--with-sku 时）。
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, Iterator, List, Optional

# 与 chagee_protocol.py 一致的网关与公共头；本链路不需要 sk/token/sign，故独立成轻量客户端
DEFAULT_BASE = "https://gw.chagee.com"
NAV = "/chagee-navigation-web/api/navigation"

# 未登录期抓包样本的安装标识（cityList 请求头实测值）；游客接口未观测到校验，可自定义
DEFAULT_UUID = "029a1add-a99d-5fc5-a6bc-d046755108d3"

BASE_HEADERS = {
    "ua": "Dart/2.12 (dart:io)",          # 兼容头，App 固定携带
    "user-agent": "Dart/3.6 (dart:io)",   # 真实引擎版本
    "avc": "638",                          # versionCode
    "apv": "1.0.0",                        # versionName
    "tcode": "CHAGEE",
    "channel": "APP",
    "os": "android",
    "aid": "100001",
    "language": "zh_CN",
    "region": "CN",
    "devicetimezoneregion": "Asia/Shanghai",
}


class ChageeMenuApi:
    """菜单浏览链客户端（无 token）。所有方法返回 errcode=="0" 时的 data 字段。"""

    def __init__(self, base_url: str = DEFAULT_BASE, uuid: str = DEFAULT_UUID,
                 timeout: int = 20, user_id: Optional[str] = None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.user_id = user_id  # 未登录=None(不传)；登录态可传 customerId（未验证差异）
        self.headers = dict(BASE_HEADERS)
        self.headers["uuid"] = self.headers["cid"] = uuid

    # ---------- 底层 ----------

    def _post(self, path: str, body: Optional[dict]) -> dict:
        """POST 明文 JSON；返回完整响应包络 {errcode,errmsg,data,...}。

        body=None 时模仿 wire 行为：空 body 且不带 content-type（cityList 实测形态）。
        """
        headers = dict(self.headers)
        data = b""
        if body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"errcode": str(e.code), "errmsg": "HTTP %s" % e.code,
                    "body": e.read().decode(errors="replace")[:300]}

    @staticmethod
    def _check(resp: dict) -> dict:
        """校验统一包络，errcode!=0 时抛 RuntimeError（错误码体系见 docs/不走流量与验证码诊断.md）。"""
        if resp.get("errcode") != "0":
            raise RuntimeError(f"接口失败 errcode={resp.get('errcode')} errmsg={resp.get('errmsg')} "
                               f"body={resp.get('body', '')}")
        return resp.get("data")

    def _common_body(self, extra: dict) -> dict:
        """按 App 习惯拼 body：userId 仅登录态携带（未登录 NULL → 不传键）。"""
        body = dict(extra)
        if self.user_id:
            body["userId"] = self.user_id
        return body

    # ---------- [M1] 城市列表 ----------

    def city_list(self) -> List[dict]:
        """全国开店城市列表。返回 [{"word":"A","cityList":[{cityName,cityCode},...]}, ...]（按拼音首字母分组）。"""
        return self._check(self._post(f"{NAV}/store/cityList", None))

    def city_code_map(self) -> Dict[str, str]:
        """{城市名: 城市码}，如 {"杭州": "3301"}。城市码为 4 位地级市码，用于 store/list。"""
        return {c["cityName"]: c["cityCode"] for group in self.city_list() for c in group.get("cityList", [])}

    def find_city_code(self, name_kw: str) -> str:
        """按关键字模糊匹配城市名取城市码（如 '杭州' / '3301' 直接透传数字也合法）。"""
        if name_kw.isdigit():
            return name_kw
        for name, code in self.city_code_map().items():
            if name_kw in name:
                return code
        raise KeyError(f"城市未找到: {name_kw}")

    # ---------- [M2] 门店列表 ----------

    def store_list(self, city_code: str, latitude: Optional[float] = None, longitude: Optional[float] = None,
                   page_num: int = 1, page_size: int = 20, business_type: str = "1") -> dict:
        """门店列表（分页）。businessType "1"=堂食/自取（wire 实测值）。

        返回 {"total": int, "pageList": [store...]}；store 含
        storeNo/storeName/address/runningStatus(Desc)/todayRunningTime/orderDistance 等。
        latitude/longitude 可空（wire 样本为 null），传坐标则按距离排序并返回 orderDistance。
        """
        body = self._common_body({
            "latitude": latitude, "longitude": longitude,
            "pageNum": page_num, "pageSize": page_size,
            "cityCode": city_code, "businessType": business_type,
            "channelCode": "android",
        })
        return self._check(self._post(f"{NAV}/store/list", body))

    def iter_stores(self, city_code: str, latitude: Optional[float] = None, longitude: Optional[float] = None,
                    page_size: int = 20) -> Iterator[dict]:
        """自动翻页遍历城市全部门店的生成器。"""
        page = 1
        while True:
            d = self.store_list(city_code, latitude, longitude, page, page_size)
            rows = d.get("pageList") or []
            yield from rows
            if page * page_size >= int(d.get("total") or 0) or not rows:
                return
            page += 1

    # ---------- [M5][M6][M7] 门店辅助（参数静态还原 / test flows 捕获，生产未实测） ----------

    def store_detail(self, store_no: str, latitude: Optional[float] = None,
                     longitude: Optional[float] = None) -> dict:
        """单门店详情（StoreDetailCubit @0x98a1ec 参数表还原）。"""
        return self._check(self._post(f"{NAV}/store/detail", self._common_body({
            "latitude": latitude, "longitude": longitude, "storeNo": store_no,
            "businessType": "1", "channelCode": "android",
        })))

    def nearest_store(self, latitude: float, longitude: float) -> dict:
        """按坐标取最近门店（ChageeMenuPageCubit @0x5da304；App 首页定位默认门店用）。"""
        return self._check(self._post(f"{NAV}/store/getNearestStoreDetail", {
            "longitude": longitude, "latitude": latitude,
        }))

    def reverse_geo(self, latitude: float, longitude: float) -> dict:
        """坐标反解城市信息（test flows 捕获：经纬度为字符串）。"""
        return self._check(self._post(f"{NAV}/store/reverseGeo", {
            "longitude": str(longitude), "latitude": str(latitude),
        }))

    # ---------- [M3] 门店菜单（分类 + SPU） ----------

    def store_goods_menu(self, store_no: str, sale_type: str = "1", sale_channel: str = "2") -> List[dict]:
        """门店点单菜单。saleType "1"=自取（wire/字面量表实测值）；saleChannel "2"（App 固定）。

        返回分类数组，每项含 menuCategoryId/menuCategoryName/icon/top/sequence 与
        spuList[]（spuId/name/description/showPriceStart/defaultSalePrice/stock/
        saleOut/imageUrlList/skuIdList...）。注意菜单只到 SPU 层，SKU 明细走 goods_detail。
        """
        return self._check(self._post(f"{NAV}/goods/storeGoodsMenu",
                                      self._common_body({"storeNo": store_no, "saleType": sale_type,
                                                         "saleChannel": sale_channel})))

    # ---------- [M4] 商品 SKU 详情 ----------

    def goods_detail(self, spu_id: str, store_no: str, sale_type: str = "1",
                     sale_channel: str = "2") -> dict:
        """单个商品的规格与 SKU（ProductDetailCubit @0x7d7fd4 参数表）。

        返回 data 含：
          specInfos[]   规格组（specId/name/sequence + specOptions[]{specOptionId,specOptionName,defaulted}）
          skuInfos[]    SKU（skuId/itemSkuId/barCode/salePrice/stock/stockLimit/saleOut/defaulted +
                        specOptionInfos[] 规格组合，如 杯型=大杯）
          attributeInfos/extraInfos 加料与扩展（字段随品类变化）
        """
        return self._check(self._post(f"{NAV}/goods/detail",
                                      self._common_body({"spuId": spu_id, "storeNo": store_no,
                                                         "saleChannel": sale_channel, "saleType": sale_type})))

    # ---------- 数据整理 ----------

    @staticmethod
    def flatten_goods(menu: List[dict], store_no: str) -> List[dict]:
        """菜单拍平为商品行（一 SPU 一行），供 CSV/表格使用。"""
        rows = []
        for cat in menu:
            for spu in cat.get("spuList") or []:
                rows.append({
                    "storeNo": store_no,
                    "category": cat.get("menuCategoryName", ""),
                    "spuId": spu.get("spuId", ""),
                    "name": spu.get("name", ""),
                    "showPriceStart": spu.get("showPriceStart", ""),
                    "defaultSalePrice": spu.get("defaultSalePrice", ""),
                    "memberDiscounted": spu.get("memberDiscounted", ""),
                    "stock": spu.get("stock", ""),
                    "saleOut": spu.get("saleOut", ""),
                    "description": (spu.get("description") or "")[:80],
                })
        return rows

    @staticmethod
    def flatten_skus(detail: dict, store_no: str) -> List[dict]:
        """goods/detail 响应拍平为 SKU 行（一 SKU 一行，规格组合拼入 spec 列）。"""
        spu_id = detail.get("spuId", "")
        spu_name = detail.get("name", "")
        rows = []
        for sku in detail.get("skuInfos") or []:
            spec_text = "/".join(f"{o.get('specName')}={o.get('specOptionName')}"
                                 for o in sku.get("specOptionInfos") or [])
            rows.append({
                "storeNo": store_no, "spuId": spu_id, "name": spu_name,
                "skuId": sku.get("skuId", ""), "itemSkuId": sku.get("itemSkuId", ""),
                "barCode": sku.get("barCode", ""), "spec": spec_text,
                "salePrice": sku.get("salePrice", ""), "stock": sku.get("stock", ""),
                "saleOut": sku.get("saleOut", ""),
            })
        return rows

    # ---------- 快照保存 ----------

    def save_snapshot(self, out_dir: str, store_no: str, city_code: Optional[str] = None,
                      with_sku: bool = False, sku_delay: float = 0.15) -> dict:
        """拉取并保存门店菜单全量快照。

        产物（out_dir 自动创建）：
          01_cityList.json / 02_store_list.json / 03_storeGoodsMenu.json   原始响应
          goods_flat.csv   商品行（分类/SPU/价格/库存）
          sku_flat.csv     SKU 行（--with-sku 时逐 SPU 调 goods/detail，注意请求量=SPU 数）
        返回统计 {"categories","goods","skus","total"}。
        """
        os.makedirs(out_dir, exist_ok=True)

        def dump(name: str, obj) -> None:
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)

        # [M1] 城市列表（附带产物，供城市码检索）
        dump("01_cityList.json", self.city_list())

        # [M2] 门店列表（给了城市码则存该城市全量翻页结果）
        stores: List[dict] = []
        if city_code:
            for s in self.iter_stores(city_code):
                stores.append(s)
            dump("02_store_list.json", stores)

        # [M3] 菜单
        menu = self.store_goods_menu(store_no)
        dump("03_storeGoodsMenu.json", menu)
        goods = self.flatten_goods(menu, store_no)

        # UTF-8 BOM 使 Excel 正确显示中文
        with open(os.path.join(out_dir, "goods_flat.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(goods[0].keys()) if goods else ["storeNo"])
            w.writeheader()
            w.writerows(goods)

        # [M4] 逐商品 SKU 明细
        sku_rows: List[dict] = []
        if with_sku:
            details = []
            for i, g in enumerate(goods):
                try:
                    d = self.goods_detail(g["spuId"], store_no)
                    details.append(d)
                    sku_rows.extend(self.flatten_skus(d, store_no))
                except RuntimeError as e:
                    print(f"  [warn] goods_detail {g['name']}: {e}", file=sys.stderr)
                time.sleep(sku_delay)  # 礼貌限速，浏览接口亦应克制
            dump("04_goods_details.json", details)
            if sku_rows:
                with open(os.path.join(out_dir, "sku_flat.csv"), "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(sku_rows[0].keys()))
                    w.writeheader()
                    w.writerows(sku_rows)

        stats = {"categories": len(menu), "goods": len(goods), "skus": len(sku_rows),
                 "total_stores": len(stores)}
        dump("00_summary.json", stats)
        return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Chagee 无 token 菜单浏览链（门店+SKU）拉取与保存")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"网关地址（默认生产 {DEFAULT_BASE}）")
    ap.add_argument("--uuid", default=DEFAULT_UUID, help="安装标识（默认抓包样本值）")
    ap.add_argument("--city", help="城市码或城市名关键字（如 3301 / 杭州）")
    ap.add_argument("--store", help="门店编号 storeNo（缺省时取城市第一家店）")
    ap.add_argument("--lat", type=float, help="可选坐标（门店按距离排序）")
    ap.add_argument("--lon", type=float)
    ap.add_argument("--with-sku", action="store_true", help="逐商品拉取 SKU 明细（请求数=SPU 数）")
    ap.add_argument("--out", default="output/menu", help="输出目录")
    args = ap.parse_args()

    api = ChageeMenuApi(base_url=args.base, uuid=args.uuid)

    city_code = args.city
    if not args.store and not city_code:
        ap.error("需要 --store 或 --city 至少其一")

    store_no = args.store
    if not store_no:
        city_code = api.find_city_code(city_code)
        first = api.store_list(city_code, args.lat, args.lon, 1, 1)["pageList"][0]
        store_no = first["storeNo"]
        print(f"城市 {city_code} 首店: {store_no} {first.get('storeName')}")

    stats = api.save_snapshot(args.out, store_no, city_code=city_code, with_sku=args.with_sku)
    print(f"完成 → {args.out}")
    print(f"  分类 {stats['categories']} / 商品 {stats['goods']} / SKU {stats['skus']}"
          + (f" / 城市门店 {stats['total_stores']}" if stats["total_stores"] else ""))


if __name__ == "__main__":
    main()
