"""本地菜单规格库服务：游客菜单权威源 → 本地缓存 → 兜底命中链。

背景（2026-09-27）：客户代下单平台仅给文案规格（"大杯/少冰/半糖"），F5 协议链需要
数字 ID（specId/specOptionId/attributeId/attributeOptionId）。规格主数据是全局字典
（甜度组 623882672850116609 的半糖=623882672850116613 等 ID 跨门店稳定，CN00529
快照与佛山/衡阳/南京下单 wire 双证），因此可以本地化：

  权威源   游客菜单（无登录态）：storeGoodsMenu（SPU 清单）+ goods/detail（规格全量）
  本地库   menu_spec_options（全局主数据）/ menu_goods_cache（门店×SPU 快照）
  更新机制 ① get_goods 本地优先（TTL 外回源 write-through）
           ② 每日定时全店刷新（活跃门店 = 有订单记录 ∪ 已缓存门店）
           ③ POST /api/ops/menu/spec/refresh 手动触发
  兜底链   L1 本地 sku_index 命中 → L2 该门店全量回源后重查 → L3 文案解析
           （归一化 + 保守别名表 + 包含式模糊）→ 歧义（0/多候选）拒绝并列出候选，
           宁可不猜不可猜错——糖度做错比下单失败更贵。

启动：app.py startup 调 start_menu_refresh_thread()（daemon，间隔环境变量
CHAGEE_MENU_REFRESH_INTERVAL_SECONDS，默认 86400=每日；<=0 不启动）。
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database import SessionLocal
from models import MenuGoodsCache, MenuRefreshLog, MenuSpecOption, OrderRecord
from services import chagee_bridge as bridge

logger = logging.getLogger(__name__)

MENU_TTL_SECONDS = int(os.environ.get("CHAGEE_MENU_TTL_SECONDS", 86400))   # 单 SPU 快照有效期
REFRESH_INTERVAL = int(os.environ.get("CHAGEE_MENU_REFRESH_INTERVAL_SECONDS", 86400))


class SpecResolveError(Exception):
    """规格解析失败（零候选/多候选/缺必选组）。message 面向人工；candidates 供 UI 列选项。"""

    def __init__(self, message: str, candidates: list | None = None):
        super().__init__(message)
        self.candidates = candidates or []


# 客户/三方文案 → 官方 optionName 的保守别名（仅收录产业内确定等价；温度类如
# "温热"≠"热" 一律不收——错温比缺温更贵）。键为归一化别名，值为归一化官方名。
SPEC_ALIASES = {
    "无糖": "不另外加糖", "0糖": "不另外加糖", "不加糖": "不另外加糖", "不要糖": "不另外加糖",
    "正常糖": "标准糖", "全糖": "标准糖",
    "正常冰": "标准冰", "标准冰度": "标准冰",
    "中杯(免费升)": "中杯",
}


def _norm(text) -> str:
    """文案归一化：去空白/全角括号统一/大小写。组名前导空格（" 温度"）也在此消除。"""
    return (re.sub(r"\s+", "", str(text or ""))
            .replace("（", "(").replace("）", ")")
            .lower())


# ---------------- 解析 goods/detail → 拍平结构 ----------------

def _parse_detail(detail: dict) -> dict:
    """goods/detail 原始响应 → 三类组拍平 + sku 索引。
    字段形态（CN00529 快照实证）：specInfos[].specOptions[]{specOptionId,specOptionName}；
    attributeInfos[].attrOptions[]{attributeOptionId,name}（注意键名 attrOptions/name）；
    skuInfos[]{skuId,itemSkuId,salePrice,stock,specOptionInfos[]}。"""
    spec_groups, attribute_groups, extra_groups = [], [], []
    for g in detail.get("specInfos") or []:
        options = [{
            "optionId": str(o.get("specOptionId") or ""),
            "optionName": str(o.get("specOptionName") or o.get("name") or ""),
            "defaulted": bool(o.get("defaulted")),
            "sequence": int(o.get("sequence") or 0),
        } for o in (g.get("specOptions") or []) if o.get("specOptionId")]
        if options:
            spec_groups.append({"groupId": str(g.get("specId") or ""),
                                "groupName": str(g.get("name") or "").strip(), "options": options})
    for g in detail.get("attributeInfos") or []:
        options = [{
            "optionId": str(o.get("attributeOptionId") or ""),
            "optionName": str(o.get("name") or o.get("attributeOptionName") or ""),
            "defaulted": bool(o.get("defaulted")),
            "sequence": int(o.get("sequence") or 0),
        } for o in (g.get("attrOptions") or g.get("attributeOptions") or []) if o.get("attributeOptionId")]
        if options:
            attribute_groups.append({
                "groupId": str(g.get("attributeId") or ""),
                "groupName": str(g.get("name") or "").strip(), "options": options,
                "must": bool(g.get("must", True)), "multiSelected": bool(g.get("multiSelected"))})
    for g in detail.get("extraInfos") or []:
        options = [{
            "optionId": str(o.get("extraOptionId") or o.get("skuId") or ""),
            "optionName": str(o.get("name") or ""),
            "defaulted": False, "sequence": int(o.get("sequence") or 0),
        } for o in (g.get("extraOptions") or []) if (o.get("extraOptionId") or o.get("skuId"))]
        if options:
            extra_groups.append({"groupId": str(g.get("extraId") or ""),
                                 "groupName": str(g.get("name") or "").strip(), "options": options,
                                 "must": bool(g.get("must"))})

    sku_index = {}
    for s in detail.get("skuInfos") or []:
        sku = str(s.get("skuId") or "")
        if not sku:
            continue
        sku_index[sku] = {
            "price": str(s.get("salePrice") or ""),
            "stock": s.get("stock"),
            "itemSkuId": str(s.get("itemSkuId") or ""),
            "specs": [{"specId": str(o.get("specId") or ""),
                       "specOptionId": str(o.get("specOptionId") or ""),
                       "specOptionName": str(o.get("specOptionName") or "")}
                      for o in (s.get("specOptionInfos") or [])],
        }
    return {"spec_groups": spec_groups, "attribute_groups": attribute_groups,
            "extra_groups": extra_groups, "sku_index": sku_index}


def _all_groups(cache_row: MenuGoodsCache) -> list[dict]:
    """规格组 + 属性组合并为可解析组池（属性组带 must 标记）。"""
    return (cache_row.spec_groups or []) + (cache_row.attribute_groups or [])


# ---------------- 落库（upsert） ----------------

def _upsert_spec_options(db: Session, parsed: dict) -> list[dict]:
    """全局规格主数据 upsert，返回本次新增的选项（新品发售信号）。"""
    new_options = []
    rows = []
    for kind, groups in (("spec", parsed["spec_groups"]),
                         ("attribute", parsed["attribute_groups"]),
                         ("extra", parsed["extra_groups"])):
        for g in groups:
            for o in g["options"]:
                rows.append((kind, g["groupId"], g["groupName"], o))
    for kind, group_id, group_name, o in rows:
        rec = (db.query(MenuSpecOption)
                 .filter(MenuSpecOption.kind == kind, MenuSpecOption.group_id == group_id,
                         MenuSpecOption.option_id == o["optionId"]).first())
        if rec is None:
            rec = MenuSpecOption(kind=kind, group_id=group_id, group_name=group_name,
                                 option_id=o["optionId"], option_name=o["optionName"],
                                 defaulted=o["defaulted"], sequence=o["sequence"])
            db.add(rec)
            new_options.append({"kind": kind, "groupName": group_name,
                                "optionName": o["optionName"], "optionId": o["optionId"]})
        else:
            rec.group_name = group_name
            rec.option_name = o["optionName"]
            rec.defaulted = o["defaulted"]
            rec.sequence = o["sequence"]
            rec.seen_count = (rec.seen_count or 0) + 1
            rec.last_seen_at = datetime.now()
    db.flush()
    return new_options


def _upsert_goods_cache(db: Session, store_no: str, spu_summary: dict,
                        detail: dict, parsed: dict) -> tuple[MenuGoodsCache, bool]:
    """门店×SPU 快照 upsert。返回 (行, 是否新增)。spu_summary 来自 storeGoodsMenu 行。"""
    spu_id = str(spu_summary.get("spuId") or detail.get("spuId") or "")
    rec = (db.query(MenuGoodsCache)
             .filter(MenuGoodsCache.store_no == store_no, MenuGoodsCache.spu_id == spu_id).first())
    is_new = rec is None
    price = spu_summary.get("defaultSalePrice") or detail.get("defaultSalePrice")
    if isinstance(price, bool) or price is None:
        price = ""
    fields = {
        "spu_name": str(spu_summary.get("name") or detail.get("name") or "")[:255],
        "spu_type": str(spu_summary.get("spuType") or detail.get("spuType") or "stand"),
        "status": int(spu_summary.get("status") or detail.get("status") or 1),
        "sale_out": bool(spu_summary.get("saleOut", detail.get("saleOut", False))),
        "default_price": str(price),
        "sku_index": parsed["sku_index"],
        "spec_groups": parsed["spec_groups"],
        "attribute_groups": parsed["attribute_groups"],
        "extra_groups": parsed["extra_groups"],
        "raw": detail,
        "fetched_at": datetime.now(),
    }
    if is_new:
        rec = MenuGoodsCache(store_no=store_no, spu_id=spu_id, **fields)
        db.add(rec)
    else:
        for k, v in fields.items():
            setattr(rec, k, v)
    db.flush()
    return rec, is_new


# ---------------- 全店刷新 ----------------

def refresh_store_menu(db: Session, store_no: str, trigger: str = "manual") -> dict:
    """全量刷新一个门店：storeGoodsMenu（SPU 清单+价格）→ 逐 SPU goods/detail → 落三表。
    diff 摘要（新品/新规格/变更）写 MenuRefreshLog 并返回。单 SPU 网络失败跳过不中断。"""
    started = time.time()
    cats = bridge.menu_api().store_goods_menu(store_no)
    spu_rows = [spu for cat in cats for spu in (cat.get("spuList") or [])]
    known = {r.spu_id: r for r in db.query(MenuGoodsCache)
             .filter(MenuGoodsCache.store_no == store_no).all()}
    spu_new, spu_changed, new_spus, changed, failed = 0, 0, [], [], []
    spec_new_total = 0
    new_spec_options: list[dict] = []
    for spu in spu_rows:
        spu_id = str(spu.get("spuId") or "")
        if not spu_id:
            continue
        try:
            detail = bridge.menu_api().goods_detail(spu_id, store_no)
        except Exception as e:   # 单品失败不阻断整店
            failed.append({"spuId": spu_id, "error": f"{type(e).__name__}: {e}"})
            continue
        parsed = _parse_detail(detail)
        new_spec_options += _upsert_spec_options(db, parsed)
        old = known.get(spu_id)
        price = spu.get("defaultSalePrice")
        if isinstance(price, bool):
            price = None
        if old is not None and (str(price) != old.default_price
                                or bool(spu.get("saleOut", False)) != bool(old.sale_out)):
            spu_changed += 1
            changed.append({"spuId": spu_id, "spuName": spu.get("name") or old.spu_name,
                            "price": {"old": old.default_price, "new": str(price or "")},
                            "saleOut": {"old": old.sale_out, "new": bool(spu.get("saleOut", False))}})
        _, is_new = _upsert_goods_cache(db, store_no, spu, detail, parsed)
        if is_new:
            spu_new += 1
            new_spus.append({"spuId": spu_id, "spuName": spu.get("name") or "",
                             "price": str(price or ""), "saleOut": bool(spu.get("saleOut", False))})
        db.commit()
    spec_new_total = len(new_spec_options)
    duration_ms = int((time.time() - started) * 1000)
    log = MenuRefreshLog(store_no=store_no, trigger=trigger, spu_total=len(spu_rows),
                         spu_new=spu_new, spu_changed=spu_changed, spec_new=spec_new_total,
                         duration_ms=duration_ms, ok=not failed,
                         error="; ".join(f['error'] for f in failed)[:255],
                         detail={"new_spus": new_spus[:50], "new_spec_options": new_spec_options[:50],
                                 "changed": changed[:50], "failed": failed[:10]})
    db.add(log)
    db.commit()
    return {"store_no": store_no, "spu_total": len(spu_rows), "spu_new": spu_new,
            "spu_changed": spu_changed, "spec_new": spec_new_total,
            "duration_ms": duration_ms, "failed": failed,
            "new_spus": new_spus, "new_spec_options": new_spec_options}


# ---------------- 本地优先读取（/api/ops/goods 改造用） ----------------

def _cache_row(db: Session, store_no: str, spu_id: str) -> MenuGoodsCache | None:
    return (db.query(MenuGoodsCache)
              .filter(MenuGoodsCache.store_no == store_no,
                      MenuGoodsCache.spu_id == spu_id).first())


def _is_stale(row: MenuGoodsCache) -> bool:
    return row is None or (datetime.now() - row.fetched_at).total_seconds() > MENU_TTL_SECONDS


def get_goods_cached(db: Session, spu_id: str, store_no: str) -> dict:
    """goods/detail 本地优先：TTL 内直接回放缓存行；陈旧/未命中回源并 write-through。
    返回**原始 goods/detail dict**（ops.py /goods 的组装逻辑零改动）。"""
    row = _cache_row(db, store_no, spu_id)
    if _is_stale(row):
        detail = bridge.menu_api().goods_detail(spu_id, store_no)
        parsed = _parse_detail(detail)
        _upsert_spec_options(db, parsed)
        _upsert_goods_cache(db, store_no, {"spuId": spu_id}, detail, parsed)
        db.commit()
        return detail
    return row.raw or {}


# ---------------- 兜底命中链（客户平台 skuId/文案 → ID 组合） ----------------

def resolve_by_sku(db: Session, store_no: str, sku_id: str) -> dict:
    """skuId → SPU 与规格组合。L1 本地 sku_index → L2 全店回源后重查（新品落地即命中）。
    返回 {spu_id, spu_name, price, stock, itemSkuId, specs[]}；未命中抛 SpecResolveError。"""
    def _find() -> MenuGoodsCache | None:
        for r in db.query(MenuGoodsCache).filter(MenuGoodsCache.store_no == store_no).all():
            if sku_id in (r.sku_index or {}):
                return r
        return None

    row = _find()
    if row is None:   # L2：本地无此 sku（新店/新品/清库后）→ 全量回源一次
        refresh_store_menu(db, store_no, trigger="ttl_miss")
        row = _find()
    if row is None:
        raise SpecResolveError(
            f"skuId {sku_id} 不在门店 {store_no} 的菜单中（已实时回源确认）")
    entry = row.sku_index[sku_id]
    return {"spu_id": row.spu_id, "spu_name": row.spu_name, "store_no": store_no,
            "sku_id": sku_id, "price": entry.get("price"), "stock": entry.get("stock"),
            "item_sku_id": entry.get("itemSkuId"), "specs": entry.get("specs") or []}


def resolve_spec_texts(db: Session, store_no: str, spu_id: str, texts: list[str]) -> dict:
    """文案规格（"大杯/少冰/半糖" 拆分后的列表）→ specList/attributeList ID 组合。
    匹配三级：归一化精确（含别名展开）→ 包含式模糊 → 歧义拒绝。
    必选组（spec + attribute must=true）缺命中时报错列出该组全部候选。"""
    row = _cache_row(db, store_no, spu_id)
    if _is_stale(row):
        get_goods_cached(db, spu_id, store_no)
        row = _cache_row(db, store_no, spu_id)
    if row is None:
        raise SpecResolveError(f"门店 {store_no} 无 SPU {spu_id} 的菜单快照")

    groups = _all_groups(row)
    # 预处理：每个选项的归一化名 + 别名展开集合
    for g in groups:
        for o in g["options"]:
            norm = _norm(o["optionName"])
            o["_norm"] = norm
            o["_aliases"] = {norm} | {a for a, target in SPEC_ALIASES.items()
                                      if target == norm}

    used_groups: dict[str, dict] = {}   # groupId → 选中的 option
    unmatched: list[str] = []
    for raw in texts:
        text = _norm(raw)
        if not text:
            continue
        hits: list[tuple[dict, dict]] = []   # (group, option)
        for g in groups:
            for o in g["options"]:
                if text in o["_aliases"]:                      # ① 精确/别名
                    hits.append((g, o))
        if not hits:
            for g in groups:                                   # ② 包含式（双向）
                for o in g["options"]:
                    if text in o["_norm"] or o["_norm"] in text:
                        hits.append((g, o))
        if len(hits) == 1:
            g, o = hits[0]
            if g["groupId"] in used_groups and used_groups[g["groupId"]]["optionId"] != o["optionId"]:
                raise SpecResolveError(
                    f"文案冲突：组「{g['groupName']}」被指定了两个不同选项"
                    f"（{used_groups[g['groupId']]['optionName']} 与 {o['optionName']}）")
            used_groups[g["groupId"]] = o
        elif len(hits) > 1:
            # 多候选：跨组同文案（如加料与属性同名）也视为歧义
            names = sorted({f"{g['groupName']}:{o['optionName']}" for g, o in hits})
            raise SpecResolveError(
                f"文案「{raw}」命中多个选项，需人工确认: {', '.join(names)}", candidates=names)
        else:
            unmatched.append(str(raw))

    # 无法识别的文案必须先报（真实根因），再谈兜底——否则"对应不上"被缺组错误掩盖
    if unmatched:
        all_names = sorted({o["optionName"] for g in groups for o in g["options"]})
        raise SpecResolveError(
            f"无法识别的规格文案: {', '.join(unmatched)}（该商品全部候选: {', '.join(all_names)}）",
            candidates=all_names)

    # 必选组兜底（App 官方语义：服务端要求每组必选；默认项优先，无默认自动选首项
    # ——F5 前端同款行为，见 ops/order 抽屉注释）。auto_filled 透出供人工复核。
    auto_filled: dict[str, str] = {}
    for g in groups:
        if g["groupId"] in used_groups:
            continue
        cands = sorted(g["options"], key=lambda o: (not o.get("defaulted"), o.get("sequence") or 0))
        if cands:
            used_groups[g["groupId"]] = cands[0]
            auto_filled[g["groupName"]] = cands[0]["optionName"]

    spec_list, attribute_list = [], []
    for g in groups:
        o = used_groups.get(g["groupId"])
        if not o:
            continue
        if g in (row.spec_groups or []):
            spec_list.append({"specId": g["groupId"], "specOptionId": o["optionId"],
                              "specOptionName": o["optionName"]})
        else:
            attribute_list.append({"attributeId": g["groupId"],
                                   "attributeOptionId": o["optionId"],
                                   "attributeOptionName": o["optionName"]})
    return {"spu_id": row.spu_id, "store_no": store_no,
            "spec_list": spec_list, "attribute_list": attribute_list,
            "resolved": {g["groupName"]: o["optionName"] for g in groups
                         if (o := used_groups.get(g["groupId"]))},
            "auto_filled": auto_filled}


# ---------------- 每日自动刷新线程 ----------------

_thread_started = False


def active_store_nos(db: Session) -> list[str]:
    """活跃门店 = 有订单记录的门店 ∪ 已建缓存的门店（新品/地区限定持续追踪面）。"""
    from sqlalchemy import distinct
    stores = {r[0] for r in db.query(distinct(OrderRecord.store_no))
              .filter(OrderRecord.store_no != "").all()}
    stores |= {r[0] for r in db.query(distinct(MenuGoodsCache.store_no)).all()}
    return sorted(stores)


def _refresh_loop(interval: int) -> None:
    while True:
        time.sleep(interval)
        try:
            with SessionLocal() as db:
                stores = active_store_nos(db)
                logger.info("菜单定时刷新开始： %d 个活跃门店", len(stores))
                for store_no in stores:
                    try:
                        summary = refresh_store_menu(db, store_no, trigger="scheduled")
                        logger.info("菜单刷新 %s： +%d 新品 /+%d 新规格 /%d 变更",
                                    store_no, summary["spu_new"], summary["spec_new"],
                                    summary["spu_changed"])
                    except Exception:
                        logger.exception("菜单刷新失败： %s", store_no)
        except Exception:
            logger.exception("菜单刷新循环异常")


def start_menu_refresh_thread() -> None:
    """幂等启动 daemon 菜单刷新线程（name="menu-refresh"）。
    间隔 CHAGEE_MENU_REFRESH_INTERVAL_SECONDS（默认 86400=每日；<=0 不启动——
    离线测试必须禁用，与 order_reconcile 线程同约定）。"""
    global _thread_started
    if _thread_started or REFRESH_INTERVAL <= 0:
        return
    _thread_started = True
    threading.Thread(target=_refresh_loop, args=(REFRESH_INTERVAL,),
                     name="menu-refresh", daemon=True).start()
