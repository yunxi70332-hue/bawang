"""chagee_menu_api 离线单测（纯数据变换与请求构造，不发网络）。"""
from scripts.chagee_menu_api import BASE_HEADERS, ChageeMenuApi

MENU_SAMPLE = [{
    "menuCategoryId": "1", "menuCategoryName": "轻因不扰眠系列", "sequence": 1,
    "spuList": [{
        "spuId": "1307378304845709312", "name": "桂馥兰香冰酿",
        "showPriceStart": "18.00", "defaultSalePrice": "22.00",
        "memberDiscounted": False, "stock": 9999, "saleOut": False,
        "description": "主要成分：桂花乌龙茶",
    }],
}]

DETAIL_SAMPLE = {
    "spuId": "1307378304845709312", "name": "桂馥兰香冰酿",
    "specInfos": [{
        "specId": "653599312273510400", "name": "杯型",
        "specOptions": [
            {"specOptionId": "653599312273510402", "specOptionName": "大杯"},
            {"specOptionId": "653599312273510401", "specOptionName": "中杯"},
        ],
    }],
    "skuInfos": [
        {"skuId": "1307378304858292225", "itemSkuId": "1", "barCode": "P0879",
         "salePrice": "22.00", "stock": 9999, "saleOut": False,
         "specOptionInfos": [{"specName": "杯型", "specOptionName": "大杯"}]},
        {"skuId": "1307378304858292227", "itemSkuId": "2", "barCode": "P0880",
         "salePrice": "18.00", "stock": 9999, "saleOut": False,
         "specOptionInfos": [{"specName": "杯型", "specOptionName": "中杯"}]},
    ],
}


def test_base_headers_match_genealogy():
    """13 个公共头与 docs/field_genealogy_20260922.md 的谱系一致（uuid/cid 由实例注入）。"""
    assert set(BASE_HEADERS) == {"ua", "user-agent", "avc", "apv", "tcode", "channel",
                                 "os", "aid", "language", "region", "devicetimezoneregion"}


def test_uuid_cid_injected_and_equal():
    api = ChageeMenuApi(uuid="test-uuid")
    assert api.headers["uuid"] == api.headers["cid"] == "test-uuid"


def test_common_body_omits_user_id_when_guest():
    """未登录（user_id=None）时 body 不带 userId 键，等价于 App 的 NULL 语义。"""
    api = ChageeMenuApi()
    assert "userId" not in api._common_body({"storeNo": "CN00529"})
    logged = ChageeMenuApi(user_id="1190018250")
    assert logged._common_body({})["userId"] == "1190018250"


def test_flatten_goods_rows():
    rows = ChageeMenuApi.flatten_goods(MENU_SAMPLE, "CN00529")
    assert len(rows) == 1
    r = rows[0]
    assert r["storeNo"] == "CN00529"
    assert r["category"] == "轻因不扰眠系列"
    assert r["spuId"] == "1307378304845709312"
    assert r["showPriceStart"] == "18.00"


def test_flatten_skus_rows():
    rows = ChageeMenuApi.flatten_skus(DETAIL_SAMPLE, "CN00529")
    assert len(rows) == 2
    big = next(r for r in rows if r["salePrice"] == "22.00")
    assert big["spec"] == "杯型=大杯"
    assert big["barCode"] == "P0879"
    small = next(r for r in rows if r["salePrice"] == "18.00")
    assert small["spec"] == "杯型=中杯"


def test_check_raises_on_error_code():
    try:
        ChageeMenuApi._check({"errcode": "1232020200004", "errmsg": "验证码错误"})
    except RuntimeError as e:
        assert "1232020200004" in str(e)
    else:
        raise AssertionError("errcode!=0 应抛 RuntimeError")
