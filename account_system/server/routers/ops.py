"""协议功能路由（六功能映射）：

  [F2] 路径选择 —— 自取参数族由桥接层固定贯穿（saleType=1 等），详情接口透出给前端展示
  [F3] 游客菜单 —— /api/ops/cities|stores|menu|goods，无需茶姬登录态
  [F4] 券分类   —— /api/accounts/{id}/coupons，登录态（本文件实现）
  [F6] 取餐查询 —— 协议层 Phase 6 未开发，状态接口如实返回未开放
"""

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import ChageeAccount, SystemUser
from security import require_perm
from services import chagee_bridge as bridge
from services import decision as decision_svc
from services import menu_spec as menu_spec_service
from services.menu_spec import SpecResolveError

router = APIRouter(prefix="/api/ops", tags=["ops"])


def _get_account(db: Session, account_id: int) -> ChageeAccount:
    account = db.get(ChageeAccount, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    return account


# ---------------- 功能3：游客城市/门店/菜单/SKU ----------------

@router.get("/cities")
def cities(_: SystemUser = Depends(require_perm("feature:menu"))):
    try:
        groups = bridge.menu_api().city_list()
    except Exception as e:
        raise HTTPException(502, f"城市列表获取失败: {type(e).__name__}: {e}")
    out = []
    for g in groups:
        for c in g.get("cityList", []):
            out.append({"cityCode": c.get("cityCode"), "cityName": c.get("cityName"),
                        "provinceName": g.get("provinceName", c.get("provinceName", ""))})
    return {"total": len(out), "cities": out}


# 门店全量缓存：iter_stores 全页遍历（此前仅取第 1 页 20 条，门店多的城市被截断——
# 2026-09-26 实测佛山"龙江盈信广店"缺失）；5 分钟 TTL，避免远程搜索每击键全量拉取
_store_cache: dict[str, tuple[float, list]] = {}


@router.get("/stores")
def stores(city: str = "", page: int = 1, page_size: int = 20,
           keyword: str = "", _: SystemUser = Depends(require_perm("feature:menu"))):
    if not city:
        raise HTTPException(400, "缺少 city 参数")
    import time as _time
    cached = _store_cache.get(city)
    if cached and _time.time() - cached[0] < 300:
        items = cached[1]
    else:
        try:
            items = list(bridge.menu_api().iter_stores(city))
        except Exception as e:
            raise HTTPException(502, f"门店列表获取失败: {type(e).__name__}: {e}")
        _store_cache[city] = (_time.time(), items)
    if keyword:
        kw = keyword.strip()
        items = [s for s in items if kw in (s.get("storeName") or "") or kw in (s.get("address") or "")]
    start = (page - 1) * page_size
    return {"total": len(items), "items": items[start:start + page_size]}


@router.get("/menu")
def menu(store: str = "", _: SystemUser = Depends(require_perm("feature:menu"))):
    if not store:
        raise HTTPException(400, "缺少 store 参数")
    try:
        cats = bridge.menu_api().store_goods_menu(store)  # saleType=1 自取
    except Exception as e:
        raise HTTPException(502, f"门店菜单获取失败: {type(e).__name__}: {e}")
    out = []
    for cat in cats:
        for spu in cat.get("spuList", []):
            # showPriceStart 是「展示起售价」布尔标志，真实价格为 defaultSalePrice
            price = spu.get("defaultSalePrice") or spu.get("defaultTradePrice")
            if isinstance(price, bool) or price is None:
                price = None
            out.append({
                "categoryId": cat.get("menuCategoryId") or cat.get("id"),
                "categoryName": cat.get("menuCategoryName") or cat.get("name", ""),
                "spuId": spu.get("spuId"), "spuName": spu.get("name") or spu.get("spuName") or "",
                # 商品描述（sellingPoint 兜底）：工作台商品搜索覆盖字段之一
                "description": spu.get("description") or spu.get("sellingPoint") or "",
                "saleOut": bool(spu.get("saleOut")),
                "price": price,
                "img": (spu.get("imageUrlList") or [None])[0] or "",
            })
    return {"storeNo": store, "saleType": "1", "categories": len(cats), "items": out}


@router.get("/goods")
def goods(spu_id: str = "", store: str = "", db: Session = Depends(get_db),
          _: SystemUser = Depends(require_perm("feature:menu"))):
    if not (spu_id and store):
        raise HTTPException(400, "缺少 spu_id/store 参数")
    try:
        # 本地规格库优先（TTL 内零线上调用，陈旧/未命中自动回源并 write-through）
        detail = menu_spec_service.get_goods_cached(db, spu_id, store)
    except Exception as e:
        raise HTTPException(502, f"商品详情获取失败: {type(e).__name__}: {e}")
    skus = []
    for s in detail.get("skuInfos", []) or []:
        skus.append({
            "skuId": s.get("skuId"), "itemSkuId": s.get("itemSkuId"),
            "price": s.get("salePrice"), "stock": s.get("stock"),
            "specDesc": " / ".join(f'{o.get("specName", "")}:{o.get("specOptionName", "")}'
                                   for o in (s.get("specOptionInfos") or [])),
            # 原始规格数组透传：F5 下单工作台据此构造 spec_list（specId/specOptionId）
            "specOptionInfos": s.get("specOptionInfos") or [],
            # 营养信息（按属性组合键控）：settle 直发行 nutritionInfo 来源
            "nutrition_infos": s.get("nutritionInfos") or [],
        })
    return {
        "spuId": spu_id, "storeNo": store,
        "spuName": detail.get("name") or detail.get("spuName") or "",
        "img": (detail.get("imageUrlList") or [None])[0] or "",
        "detailImages": detail.get("detailImageUrlList") or [],
        "specGroups": detail.get("specInfos") or [],
        "attributes": detail.get("attributeInfos") or [],   # 含 defaulted 默认项，F5 加购属性来源
        "extras": detail.get("extraInfos") or [],           # 加料组（must 组必选，F5 settle extraList 来源）
        "skus": skus,
        "description": detail.get("description") or detail.get("sellingPoint") or "",
    }


# ---------------- 本地菜单规格库：刷新 / 状态 / 文案解析（兜底命中链入口）----------------

class MenuRefreshRequest(BaseModel):
    store_no: str = ""
    all_active: bool = False   # 刷新全部活跃门店（有订单记录 ∪ 已缓存门店）


@router.post("/menu/spec/refresh")
def menu_spec_refresh(body: MenuRefreshRequest, request: Request,
                      db: Session = Depends(get_db),
                      user: SystemUser = Depends(require_perm("feature:menu"))):
    """手动刷新本地规格库：单门店或全部活跃门店。返回 diff 摘要（新品/新规格/价格变更）。"""
    stores = []
    if body.all_active:
        stores = menu_spec_service.active_store_nos(db)
    elif body.store_no:
        stores = [body.store_no.strip()]
    else:
        raise HTTPException(400, "缺少 store_no 或 all_active 参数")
    if not stores:
        raise HTTPException(400, "无活跃门店（先在下单工作台选过门店，或指定 store_no）")
    results, failed = [], []
    for store_no in stores:
        try:
            results.append(menu_spec_service.refresh_store_menu(db, store_no, trigger="manual"))
        except Exception as e:
            failed.append({"store_no": store_no, "error": f"{type(e).__name__}: {e}"})
    log_audit(db, request, user, "menu.spec_refresh", ",".join(stores)[:128], {
        "stores": len(stores), "ok": len(results), "failed": len(failed),
        "spu_new": sum(r["spu_new"] for r in results),
        "spec_new": sum(r["spec_new"] for r in results)})
    return {"refreshed": len(results), "failed": failed, "results": results}


@router.get("/menu/spec/status")
def menu_spec_status(db: Session = Depends(get_db),
                     _: SystemUser = Depends(require_perm("feature:menu"))):
    """本地规格库状态总览：门店/SPU/规格主数据规模、陈旧快照数、最近刷新记录。"""
    from models import MenuGoodsCache, MenuRefreshLog, MenuSpecOption
    from datetime import datetime, timedelta
    stores = db.query(MenuGoodsCache.store_no).distinct().all()
    total_spus = db.query(MenuGoodsCache).count()
    total_options = db.query(MenuSpecOption).count()
    stale_before = datetime.now() - timedelta(seconds=menu_spec_service.MENU_TTL_SECONDS)
    stale = (db.query(MenuGoodsCache)
               .filter(MenuGoodsCache.fetched_at < stale_before).count())
    recent = (db.query(MenuRefreshLog).order_by(MenuRefreshLog.id.desc()).limit(10).all())
    return {
        "ttl_seconds": menu_spec_service.MENU_TTL_SECONDS,
        "refresh_interval_seconds": menu_spec_service.REFRESH_INTERVAL,
        "stores": [s[0] for s in stores],
        "spu_cached": total_spus,
        "spec_options": total_options,
        "stale_rows": stale,
        "recent_refreshes": [{
            "id": r.id, "store_no": r.store_no, "trigger": r.trigger,
            "spu_total": r.spu_total, "spu_new": r.spu_new, "spu_changed": r.spu_changed,
            "spec_new": r.spec_new, "ok": r.ok, "error": r.error,
            "duration_ms": r.duration_ms, "created_at": r.created_at,
            "detail": r.detail,
        } for r in recent],
    }


@router.get("/menu/spec/resolve")
def menu_spec_resolve(store: str = Query(...), sku_id: str = Query(""),
                      spu_id: str = Query(""), spec: str = Query(...),
                      db: Session = Depends(get_db),
                      _: SystemUser = Depends(require_perm("feature:menu"))):
    """兜底命中链（客户平台文案 → ID 组合）：sku_id（客户 linkId）或 spu_id 二选一；
    spec 为 '/' 分隔文案（如 "大杯/少冰/半糖"）。歧义时 422 返回候选列表（不猜）。"""
    texts = [t for t in spec.split("/") if t.strip()]
    if not (sku_id or spu_id):
        raise HTTPException(400, "缺少 sku_id 或 spu_id 参数")
    try:
        if not spu_id:
            hit = menu_spec_service.resolve_by_sku(db, store, sku_id)
            spu_id = hit["spu_id"]
        resolved = menu_spec_service.resolve_spec_texts(db, store, spu_id, texts)
    except SpecResolveError as e:
        raise HTTPException(422, {"message": str(e), "candidates": e.candidates})
    except Exception as e:
        raise HTTPException(502, f"规格解析失败: {type(e).__name__}: {e}")
    return resolved


# ---------------- 功能2：自取路径参数族（静态定义，供前端展示） ----------------

@router.get("/pickup-context")
def pickup_context(_: SystemUser = Depends(require_perm("feature:menu"))):
    return {
        "service": "门店自取",
        "params": {
            "saleType": "1（自取菜单）", "saleChannel": "2（菜单域）",
            "businessType": "1（门店域 String）/ 2（下单域 int）",
            "orderType": "0", "deliveryType": "1（自取，推断值，Phase 5 抓包定案）",
        },
        "note": "参数族贯穿 F3 菜单链与 F5 下单链（trade 家族）；F5/F6 已于 2026-09-26 集成开放。",
    }


# ---------------- 功能4：优惠券分类查询（登录态） ----------------

def _persist_coupon_records(db: Session, account, result: dict) -> int:
    """券档案全量入库：完整名称 + 券ID↔token 映射 + 使用范围（可用/历史两桶，来源 coupon_query）。
    返回本账号落库券数（同步失败不阻塞，逐张容错）。

    桶标优先级（2026-09-29 修复）：wire 的 historical-list 实测含全部券（可用40/历史40
    完全重叠），classify 顺序可用在前/历史在后，同券码 upsert 后写覆盖——若不排序，
    历史标签必然覆盖可用标签（此前生产 40 张全被标 historical 的根因）。故按
    「历史先写、可用后写」排序，同码两列表并见时可用（effective）胜出；仅出现在
    历史列表的券（真已用/过期）不受影响。"""
    from routers.orders import _upsert_coupon
    n = 0
    entries = sorted(result.get("coupons") or [],
                     key=lambda c: 1 if c.get("bucket") == "可用" else 0)   # 历史先写、可用后写
    for c in entries:
        entry = {
            "couponCode": c.get("couponCode"),
            "templateName": c.get("templateName"),
            "benefitText": c.get("benefitText"),
            "benefit2Text": c.get("benefit2Text"),
            "bizType": c.get("bizType"),
            "thresholdTips": c.get("thresholdTips"),
            "usableScenes": c.get("usableScenes"),
            "useStartTime": _to_ms(c.get("useStartTimeStr")),
            "useEndTime": _to_ms(c.get("useEndTimeStr")),
        }
        try:
            _upsert_coupon(db, account, entry,
                           "effective" if c.get("bucket") == "可用" else "historical",
                           "coupon_query")
            n += 1
        except Exception:
            pass
    return n


@router.post("/accounts/{account_id}/coupons")
def account_coupons(account_id: int, request: Request,
                    db: Session = Depends(get_db), user: SystemUser = Depends(require_perm("feature:coupon"))):
    account = _get_account(db, account_id)
    try:
        client = bridge.build_client(account)
        result = bridge.proto_coupons(client)
    except bridge.SessionExpiredError as e:
        account.status = "expired"
        db.commit()
        log_audit(db, request, user, "feature.coupon", f"{account.label}#{account.id}",
                  {"result": "expired"})
        raise HTTPException(409, f"账号凭证已失效，请重新登录后查询: {e}")
    except bridge.ChageeBridgeError as e:
        raise HTTPException(400, str(e))
    except bridge.ChageeError as e:
        raise HTTPException(502, f"协议错误: {e}")
    except Exception as e:
        raise HTTPException(502, f"请求失败: {type(e).__name__}: {e}")
    log_audit(db, request, user, "feature.coupon", f"{account.label}#{account.id}",
              {"effective": result.get("summary", {}).get("effective_total"),
               "effective_usable_times": result.get("summary", {}).get("effective_usable_times"),
               "historical": result.get("summary", {}).get("historical_total")})
    _persist_coupon_records(db, account, result)
    for c in (result.get("coupons") or []):   # 剩余有效期随响应下发（前端只渲染不计算）
        c.update(_validity_fields(c))
    return {"run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "account": {"id": account.id, "label": account.label,
                        "nickname": account.nickname, "phone_masked": account.phone or ""},
            **result}


@router.post("/coupons/sync-all")
def coupons_sync_all(request: Request, db: Session = Depends(get_db),
                     user: SystemUser = Depends(require_perm("feature:coupon"))):
    """全量优惠券查询：遍历系统内所有可登录账号的 token，逐账号批量拉取可用/历史两列表，
    收集全部优惠券 ID（coupon_code 全局去重）并全量落库券档案；单账号失败/失效不中断整体遍历。"""
    accounts = (db.query(ChageeAccount)
                  .filter(ChageeAccount.status != "disabled",
                          ChageeAccount.token != "")
                  .order_by(ChageeAccount.id).all())
    if not accounts:
        raise HTTPException(400, "系统内没有可查询的账号（需要有 token 且未停用的账号）")

    account_rows, coupons, all_codes = [], [], set()
    by_biz: dict = {}
    effective_total = historical_total = effective_usable_times = 0
    ok = expired = failed = 0
    for account in accounts:
        row = {"id": account.id, "label": account.label,
               "nickname": account.nickname,
               "phone_masked": account.phone or "",
               "status": account.status, "result": "ok", "coupons": 0, "error": ""}
        try:
            client = bridge.build_client(account)
            result = bridge.proto_coupons(client)
        except bridge.SessionExpiredError as e:
            account.status = "expired"
            db.commit()
            expired += 1
            row.update(result="expired", error=f"凭证已失效: {str(e)[:120]}")
            account_rows.append(row)
            continue
        except bridge.ChageeBridgeError as e:
            failed += 1
            row.update(result="error", error=f"桥接错误: {str(e)[:120]}")
            account_rows.append(row)
            continue
        except Exception as e:   # 协议/网络层错误：记录后继续遍历下一个账号
            failed += 1
            row.update(result="error", error=f"{type(e).__name__}: {str(e)[:120]}")
            account_rows.append(row)
            continue
        ok += 1
        persisted = _persist_coupon_records(db, account, result)
        row["coupons"] = persisted
        s = result.get("summary", {}) or {}
        effective_total += s.get("effective_total", 0)
        historical_total += s.get("historical_total", 0)
        effective_usable_times += s.get("effective_usable_times", 0)
        for t, c in (s.get("by_type") or {}).items():
            agg = by_biz.setdefault(t, {"可用": 0, "历史": 0})
            agg["可用"] += c.get("可用", 0)
            agg["历史"] += c.get("历史", 0)
        for c in (result.get("coupons") or []):
            code = str(c.get("couponCode") or "")
            if code and code not in all_codes:
                all_codes.add(code)
                coupons.append({**c,
                                "account_id": account.id,
                                "account_label": f"{account.label}#{account.id}",
                                **_validity_fields(c)})   # 剩余有效期随响应下发
        account_rows.append(row)

    log_audit(db, request, user, "feature.coupon_sync_all", "全账号",
              {"scanned": len(accounts), "ok": ok, "expired": expired, "failed": failed,
               "distinct_coupon_ids": len(all_codes)})
    return {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scanned": len(accounts), "ok": ok, "expired": expired, "failed": failed,
        "distinct_coupon_ids": len(all_codes),
        "accounts": account_rows,
        "summary": {
            "effective_total": effective_total,
            "effective_usable_times": effective_usable_times,
            "historical_total": historical_total,
            "by_type": by_biz,
            "order_coupon_list": "生产 404 未部署（2026-09-23 实测，下单券入口待补抓）",
        },
        "coupons": coupons,
        "exchange_vouchers": [i for i in coupons if i.get("isExchangeVoucher")],
    }


def _to_ms(t: str) -> int | None:
    """'YYYY-MM-DD HH:mm' → 毫秒时间戳（券档案入库用；解析失败返回 None 不落库）。"""
    from datetime import datetime
    try:
        return int(datetime.strptime(str(t), "%Y-%m-%d %H:%M").timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _validity_fields(c: dict) -> dict:
    """wire 券条目（*Str 时间字段）→ 剩余有效期字段（services.decision.coupon_validity
    唯一口径；失败 fail-soft 给 unknown，不阻塞查询响应）。"""
    try:
        v = decision_svc.coupon_validity(_to_ms(c.get("useStartTimeStr")),
                                         _to_ms(c.get("useEndTimeStr")))
        return {"days_remaining": v["days_remaining"],
                "validity_status": v["validity_status"]}
    except Exception:
        return {"days_remaining": None, "validity_status": "unknown"}


# ---------------- 功能6：取餐查询（已开放，实际数据走 orders 路由） ----------------

@router.get("/pickup-status")
def pickup_status(_: SystemUser = Depends(require_perm("feature:pickup"))):
    return {
        "available": True,
        "reason": "F5/F6 已开放：订单查询请使用 /api/ops/accounts/{id}/orders 系列端点"
                  "（getOrderList → getOrderDetail 取 pickupNo → getWaitingInfo 取等待杯数 → getOrderStatus 轻探针）。",
        "planned_flow": ["getOrderList 定位订单", "getOrderDetail 取 pickupNo",
                         "getWaitingInfo 取等待杯数/时间", "getOrderStatus 轮询状态"],
    }
