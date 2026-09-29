"""下单决策域路由（波2-C，契约 docs/decision_api_contract.md §5/§6）。

五组职责（金额字段全 string，时间 YYYY-MM-DD HH:MM:SS，列表 {total, items}）：
  套餐/成本规则/成本子类/配置管理 —— PacketConfig（items 级联 delete-orphan 全量替换）、
    VoucherCostRule（四类 match_type，category_id 软关联子类）、VoucherCostCategory
    （券成本子类=业务分类层：采购付费/活动免费/银行渠道，自动归类匹配；子类只做分类
    不做成本——成本金额仍由 voucher_cost_rules 唯一决定）、data/decision_config.json 读写；
  扫描与库存 —— POST /scan 复用 ops.coupons_sync_all 的全账号遍历语义
    （status!=disabled 且 token!=""，单账号失败不中断）拉券入 coupon_records，
    再对全库 effective/settle_available 券跑 resolve_cost 盘点（by_category 子类归组）；
    GET /coupon-inventory 每张券带分型（classify_coupon）、成本（resolve_cost）、
    子类归类（classify_category）与本地可用性判定；
  报表/流水 —— decision_logs 聚合（成单行 = order_no 非空且 verdict=pass）与分页查询；
  decide 决策评估 —— POST /api/ops/orders/decide（完整路径挂 global_router，
    与 orders.py 双路由模式一致，权限 feature:order）：
    menu_spec.resolve_by_sku 本地命中商品（未命中 422）→ revenue=customer_price →
    match_packets 套餐命中（packet_id 指定则校验在命中集内）→ total 估算
    （menu 价×数量；deep=true 对 top1 候选账号 settle_direct(no_recommend=True)
     服务端探针，失败降级 menu 估值）→ 券候选初筛（在线账号 + 未用 + bucket/
    有效期/门槛）→ 套餐 item 券规则过滤 → rank_candidates 排序 → 阈值判定
    （套餐级 min_profit 覆盖全局）→ DecisionLog 落库（order_no 空，blocked 也写；
    成单后由 orders.order_create 凭 decision_log_id 回填，见 §6 挂钩）。
    cost_breakdown 带 cost_category_name（选中券的子类归类，无券 ""）。
"""

import logging
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from audit import log_audit
from database import get_db
from models import (ChageeAccount, CouponRecord, DecisionLog, MenuGoodsCache,
                    OrderPlan, OrderPlanCouponPriority, OrderPlanDrink,
                    PacketConfig, PacketItem, PLAN_STRATEGY_LABELS, SystemUser,
                    VoucherCostCategory, VoucherCostRule)
from oplog import log_op
from schemas import (CostCategoryRequest, CostRuleImportRequest, CostRuleRequest,
                     DecideRequest, DecisionConfigRequest, OrderPlanRequest,
                     PacketCreateRequest, ScanRequest)
from security import require_perm
from services import chagee_bridge as bridge
from services import decision as decision_svc
from services import menu_spec as menu_spec_service
from services.menu_spec import SpecResolveError

router = APIRouter(prefix="/api/ops/decision", tags=["decision"])
# 全局域端点：decide 决策评估（契约 §5 冻结为 /api/ops/orders/decide，实现挂本文件）
global_router = APIRouter(prefix="/api/ops", tags=["decision"])

logger = logging.getLogger(__name__)

# 套餐候选票：owner 票 + 备选票
ALTERNATIVES_LIMIT = 5


# ---------------- 本地工具：金额/时间/序列化 ----------------

def _to_dec(v) -> Decimal:
    """金额安全转 Decimal：None/空/非法 → 0（展示聚合缺省语义）。"""
    try:
        if v is None or v == "":
            return Decimal("0")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _to_dec_or_none(v) -> Decimal | None:
    try:
        if v is None or v == "":
            return None
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(v) -> str:
    """金额统一两位小数字符串（ROUND_HALF_UP）。"""
    return f"{_to_dec(v).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _packet_item_row(it: PacketItem) -> dict:
    return {
        "id": it.id, "packet_id": it.packet_id,
        "spu_id": it.spu_id, "sku_id": it.sku_id,
        "product_name": it.product_name,
        "face_price": it.face_price, "premium_price": it.premium_price,
        "is_premium": bool(it.is_premium),
        "normal_coupon_rule": it.normal_coupon_rule,
        "premium_coupon_rule": it.premium_coupon_rule,
        "created_at": _fmt_dt(it.created_at),
    }


def _packet_summary(p: PacketConfig, item_count: int) -> dict:
    return {
        "id": p.id, "name": p.name, "open_flag": bool(p.open_flag),
        "min_order_amount": p.min_order_amount, "max_order_amount": p.max_order_amount,
        "available_start": p.available_start, "available_end": p.available_end,
        "min_profit": p.min_profit, "max_order_cost": p.max_order_cost, "note": p.note,
        "item_count": int(item_count or 0),
        "created_at": _fmt_dt(p.created_at), "updated_at": _fmt_dt(p.updated_at),
    }


def _packet_detail(p: PacketConfig) -> dict:
    detail = _packet_summary(p, len(p.items or []))
    detail["items"] = [_packet_item_row(it) for it in (p.items or [])]
    return detail


def _cost_rule_row(r: VoucherCostRule, category_names: dict[int, str] | None = None) -> dict:
    """成本规则行 + 子类映射（category_names={id: name}，0/缺失 → ""）。"""
    category_id = int(r.category_id or 0)
    return {
        "id": r.id, "name": r.name, "match_type": r.match_type,
        "match_value": r.match_value, "face_value": r.face_value,
        "cost_price": r.cost_price, "priority": r.priority,
        "enabled": bool(r.enabled), "note": r.note,
        "category_id": category_id,
        "category_name": (category_names or {}).get(category_id, ""),
        "created_at": _fmt_dt(r.created_at), "updated_at": _fmt_dt(r.updated_at),
    }


def _cost_category_row(c: VoucherCostCategory) -> dict:
    return {
        "id": c.id, "name": c.name, "biz_type": c.biz_type,
        "match_type": c.match_type, "match_value": c.match_value,
        "priority": c.priority, "enabled": bool(c.enabled), "note": c.note,
        "sort": c.sort,
        "created_at": _fmt_dt(c.created_at), "updated_at": _fmt_dt(c.updated_at),
    }


def _category_name_map(db: Session) -> dict[int, str]:
    """全量子类 id → name 映射（子类表小，直接查表；cost-rules 响应拼 category_name 用）。"""
    return {c.id: c.name for c in db.query(VoucherCostCategory).all()}


def _log_row(r: DecisionLog) -> dict:
    """DecisionLog 全字段（契约 §5 GET /logs）。"""
    return {
        "id": r.id, "order_no": r.order_no,
        "packet_id": r.packet_id, "packet_name": r.packet_name,
        "account_id": r.account_id, "coupon_code": r.coupon_code,
        "revenue": r.revenue, "total_trade_price": r.total_trade_price,
        "voucher_cost": r.voucher_cost, "pay_cost": r.pay_cost,
        "overhead": r.overhead, "total_cost": r.total_cost,
        "profit": r.profit, "margin": r.margin,
        "verdict": r.verdict, "blocked_reason": r.blocked_reason,
        "threshold_json": r.threshold_json, "plan_json": r.plan_json,
        "deduction_actual": r.deduction_actual, "pay_actual": r.pay_actual,
        "created_at": _fmt_dt(r.created_at),
    }


# ---------------- 套餐 CRUD ----------------

def _build_packet_items(packet: PacketConfig, items) -> None:
    """请求体 items → PacketItem 集合（append 进 relationship，cascade 统一落库）。"""
    for it in items or []:
        packet.items.append(PacketItem(
            spu_id=str(it.spu_id or ""), sku_id=str(it.sku_id or ""),
            product_name=str(it.product_name or "")[:128],
            face_price=str(it.face_price or ""), premium_price=str(it.premium_price or ""),
            is_premium=bool(it.is_premium),
            normal_coupon_rule=it.normal_coupon_rule or None,
            premium_coupon_rule=it.premium_coupon_rule or None,
        ))


def _check_packet_body(db: Session, body: PacketCreateRequest, exclude_id: int = 0):
    """套餐请求体校验：名称唯一 + items 内 sku 不重复（uq_packet_item 前置拦截）。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "套餐名称不能为空")
    q = db.query(PacketConfig).filter(PacketConfig.name == name)
    if exclude_id:
        q = q.filter(PacketConfig.id != exclude_id)
    if q.first():
        raise HTTPException(400, f"套餐名称「{name}」已存在")
    sku_ids = [str(it.sku_id or "") for it in body.items or [] if str(it.sku_id or "")]
    if len(sku_ids) != len(set(sku_ids)):
        raise HTTPException(400, "套餐 items 内 sku_id 重复（同一套餐每 sku 仅一行）")


@router.get("/packets")
def packets_list(
    keyword: str = Query("", max_length=64),
    open: bool | None = Query(None, description="开放状态过滤（缺省=全部）"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: SystemUser = Depends(require_perm("decision:manage")),
):
    q = db.query(PacketConfig)
    if keyword:
        q = q.filter(PacketConfig.name.like(f"%{keyword}%"))
    if open is not None:
        q = q.filter(PacketConfig.open_flag == open)
    total = q.count()
    rows = (q.order_by(PacketConfig.id.desc())
              .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total,
            "items": [_packet_summary(p, len(p.items or [])) for p in rows]}


@router.post("/packets")
def packet_create(body: PacketCreateRequest, request: Request,
                  db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("decision:manage"))):
    _check_packet_body(db, body)
    packet = PacketConfig(
        name=body.name.strip(),
        min_order_amount=body.min_order_amount or "0",
        max_order_amount=body.max_order_amount or "0",
        available_start=body.available_start or "",
        available_end=body.available_end or "",
        min_profit=body.min_profit or "",
        max_order_cost=body.max_order_cost or "",
        note=body.note or "",
    )
    _build_packet_items(packet, body.items)
    db.add(packet)
    db.commit()
    db.refresh(packet)
    log_audit(db, request, user, "decision.packet_create", f"套餐#{packet.id}",
              {"name": packet.name, "items": len(packet.items or [])})
    log_op(action="decision.packet_create", actor=user.username, target=f"套餐#{packet.id}",
           params={"name": packet.name, "items": len(packet.items or [])})
    return _packet_detail(packet)


def _get_packet(db: Session, packet_id: int) -> PacketConfig:
    packet = db.get(PacketConfig, packet_id)
    if not packet:
        raise HTTPException(404, "套餐不存在")
    return packet


@router.get("/packets/{packet_id}")
def packet_get(packet_id: int, db: Session = Depends(get_db),
               _: SystemUser = Depends(require_perm("decision:manage"))):
    return _packet_detail(_get_packet(db, packet_id))


@router.put("/packets/{packet_id}")
def packet_update(packet_id: int, body: PacketCreateRequest, request: Request,
                  db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("decision:manage"))):
    packet = _get_packet(db, packet_id)
    _check_packet_body(db, body, exclude_id=packet_id)
    packet.name = body.name.strip()
    packet.min_order_amount = body.min_order_amount or "0"
    packet.max_order_amount = body.max_order_amount or "0"
    packet.available_start = body.available_start or ""
    packet.available_end = body.available_end or ""
    packet.min_profit = body.min_profit or ""
    packet.max_order_cost = body.max_order_cost or ""
    packet.note = body.note or ""
    # items 全量替换：清空集合由 cascade delete-orphan 兜底删除旧行后重建。
    # 先 flush 落 DELETE：SQLAlchemy 单次 flush 内同表 INSERT 先于 DELETE，若新行与旧行
    # 同 (packet_id, sku_id) 会先撞 uq_packet_item 唯一约束（离线测试实测复现）
    packet.items.clear()
    db.flush()
    _build_packet_items(packet, body.items)
    db.commit()
    db.refresh(packet)
    log_audit(db, request, user, "decision.packet_update", f"套餐#{packet.id}",
              {"name": packet.name, "items": len(packet.items or [])})
    log_op(action="decision.packet_update", actor=user.username, target=f"套餐#{packet.id}",
           params={"name": packet.name, "items": len(packet.items or [])})
    return _packet_detail(packet)


@router.delete("/packets/{packet_id}")
def packet_delete(packet_id: int, request: Request, db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("decision:manage"))):
    packet = _get_packet(db, packet_id)
    name = packet.name
    db.delete(packet)   # items 随 cascade 一并删除
    db.commit()
    log_audit(db, request, user, "decision.packet_delete", f"套餐#{packet_id}", {"name": name})
    log_op(action="decision.packet_delete", actor=user.username, target=f"套餐#{packet_id}",
           params={"name": name})
    return {"ok": True}


@router.post("/packets/{packet_id}/toggle-open")
def packet_toggle_open(packet_id: int, request: Request, db: Session = Depends(get_db),
                       user: SystemUser = Depends(require_perm("decision:manage"))):
    packet = _get_packet(db, packet_id)
    packet.open_flag = not bool(packet.open_flag)
    db.commit()
    db.refresh(packet)
    log_audit(db, request, user, "decision.packet_toggle_open", f"套餐#{packet.id}",
              {"open_flag": packet.open_flag})
    log_op(action="decision.packet_toggle_open", actor=user.username, target=f"套餐#{packet.id}",
           params={"open_flag": packet.open_flag})
    return _packet_detail(packet)


# ---------------- 券成本规则 CRUD + 批量导入 ----------------

def _apply_cost_rule(rule: VoucherCostRule, body: CostRuleRequest) -> None:
    rule.name = body.name.strip()
    rule.match_type = body.match_type
    rule.match_value = body.match_value
    rule.face_value = body.face_value or ""
    rule.cost_price = body.cost_price
    rule.priority = body.priority
    rule.enabled = body.enabled
    rule.note = body.note or ""
    rule.category_id = int(body.category_id or 0)   # 软关联子类（0=未分类，不做外键校验）


@router.get("/cost-rules")
def cost_rules_list(db: Session = Depends(get_db),
                    _: SystemUser = Depends(require_perm("decision:manage"))):
    rows = (db.query(VoucherCostRule)
              .order_by(VoucherCostRule.priority.asc(), VoucherCostRule.id.asc()).all())
    category_names = _category_name_map(db)
    return {"items": [_cost_rule_row(r, category_names) for r in rows]}


@router.post("/cost-rules")
def cost_rule_create(body: CostRuleRequest, request: Request,
                     db: Session = Depends(get_db),
                     user: SystemUser = Depends(require_perm("decision:manage"))):
    rule = VoucherCostRule()
    _apply_cost_rule(rule, body)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    log_audit(db, request, user, "decision.cost_rule_create", f"规则#{rule.id}",
              {"name": rule.name, "match_type": rule.match_type, "cost_price": rule.cost_price,
               "category_id": rule.category_id})
    log_op(action="decision.cost_rule_create", actor=user.username, target=f"规则#{rule.id}",
           params={"name": rule.name, "cost_price": rule.cost_price,
                   "category_id": rule.category_id})
    return _cost_rule_row(rule, _category_name_map(db))


@router.put("/cost-rules/{rule_id}")
def cost_rule_update(rule_id: int, body: CostRuleRequest, request: Request,
                     db: Session = Depends(get_db),
                     user: SystemUser = Depends(require_perm("decision:manage"))):
    rule = db.get(VoucherCostRule, rule_id)
    if not rule:
        raise HTTPException(404, "成本规则不存在")
    _apply_cost_rule(rule, body)
    db.commit()
    db.refresh(rule)
    log_audit(db, request, user, "decision.cost_rule_update", f"规则#{rule.id}",
              {"name": rule.name, "cost_price": rule.cost_price,
               "category_id": rule.category_id})
    log_op(action="decision.cost_rule_update", actor=user.username, target=f"规则#{rule.id}",
           params={"name": rule.name, "cost_price": rule.cost_price,
                   "category_id": rule.category_id})
    return _cost_rule_row(rule, _category_name_map(db))


@router.delete("/cost-rules/{rule_id}")
def cost_rule_delete(rule_id: int, request: Request, db: Session = Depends(get_db),
                     user: SystemUser = Depends(require_perm("decision:manage"))):
    rule = db.get(VoucherCostRule, rule_id)
    if not rule:
        raise HTTPException(404, "成本规则不存在")
    name = rule.name
    db.delete(rule)
    db.commit()
    log_audit(db, request, user, "decision.cost_rule_delete", f"规则#{rule_id}", {"name": name})
    log_op(action="decision.cost_rule_delete", actor=user.username, target=f"规则#{rule_id}",
           params={"name": name})
    return {"ok": True}


@router.post("/cost-rules/import")
def cost_rules_import(body: CostRuleImportRequest, request: Request,
                      db: Session = Depends(get_db),
                      user: SystemUser = Depends(require_perm("decision:manage"))):
    """批量导入：逐条校验（非法条目进 errors 不中断），name 与库内重复跳过。
    条目字段同 CostRuleRequest（含 category_id，缺省 0=未分类）。"""
    imported, skipped, errors = 0, 0, []
    for i, raw in enumerate(body.rules or []):
        try:
            req = CostRuleRequest(**(raw or {}))
        except (ValidationError, TypeError) as e:
            errors.append({"index": i, "name": str((raw or {}).get("name") or ""),
                           "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        if db.query(VoucherCostRule).filter(VoucherCostRule.name == req.name.strip()).first():
            skipped += 1
            continue
        rule = VoucherCostRule()
        _apply_cost_rule(rule, req)
        db.add(rule)
        imported += 1
    db.commit()
    log_audit(db, request, user, "decision.cost_rule_import", "成本规则",
              {"imported": imported, "skipped": skipped, "errors": len(errors)})
    log_op(action="decision.cost_rule_import", actor=user.username, target="成本规则",
           params={"imported": imported, "skipped": skipped, "errors": len(errors)})
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ---------------- 券成本子类 CRUD（业务分类层：只做分类，不做成本） ----------------

def _check_category_body(db: Session, body: CostCategoryRequest, exclude_id: int = 0):
    """子类请求体校验：名称唯一（重名 400）。biz_type/match_type 由 schema pattern 拦截。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "子类名称不能为空")
    q = db.query(VoucherCostCategory).filter(VoucherCostCategory.name == name)
    if exclude_id:
        q = q.filter(VoucherCostCategory.id != exclude_id)
    if q.first():
        raise HTTPException(400, f"子类名称「{name}」已存在")


def _inventory_category_stats(db: Session) -> dict[int, dict]:
    """两桶（effective+settle_available）可用券逐张 resolve_cost + classify_category 的
    内存归组：{category_id: {"count", "face", "cost"(Decimal)}}（含 0=未分类，调用方按需
    取舍——cost-categories 列表不展示 0 桶，scan by_category 透出全集）。
    规则/子类/配置每请求各查一次，循环内复用（匹配为正则语义，无法下推 SQL）。"""
    rules = db.query(VoucherCostRule).all()
    categories = db.query(VoucherCostCategory).all()
    cfg = decision_svc.load_config()
    stats: dict[int, dict] = {}
    coupons = (db.query(CouponRecord)
                 .filter(CouponRecord.bucket.in_(("effective", "settle_available"))).all())
    for c in coupons:
        cat = decision_svc.classify_category(categories, c)
        agg = stats.setdefault(cat["category_id"],
                               {"count": 0, "face": Decimal("0"), "cost": Decimal("0")})
        agg["count"] += 1
        face = _to_dec_or_none(c.amount)
        if face is not None:
            agg["face"] += face
        agg["cost"] += decision_svc.resolve_cost(rules, c, cfg)["cost"]
    return stats


@router.get("/cost-categories")
def cost_categories_list(db: Session = Depends(get_db),
                         _: SystemUser = Depends(require_perm("decision:manage"))):
    """子类列表 + 两桶可用券的内存汇总（coupon_count/face_total/cost_total，逐张
    resolve_cost+classify_category 按 category_id 归组；未命中子类的券=未分类 0，
    不在此表显示——全集口径见 coupon-inventory / coupons/search 的 by_category）。"""
    rows = (db.query(VoucherCostCategory)
              .order_by(VoucherCostCategory.sort.asc(), VoucherCostCategory.priority.asc(),
                        VoucherCostCategory.id.asc()).all())
    stats = _inventory_category_stats(db)
    items = []
    for c in rows:
        agg = stats.get(c.id, {"count": 0, "face": Decimal("0"), "cost": Decimal("0")})
        items.append({**_cost_category_row(c),
                      "coupon_count": agg["count"],
                      "face_total": _money(agg["face"]),
                      "cost_total": _money(agg["cost"])})
    return {"items": items}


@router.post("/cost-categories")
def cost_category_create(body: CostCategoryRequest, request: Request,
                         db: Session = Depends(get_db),
                         user: SystemUser = Depends(require_perm("decision:manage"))):
    _check_category_body(db, body)
    category = VoucherCostCategory(
        name=body.name.strip(), biz_type=body.biz_type,
        match_type=body.match_type, match_value=body.match_value,
        priority=body.priority, enabled=body.enabled,
        note=body.note or "", sort=body.sort,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    log_audit(db, request, user, "decision.cost_category_create", f"子类#{category.id}",
              {"name": category.name, "biz_type": category.biz_type,
               "match_type": category.match_type, "match_value": category.match_value})
    log_op(action="decision.cost_category_create", actor=user.username, target=f"子类#{category.id}",
           params={"name": category.name, "biz_type": category.biz_type})
    return _cost_category_row(category)


@router.put("/cost-categories/{category_id}")
def cost_category_update(category_id: int, body: CostCategoryRequest, request: Request,
                         db: Session = Depends(get_db),
                         user: SystemUser = Depends(require_perm("decision:manage"))):
    category = db.get(VoucherCostCategory, category_id)
    if not category:
        raise HTTPException(404, "成本子类不存在")
    _check_category_body(db, body, exclude_id=category_id)
    category.name = body.name.strip()
    category.biz_type = body.biz_type
    category.match_type = body.match_type
    category.match_value = body.match_value
    category.priority = body.priority
    category.enabled = body.enabled
    category.note = body.note or ""
    category.sort = body.sort
    db.commit()
    db.refresh(category)
    log_audit(db, request, user, "decision.cost_category_update", f"子类#{category.id}",
              {"name": category.name, "biz_type": category.biz_type})
    log_op(action="decision.cost_category_update", actor=user.username, target=f"子类#{category.id}",
           params={"name": category.name, "biz_type": category.biz_type})
    return _cost_category_row(category)


@router.delete("/cost-categories/{category_id}")
def cost_category_delete(category_id: int, request: Request, db: Session = Depends(get_db),
                         user: SystemUser = Depends(require_perm("decision:manage"))):
    category = db.get(VoucherCostCategory, category_id)
    if not category:
        raise HTTPException(404, "成本子类不存在")
    name = category.name
    # 关联规则软引用回退未分类（子类删除不连带删规则，成本链不受影响）
    (db.query(VoucherCostRule)
       .filter(VoucherCostRule.category_id == category_id)
       .update({VoucherCostRule.category_id: 0}, synchronize_session=False))
    db.delete(category)
    db.commit()
    log_audit(db, request, user, "decision.cost_category_delete", f"子类#{category_id}",
              {"name": name})
    log_op(action="decision.cost_category_delete", actor=user.username, target=f"子类#{category_id}",
           params={"name": name})
    return {"ok": True}


# ---------------- 券库存扫描与盘点 ----------------

@router.post("/scan")
def decision_scan(body: ScanRequest, request: Request, db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("decision:manage"))):
    """券库存扫描：复用 ops.coupons_sync_all 的全账号遍历语义（status!=disabled 且
    token!=""，account_ids 可圈定范围；单账号凭证失效/协议异常不中断整体），拉取
    effective/historical 两桶券入 coupon_records 后，对全库 effective/settle_available
    券跑 resolve_cost 成本盘点汇总（by_category 按子类归组：count/cost_total，
    含 0=未分类桶，金额口径与 total_cost_estimate 同为逐张 resolve_cost）。"""
    from routers.ops import _persist_coupon_records   # 延迟 import：复用 F4 落库映射

    q = (db.query(ChageeAccount)
           .filter(ChageeAccount.status != "disabled", ChageeAccount.token != ""))
    if body.account_ids:
        q = q.filter(ChageeAccount.id.in_(body.account_ids))
    accounts = q.order_by(ChageeAccount.id).all()
    if not accounts:
        raise HTTPException(400, "系统内没有可查询的账号（需要有 token 且未停用的账号）")

    ok = expired = failed = 0
    for account in accounts:
        try:
            client = bridge.build_client(account)
            result = bridge.proto_coupons(client)
        except bridge.SessionExpiredError as e:
            account.status = "expired"
            db.commit()
            expired += 1
            log_op(level="WARN", action="decision.scan", actor=user.username,
                   target=f"{account.label}#{account.id}", result="expired",
                   error=e)
            continue
        except Exception as e:   # 协议/网络层错误：记录后继续下一个账号
            failed += 1
            log_op(level="WARN", action="decision.scan", actor=user.username,
                   target=f"{account.label}#{account.id}", result="failed",
                   error=e)
            continue
        ok += 1
        _persist_coupon_records(db, account, result)   # 逐张容错，内部不抛

    # 成本盘点：全库两桶可用券逐张 resolve_cost（规则匹配链 + fallback）+ classify_category
    # 子类归组（子类只做分类，cost_total 的金额口径仍逐张 resolve_cost）
    rules = db.query(VoucherCostRule).all()
    categories = db.query(VoucherCostCategory).all()
    cfg = decision_svc.load_config()
    coupons = (db.query(CouponRecord)
                 .filter(CouponRecord.bucket.in_(("effective", "settle_available"))).all())
    coupons_total = len(coupons)
    with_cost = unknown_cost = 0
    total_face = Decimal("0")
    total_cost = Decimal("0")
    by_cat: dict[int, dict] = {}
    for c in coupons:
        face = _to_dec_or_none(c.amount)
        if face is not None:
            total_face += face
        res = decision_svc.resolve_cost(rules, c, cfg)
        total_cost += res["cost"]
        if str(res["source"]).startswith("rule:"):
            with_cost += 1
        else:
            unknown_cost += 1
        cat = decision_svc.classify_category(categories, c)
        agg = by_cat.setdefault(cat["category_id"],
                                {"category_id": cat["category_id"],
                                 "category_name": cat["category_name"],
                                 "biz_type": cat["biz_type"],
                                 "count": 0, "cost_total": Decimal("0")})
        agg["count"] += 1
        agg["cost_total"] += res["cost"]
    by_category = [{**v, "cost_total": _money(v["cost_total"])}
                   for v in sorted(by_cat.values(),
                                   key=lambda x: (-x["count"], x["category_id"]))]
    summary = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "accounts_scanned": len(accounts), "accounts_ok": ok,
        "accounts_expired": expired, "accounts_failed": failed,
        "coupons_total": coupons_total, "coupons_with_cost": with_cost,
        "coupons_unknown_cost": unknown_cost,
        "total_face": _money(total_face), "total_cost_estimate": _money(total_cost),
        "by_category": by_category,
    }
    log_audit(db, request, user, "decision.scan", "全账号",
              {k: summary[k] for k in ("accounts_scanned", "accounts_ok", "coupons_total",
                                       "coupons_with_cost", "coupons_unknown_cost")})
    log_op(action="decision.scan", actor=user.username, target="全账号", params=summary)
    return summary


# ---------------- 券可用性初筛（扫描库存/decide 共用） ----------------

def _parse_threshold(tips) -> Decimal | None:
    """thresholdTips「满30元可用」→ 30；无数字（"优惠1杯"）/空 → None。"""
    m = re.search(r"(\d+(?:\.\d+)?)", str(tips or ""))
    return Decimal(m.group(1)) if m else None


def _screen_usable(record: CouponRecord, now_ms: int) -> tuple[bool, str]:
    """本地可用性初筛（无订单语境的静态部分）：canDiscount 标记 + 有效期毫秒窗口。
    返回 (可用, 不可用原因中文)；门槛校验依赖比较基准（面额/订单总额），由调用方追加。"""
    if record.can_discount is False:
        return False, "服务端标记该券当前不可用"
    start, end = record.use_start_time, record.use_end_time
    if isinstance(start, int) and now_ms < start:
        return False, f"券未到生效时间（生效于 {datetime.fromtimestamp(start / 1000).strftime('%Y-%m-%d %H:%M')}）"
    if isinstance(end, int) and now_ms > end:
        return False, f"券已过期（有效期至 {datetime.fromtimestamp(end / 1000).strftime('%Y-%m-%d %H:%M')}）"
    return True, ""


@router.get("/coupon-inventory")
def coupon_inventory(
    keyword: str = Query("", max_length=64),
    account_id: int = Query(0, ge=0),
    usable: bool | None = Query(None, description="可用性过滤（缺省=全部）"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: SystemUser = Depends(require_perm("decision:manage")),
):
    """券库存：coupon_records 每张带分型（classify_coupon）、成本（resolve_cost）、
    子类归类（classify_category：cost_category_id/name/biz_type，0=未分类）与
    本地可用性判定。usable 过滤依赖逐张文案/规则计算，Python 侧过滤后分页。"""
    q = (db.query(CouponRecord)
           .join(ChageeAccount, CouponRecord.account_id == ChageeAccount.id, isouter=True))
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(CouponRecord.template_name.like(like)
                     | CouponRecord.coupon_code.like(like))
    if account_id:
        q = q.filter(CouponRecord.account_id == account_id)
    rows = q.order_by(CouponRecord.id.desc()).all()

    rules = db.query(VoucherCostRule).all()
    categories = db.query(VoucherCostCategory).all()
    cfg = decision_svc.load_config()
    now_ms = int(time.time() * 1000)
    items = []
    for r in rows:
        kind = decision_svc.classify_coupon(r.benefit_text, r.benefit2_text,
                                            r.template_name, r.biz_type)
        cost = decision_svc.resolve_cost(rules, r, cfg)
        cat = decision_svc.classify_category(categories, r)
        # 可用性：静态初筛 + 门槛（库存无订单总额语境，按门槛与券面额比较——
        # 面额低于自身门槛的券单独使用必然不满足，折扣/兑换券无面额不作此判）
        usable_flag, reason = _screen_usable(r, now_ms)
        if usable_flag:
            threshold = _parse_threshold(r.threshold_tips)
            face = _to_dec_or_none(r.amount)
            if threshold is not None and face is not None and threshold > face:
                usable_flag = False
                reason = (f"使用门槛 {threshold} 元高于券面额 {face} 元"
                          f"（需凑单满足门槛）")
        items.append({
            "coupon_code": r.coupon_code, "template_name": r.template_name,
            "benefit_text": r.benefit_text, "account_id": r.account_id,
            "account_label": (f"{r.account.label}#{r.account.id}" if r.account else ""),
            "amount": r.amount, "threshold_tips": r.threshold_tips,
            "usable_scenes": r.usable_scenes,
            "use_start_time": r.use_start_time, "use_end_time": r.use_end_time,
            "can_discount": r.can_discount, "bucket": r.bucket,
            "coupon_kind": kind["kind"],
            "cost_price": _money(cost["cost"]), "cost_source": cost["source"],
            "cost_category_id": cat["category_id"],
            "cost_category_name": cat["category_name"],
            "biz_type": cat["biz_type"],
            "usable": usable_flag, "unusable_reason": reason,
            "last_order_no": r.last_order_no,
        })
    if usable is not None:
        items = [x for x in items if x["usable"] == usable]
    total = len(items)
    start = (page - 1) * page_size
    return {"total": total, "items": items[start:start + page_size]}


# ---------------- 全局配置 ----------------

@router.get("/config")
def config_get(_: SystemUser = Depends(require_perm("decision:manage"))):
    """data/decision_config.json 原文（文件不存在时返回默认值，不落盘）。"""
    return decision_svc.load_config()


@router.put("/config")
def config_put(body: DecisionConfigRequest, request: Request,
               db: Session = Depends(get_db),
               user: SystemUser = Depends(require_perm("decision:manage"))):
    saved = decision_svc.save_config(body.model_dump())
    log_audit(db, request, user, "decision.config_put", "全局决策配置", dict(saved))
    log_op(action="decision.config_put", actor=user.username, target="全局决策配置",
           params=dict(saved))
    return saved


# ---------------- 下单方案：策略 + 券优先级层级（2026-09-29 §11） ----------------
# 2026-09-29 合并优化：方案 CRUD 与套餐 CRUD 规范对齐——审计打标（log_audit/log_op）、
# 独立 toggle-enabled 端点（修原「行内开关走全量 PUT」缺 drink_info 必填字段致 422
# 静默失败、且 payload 漏 drinks 会清空饮品关联的缺陷）、列表 keyword/enabled 过滤。

def _plan_priority_row(p: OrderPlanCouponPriority) -> dict:
    return {"id": p.id, "plan_id": p.plan_id, "level": p.level, "name": p.name,
            "match_type": p.match_type, "match_value": p.match_value,
            "face_value": p.face_value, "created_at": _fmt_dt(p.created_at)}


def _plan_detail(plan: OrderPlan) -> dict:
    return {
        "id": plan.id, "name": plan.name, "strategy": plan.strategy,
        "strategy_label": PLAN_STRATEGY_LABELS.get(plan.strategy, plan.strategy),
        "drink_info": plan.drink_info or "",
        "note": plan.note, "enabled": bool(plan.enabled),
        "priority_count": len(plan.priorities or []),
        "priorities": [_plan_priority_row(p) for p in (plan.priorities or [])],
        "drinks": [{"id": d.id, "spu_id": d.spu_id, "sku_id": d.sku_id,
                    "drink_name": d.drink_name, "face_price": d.face_price}
                   for d in (plan.drinks or [])],
        "created_at": _fmt_dt(plan.created_at), "updated_at": _fmt_dt(plan.updated_at),
    }


def _check_plan_body(db: Session, body: OrderPlanRequest, exclude_id: int = 0):
    """方案请求体校验：重名 400。"""
    name = body.name.strip()
    q = db.query(OrderPlan).filter(OrderPlan.name == name)
    if exclude_id:
        q = q.filter(OrderPlan.id != exclude_id)
    if q.first():
        raise HTTPException(400, f"方案名已存在：{name}")


def _build_plan_drinks(plan: OrderPlan, drinks: list) -> None:
    seen: set[str] = set()
    for d in (drinks or []):
        if d.sku_id in seen:
            continue   # 同 sku 多行去重（跨门店同名 sku 只保留首个）
        seen.add(d.sku_id)
        plan.drinks.append(OrderPlanDrink(
            spu_id=d.spu_id or "", sku_id=d.sku_id,
            drink_name=d.drink_name or "", face_price=d.face_price or ""))


@router.get("/sku-search")
@router.get("/plan-drinks/search")   # 旧路径保留兼容（契约 §11；方案饮品 Tab 原专用名）
def sku_search(keyword: str = Query(..., min_length=1, max_length=64),
               limit: int = Query(20, ge=1, le=50),
               db: Session = Depends(get_db),
               _: SystemUser = Depends(require_perm("decision:manage"))):
    """SKU 模糊搜索（套餐商品 / 方案饮品共用，2026-09-29 合并优化）：本地菜单库
    menu_goods_cache 按 SPU 名 LIKE，展开每个 SPU 的 sku_index 为可选行，按 sku_id
    去重（跨门店同 sku 只出一行，附首个命中门店）。实时反馈：前端输入防抖后调本端点。"""
    like = f"%{keyword.strip()}%"
    rows = (db.query(MenuGoodsCache)
              .filter(MenuGoodsCache.spu_name.like(like),
                      MenuGoodsCache.status == 1)
              .order_by(MenuGoodsCache.fetched_at.desc())
              .limit(30).all())
    items, seen = [], set()
    for row in rows:
        try:
            sku_index = row.sku_index or {}
        except Exception:
            continue
        for sku_id, info in (sku_index or {}).items():
            if sku_id in seen or not isinstance(info, dict):
                continue
            seen.add(sku_id)
            specs = " / ".join(str(s.get("specOptionName") or s.get("specName") or "")
                               for s in (info.get("specs") or []) if isinstance(s, dict))
            items.append({
                "spu_id": row.spu_id, "spu_name": row.spu_name,
                "sku_id": str(sku_id), "spec_desc": specs or "默认",
                "price": str(info.get("price") or row.default_price or ""),
                "store_no": row.store_no,
            })
            if len(items) >= limit:
                return {"items": items}
    return {"items": items}


def _build_plan_priorities(plan: OrderPlan, tiers: list) -> None:
    seen_levels: set[int] = set()
    for t in (tiers or []):
        if t.level in seen_levels:
            raise HTTPException(400, f"优先级层级重复：第 {t.level} 优先出现多次")
        seen_levels.add(t.level)
        plan.priorities.append(OrderPlanCouponPriority(
            level=t.level, name=t.name or f"第 {_CN_ORDINAL.get(t.level, t.level)}优先",
            match_type=t.match_type, match_value=t.match_value, face_value=t.face_value or ""))


_CN_ORDINAL = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}


@router.get("/order-plans")
def order_plans_list(keyword: str = Query("", max_length=64),
                     enabled: bool | None = Query(None, description="启用状态过滤（缺省=全部）"),
                     page: int = Query(1, ge=1),
                     page_size: int = Query(100, ge=1, le=100),
                     db: Session = Depends(get_db),
                     _: SystemUser = Depends(require_perm("decision:manage"))):
    """方案列表（含优先级层级明细；decide/create 的工作台下拉也用此端点——
    缺省 page_size=100 保证全量下发）。过滤参数与套餐列表对齐（合并优化）。"""
    q = db.query(OrderPlan)
    if keyword:
        q = q.filter(OrderPlan.name.like(f"%{keyword}%"))
    if enabled is not None:
        q = q.filter(OrderPlan.enabled == enabled)
    total = q.count()
    rows = (q.order_by(OrderPlan.id)
              .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "items": [_plan_detail(p) for p in rows]}


@router.post("/order-plans")
def order_plan_create(body: OrderPlanRequest, request: Request,
                      db: Session = Depends(get_db),
                      user: SystemUser = Depends(require_perm("decision:manage"))):
    _check_plan_body(db, body)
    plan = OrderPlan(name=body.name.strip(), strategy=body.strategy,
                     drink_info=body.drink_info.strip(),
                     note=body.note or "", enabled=body.enabled)
    _build_plan_drinks(plan, body.drinks)
    _build_plan_priorities(plan, body.priorities)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    log_audit(db, request, user, "decision.order_plan_create", f"方案#{plan.id}",
              {"name": plan.name, "strategy": plan.strategy,
               "priorities": len(plan.priorities or []),
               "drinks": len(plan.drinks or [])})
    log_op(action="decision.order_plan_create", actor=user.username, target=f"方案#{plan.id}",
           params={"name": plan.name, "strategy": plan.strategy,
                   "priorities": len(plan.priorities or []),
                   "drinks": len(plan.drinks or [])})
    return _plan_detail(plan)


@router.put("/order-plans/{plan_id}")
def order_plan_update(plan_id: int, body: OrderPlanRequest, request: Request,
                      db: Session = Depends(get_db),
                      user: SystemUser = Depends(require_perm("decision:manage"))):
    plan = db.get(OrderPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    _check_plan_body(db, body, exclude_id=plan_id)
    plan.name = body.name.strip()
    plan.strategy = body.strategy
    plan.drink_info = body.drink_info.strip()
    plan.note = body.note or ""
    plan.enabled = body.enabled
    # 层级全量替换（clear 后先 flush 落 DELETE，规避同表 INSERT 先于 DELETE 撞唯一约束）
    plan.priorities.clear()
    plan.drinks.clear()
    db.flush()
    _build_plan_drinks(plan, body.drinks)
    _build_plan_priorities(plan, body.priorities)
    db.commit()
    db.refresh(plan)
    log_audit(db, request, user, "decision.order_plan_update", f"方案#{plan.id}",
              {"name": plan.name, "strategy": plan.strategy,
               "priorities": len(plan.priorities or []),
               "drinks": len(plan.drinks or [])})
    log_op(action="decision.order_plan_update", actor=user.username, target=f"方案#{plan.id}",
           params={"name": plan.name, "strategy": plan.strategy,
                   "priorities": len(plan.priorities or []),
                   "drinks": len(plan.drinks or [])})
    return _plan_detail(plan)


@router.post("/order-plans/{plan_id}/toggle-enabled")
def order_plan_toggle_enabled(plan_id: int, request: Request,
                              db: Session = Depends(get_db),
                              user: SystemUser = Depends(require_perm("decision:manage"))):
    """方案启用开关（与套餐 toggle-open 同构）：仅翻转 enabled，priorities/drinks
    原样保留——替代前端原「全量 PUT」翻转（payload 缺 drink_info 必填必 422，
    且漏 drinks 会清空饮品关联）。"""
    plan = db.get(OrderPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    plan.enabled = not bool(plan.enabled)
    db.commit()
    db.refresh(plan)
    log_audit(db, request, user, "decision.order_plan_toggle_enabled", f"方案#{plan.id}",
              {"enabled": plan.enabled})
    log_op(action="decision.order_plan_toggle_enabled", actor=user.username,
           target=f"方案#{plan.id}", params={"enabled": plan.enabled})
    return _plan_detail(plan)


@router.delete("/order-plans/{plan_id}")
def order_plan_delete(plan_id: int, request: Request, db: Session = Depends(get_db),
                      user: SystemUser = Depends(require_perm("decision:manage"))):
    plan = db.get(OrderPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "方案不存在")
    name = plan.name
    db.delete(plan)   # priorities / drinks 随 cascade 删除
    db.commit()
    log_audit(db, request, user, "decision.order_plan_delete", f"方案#{plan_id}",
              {"name": name})
    log_op(action="decision.order_plan_delete", actor=user.username,
           target=f"方案#{plan_id}", params={"name": name})
    return {"ok": True}


# ---------------- 盈利报表 / 决策流水 ----------------

def _parse_day(value: str, end_of_day: bool = False) -> datetime | None:
    """'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'（仪表盘 profitReport 发送格式）→ 起止 datetime；
    日期-only 时按日界（起 00:00:00 / 止 23:59:59），带时刻则尊重原文时刻；空返回 None，非法抛 400。"""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        d = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"日期参数格式须为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS: {value}")
    if len(value) > 10:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return d.replace(hour=23, minute=59, second=59) if end_of_day else d


@router.get("/profit-report")
def profit_report(
    from_: str = Query("", alias="from"),
    to: str = Query(""),
    packet_id: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: SystemUser = Depends(require_perm("decision:manage")),
):
    """decision_logs 聚合：成单行 = order_no 非空且 verdict=pass（收入/成本/利润均按
    该集合累计）；blocked_count 统计全部 blocked 评估（未成单留痕）。by_coupon_template
    按 decision_logs.coupon_code join coupon_records 的 template_name 归组。"""
    q = db.query(DecisionLog)
    dt_from = _parse_day(from_)
    if dt_from:
        q = q.filter(DecisionLog.created_at >= dt_from)
    dt_to = _parse_day(to, end_of_day=True)
    if dt_to:
        q = q.filter(DecisionLog.created_at <= dt_to)
    if packet_id:
        q = q.filter(DecisionLog.packet_id == packet_id)
    rows = q.all()

    orders = [r for r in rows if r.order_no and r.verdict == "pass"]
    revenue_total = sum((_to_dec(r.revenue) for r in orders), Decimal("0"))
    cost_total = sum((_to_dec(r.total_cost) for r in orders), Decimal("0"))
    profit_total = sum((_to_dec(r.profit) for r in orders), Decimal("0"))
    margins = [_to_dec(r.margin) for r in orders if str(r.margin or "").strip()]
    margin_avg = (sum(margins, Decimal("0")) / len(margins)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP) if margins else ""
    summary = {
        "orders": len(orders),
        "revenue_total": _money(revenue_total),
        "cost_total": _money(cost_total),
        "profit_total": _money(profit_total),
        "margin_avg": str(margin_avg),
        "blocked_count": sum(1 for r in rows if r.verdict == "blocked"),
    }

    by_packet: dict[int, dict] = {}
    for r in orders:
        if not r.packet_id:
            continue
        agg = by_packet.setdefault(r.packet_id, {"packet_id": r.packet_id,
                                                 "packet_name": r.packet_name,
                                                 "orders": 0, "revenue": Decimal("0"),
                                                 "cost": Decimal("0"), "profit": Decimal("0")})
        agg["orders"] += 1
        agg["revenue"] += _to_dec(r.revenue)
        agg["cost"] += _to_dec(r.total_cost)
        agg["profit"] += _to_dec(r.profit)
    by_packet_rows = [{
        "packet_id": v["packet_id"], "packet_name": v["packet_name"],
        "orders": v["orders"], "revenue": _money(v["revenue"]),
        "cost": _money(v["cost"]), "profit": _money(v["profit"]),
    } for v in sorted(by_packet.values(), key=lambda x: -x["orders"])]

    # 券模板归组：coupon_code → coupon_records.template_name（档案缺失回退空串归一组）
    codes = {r.coupon_code for r in orders if r.coupon_code}
    name_map = {c.coupon_code: c.template_name
                for c in db.query(CouponRecord)
                          .filter(CouponRecord.coupon_code.in_(codes)).all()} if codes else {}
    by_tpl: dict[str, dict] = {}
    for r in orders:
        if not r.coupon_code:
            continue
        tpl = name_map.get(r.coupon_code) or ""
        agg = by_tpl.setdefault(tpl, {"template_name": tpl, "uses": 0,
                                      "voucher_cost": Decimal("0"),
                                      "pay_cost": Decimal("0")})
        agg["uses"] += 1
        agg["voucher_cost"] += _to_dec(r.voucher_cost)
        agg["pay_cost"] += _to_dec(r.pay_cost)
    by_tpl_rows = [{
        "template_name": v["template_name"], "uses": v["uses"],
        "voucher_cost": _money(v["voucher_cost"]), "pay_cost": _money(v["pay_cost"]),
    } for v in sorted(by_tpl.values(), key=lambda x: -x["uses"])]

    return {"summary": summary, "by_packet": by_packet_rows, "by_coupon_template": by_tpl_rows}


@router.get("/logs")
def decision_logs(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    verdict: str = Query("", pattern="^(pass|blocked)?$"),
    packet_id: int = Query(0, ge=0),
    order_no: str = Query("", max_length=64),
    db: Session = Depends(get_db),
    _: SystemUser = Depends(require_perm("decision:manage")),
):
    q = db.query(DecisionLog)
    if verdict:
        q = q.filter(DecisionLog.verdict == verdict)
    if packet_id:
        q = q.filter(DecisionLog.packet_id == packet_id)
    if order_no:
        q = q.filter(DecisionLog.order_no == order_no)
    total = q.count()
    rows = (q.order_by(DecisionLog.id.desc())
              .offset((page - 1) * page_size).limit(page_size).all())
    return {"total": total, "items": [_log_row(r) for r in rows]}


# ---------------- decide 决策评估（POST /api/ops/orders/decide） ----------------
# 套餐 item 券规则过滤统一走 services.decision.rule_satisfied（2026-09-29 合并优化：
# 原本文件内 _item_rule_hit 与 resolve_cost/_match_hit 重复实现四类匹配+面额校验）

def _settle_probe_total(account: ChageeAccount, target: dict,
                        username: str) -> Decimal | None:
    """deep 探针：对候选账号真实 settle_direct(no_recommend=True) 取服务端订单总额
    （无券原价口径）；任何失败返回 None（调用方降级 menu 估值），不中断决策。"""
    try:
        api = bridge.trade_api(account)
        price = api.calculate_price(target)
        settle = api.settle_direct(target, price, no_recommend=True)
        return _to_dec(settle.total_trade_price)
    except Exception as e:
        log_op(level="WARN", action="decision.settle_probe", actor=username,
               target=f"{account.label}#{account.id}", result="failed", error=e)
        return None


@global_router.post("/orders/decide")
def orders_decide(body: DecideRequest, request: Request,
                  db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("feature:order"))):
    started = time.time()   # oplog 耗时统计
    # ① 商品解析：本地菜单规格库 skuId 命中（未命中 422 带原因）；文案规格非空时再解析
    try:
        hit = menu_spec_service.resolve_by_sku(db, body.store_no, body.sku_id)
        spec_res = None
        texts = [str(t).strip() for t in body.spec_list if str(t).strip()]
        if texts:
            spec_res = menu_spec_service.resolve_spec_texts(
                db, body.store_no, hit["spu_id"], texts)
    except SpecResolveError as e:
        raise HTTPException(422, {"message": str(e), "candidates": e.candidates})
    # ② revenue = 客户支付价（必填，Decimal 语义校验）
    revenue = _to_dec_or_none(body.customer_price)
    if revenue is None or revenue < 0:
        raise HTTPException(422, f"customer_price 不是合法金额: {body.customer_price!r}")
    quantity = int(body.quantity or 1)
    unit = _to_dec(hit.get("price"))
    # spec_list/attribute_list：文案解析结果优先，未传文案时用 sku_index 的默认组合
    spec_list = ((spec_res or {}).get("spec_list")
                 or [dict(s) for s in (hit.get("specs") or [])])
    attribute_list = (spec_res or {}).get("attribute_list") or []

    # ③ 套餐命中：价格区间 + 时段 + 商品圈定；packet_id 指定时校验其在命中集内。
    #    方案与套餐不做绑定（§13 绑定功能已移除），两者正交：套餐管接单范围，方案管选券策略
    packets = db.query(PacketConfig).order_by(PacketConfig.id).all()
    matched = decision_svc.match_packets(packets, body.sku_id, revenue, datetime.now())
    packet = None
    if body.packet_id:
        packet = next((p for p in matched if p.id == body.packet_id), None)
        if packet is None:
            raise HTTPException(422, f"指定套餐 #{body.packet_id} 未命中当前单"
                                     f"（价格区间/可用时段/商品圈定不符，命中集: "
                                     f"{[p.id for p in matched] or '空'}）")
    elif matched:
        packet = matched[0]
    item = None
    if packet is not None:
        item = next((it for it in (packet.items or [])
                     if str(it.sku_id) == str(body.sku_id)), None)

    # ④ total 估算：menu_goods_cache 价×数量（price_source=menu）
    total = unit * quantity
    price_source = "menu"

    # ⑤ 券候选：在线账号 + 未使用 + 两桶可用 + 有效期/门槛初筛
    rules = db.query(VoucherCostRule).all()
    categories = db.query(VoucherCostCategory).all()
    cfg = decision_svc.load_config()
    now_ms = int(time.time() * 1000)
    cand_q = (db.query(CouponRecord)
                .join(ChageeAccount, CouponRecord.account_id == ChageeAccount.id)
                .filter(CouponRecord.bucket.in_(("effective", "settle_available")),
                        ChageeAccount.status == "online",
                        CouponRecord.last_order_no == ""))
    candidates = []
    for r in cand_q.order_by(CouponRecord.id).all():
        usable_flag, _reason = _screen_usable(r, now_ms)
        if not usable_flag:
            continue
        threshold = _parse_threshold(r.threshold_tips)
        if threshold is not None and total > 0 and threshold > total:
            continue   # 门槛高于估算总额，本单必然不满足
        cls = decision_svc.classify_coupon(r.benefit_text, r.benefit2_text,
                                           r.template_name, r.biz_type)
        cost_res = decision_svc.resolve_cost(rules, r, cfg)
        candidates.append({"record": r, "cost": cost_res["cost"],
                           "source": cost_res["source"],
                           "kind": cls["kind"], "face": cls["face"], "rate": cls["rate"],
                           "use_end_time": r.use_end_time})   # 临期排序因子（§10 第③级）

    # ⑥ 套餐 item 券规则过滤：is_premium 商品只留 premium 规则命中者，规则 null=不限
    #    （判定统一走 decision_svc.rule_satisfied，与成本规则/子类/优先级层同一实现）
    if item is not None:
        rule = item.premium_coupon_rule if item.is_premium else item.normal_coupon_rule
        if rule:
            candidates = [c for c in candidates
                          if decision_svc.rule_satisfied(rule, c["record"])]

    # 阈值来源：套餐级 min_profit / max_order_cost 任一非空即视为套餐覆盖（source=packet）；
    # min_margin 恒取全局
    threshold_json = {"min_profit": "", "min_margin": str(cfg.get("min_margin") or ""),
                      "max_order_cost": "", "source": "global"}
    packet_override = False
    if packet is not None and str(packet.min_profit or "").strip():
        threshold_json["min_profit"] = str(packet.min_profit).strip()
        packet_override = True
    else:
        threshold_json["min_profit"] = str(cfg.get("min_profit") or "")
    if packet is not None and str(getattr(packet, "max_order_cost", "") or "").strip():
        threshold_json["max_order_cost"] = str(packet.max_order_cost).strip()
        packet_override = True
    else:
        threshold_json["max_order_cost"] = str(cfg.get("max_order_cost") or "")
    if packet_override:
        threshold_json["source"] = "packet"
    overhead = str(cfg.get("overhead") or "0")

    # 下单方案（§11）：策略 + 券优先级层级；plan_id=0 = 自动（保持四级漏斗默认行为）
    plan = None
    if body.plan_id:
        plan = db.get(OrderPlan, body.plan_id)
        if plan is None:
            raise HTTPException(422, f"下单方案不存在：{body.plan_id}")
        # 方案饮品白名单（§11 饮品管理 Tab）：关联了饮品时仅可下单这些饮品（空=不限）
        drink_skus = {str(d.sku_id) for d in (plan.drinks or [])}
        if drink_skus and str(body.sku_id) not in drink_skus:
            raise HTTPException(422, f"方案「{plan.name}」未关联此饮品（可在方案的饮品管理 Tab 维护关联），"
                                     f"该方案仅可下单已关联的 {len(drink_skus)} 种饮品")
    tier_list = list(plan.priorities or []) if plan is not None else []
    strategy = plan.strategy if plan is not None else "cost_first"

    def _rank(t: Decimal) -> list[dict]:
        return decision_svc.rank_candidates(candidates, _money(revenue), _money(t),
                                            overhead, threshold_json["min_profit"],
                                            threshold_json["min_margin"],
                                            threshold_json["max_order_cost"],
                                            strategy=strategy)

    def _rank_with_tiers(t: Decimal) -> tuple[list[dict], dict]:
        rk = _rank(t)
        if not tier_list:
            return rk, {}
        return decision_svc.apply_priority_tiers(rk, tier_list)

    ranked, tier_map = _rank_with_tiers(total)
    # deep=true：对 top1 候选账号真实 settle 探针取服务端总额后重排（失败降级 menu 估值）
    if body.deep and ranked:
        probe_account = db.get(ChageeAccount, ranked[0]["record"].account_id)
        target = {
            "storeNo": body.store_no, "spuId": hit["spu_id"], "spuName": hit["spu_name"],
            "skuId": body.sku_id, "skuName": hit["spu_name"],
            "itemSkuId": hit.get("item_sku_id") or "", "quantity": quantity,
            "salePrice": float(unit), "specList": spec_list,
            "attributeList": attribute_list, "imageUrl": "",
            "spuType": "stand", "nutritionInfo": None,
        }
        server_total = (_settle_probe_total(probe_account, target, user.username)
                        if probe_account else None)
        if server_total is not None:
            total = server_total
            price_source = "settle"
            ranked, tier_map = _rank_with_tiers(total)

    # 判定：pass 候选优先；无 pass 时按 allow_full_price 决定原价单或 blocked
    top = ranked[0] if ranked else None
    verdict, blocked_reason, chosen, breakdown = "pass", "", None, None

    def _tier_names() -> str:
        return "、".join(f"第{_CN_ORDINAL.get(t.level, t.level)}优先「{t.name}」"
                         for t in tier_list) or "未设置优先级层级"

    def _full_price_breakdown() -> dict:
        b = decision_svc.evaluate_cost(revenue, total, "0", "0", overhead)
        b["deduction_estimated"] = False
        b["cost_source"] = "none"
        b["coupon_kind"] = ""
        return b

    if top is not None:
        chosen, breakdown = top["record"], top["cost_breakdown"]
    elif body.allow_full_price:
        b = _full_price_breakdown()
        v, reason = decision_svc.check_threshold(b, threshold_json["min_profit"],
                                                 threshold_json["min_margin"],
                                                 threshold_json["max_order_cost"])
        if v == "pass":
            breakdown = b
        else:
            verdict, blocked_reason, breakdown = "blocked", reason, b
    elif candidates:
        # 有候选但全部被阈值拦下：取成本最低候选的明细作为“差距参考”并说明原因
        best = None
        for c in candidates:
            ded, _est = decision_svc.estimate_deduction(c["kind"], c["face"], c["rate"], total)
            b = decision_svc.evaluate_cost(revenue, total, ded, c["cost"], overhead)
            b["deduction_estimated"] = _est
            b["cost_source"] = c["source"]
            b["coupon_kind"] = c["kind"]
            v, reason = decision_svc.check_threshold(b, threshold_json["min_profit"],
                                                     threshold_json["min_margin"],
                                                     threshold_json["max_order_cost"])
            if best is None or _to_dec(b["total_cost"]) < _to_dec(best[2]["total_cost"]):
                best = (c["record"], (v, reason), b)
        chosen, (_v, reason), breakdown = best
        verdict = "blocked"
        blocked_reason = f"最佳券候选未过阈值（{reason}），且 allow_full_price=false 不允许原价单"
        if plan is not None:   # §11：指定方案时，所有优先级券均失败 → 暂无库存话术
            blocked_reason = (f"暂无库存：方案「{plan.name}」各优先级券（{_tier_names()}）"
                              f"均不可用或未过阈值（{reason}）")
    else:
        verdict = "blocked"
        blocked_reason = "无可用券候选（在线账号无未使用的有效券），且 allow_full_price=false 不允许原价单"
        if plan is not None:
            blocked_reason = (f"暂无库存：方案「{plan.name}」的各优先级券"
                              f"（{_tier_names()}）均不可用，且 allow_full_price=false")
        breakdown = _full_price_breakdown()
    breakdown["price_source"] = price_source
    # 券成本子类（业务分类层）：选中券的归类名（无券 ""）；子类只做分类，
    # 成本金额仍由 resolve_cost（voucher_cost_rules）唯一决定，不影响上面的成本链
    breakdown["cost_category_name"] = (
        decision_svc.classify_category(categories, chosen)["category_name"]
        if chosen is not None else "")

    # 推荐账号：选券候选所在账号；原价单取首个在线账号作建议
    account_id, account_label = 0, ""
    if chosen is not None:
        acc = db.get(ChageeAccount, chosen.account_id)
        account_id = chosen.account_id
        account_label = f"{acc.label}#{acc.id}" if acc else ""
    elif verdict == "pass":
        first = (db.query(ChageeAccount).filter(ChageeAccount.status == "online")
                   .order_by(ChageeAccount.id).first())
        if first:
            account_id, account_label = first.id, f"{first.label}#{first.id}"

    coupon = None
    if chosen is not None:
        coupon = {
            "coupon_code": chosen.coupon_code, "template_name": chosen.template_name,
            "benefit_text": chosen.benefit_text, "amount": chosen.amount,
            "coupon_kind": breakdown.get("coupon_kind") or "",
        }

    alternatives = []
    for alt in ranked[1:1 + ALTERNATIVES_LIMIT]:
        rec = alt["record"]
        acc = db.get(ChageeAccount, rec.account_id)
        tier_info = tier_map.get(rec.coupon_code) or {}
        alternatives.append({
            "account_id": rec.account_id,
            "account_label": f"{acc.label}#{acc.id}" if acc else "",
            "coupon_code": rec.coupon_code, "template_name": rec.template_name,
            "total_cost": alt["cost_breakdown"]["total_cost"],
            "profit": alt["cost_breakdown"]["profit"],
            "use_end_time": rec.use_end_time,
            "tier_level": tier_info.get("level", 0),       # 0=不匹配任何优先级层
            "tier_name": tier_info.get("name", ""),
        })

    # settle 直发预填（字段名对齐 OrderSettleRequest；skuName 缺省回退 SPU 名）
    settle_prefill = {
        "spu_id": hit["spu_id"], "spu_name": hit["spu_name"],
        "sku_id": body.sku_id, "sku_name": hit["spu_name"],
        "item_sku_id": hit.get("item_sku_id") or "",
        "sale_price": float(unit), "quantity": quantity,
        "spec_list": spec_list, "attribute_list": attribute_list,
        "extra_list": [], "spu_type": "stand",
    }

    # DecisionLog 落库（order_no 空=decide 评估；blocked 也写；成单后凭 id 回填）
    log_row = DecisionLog(
        order_no="",
        packet_id=packet.id if packet is not None else 0,
        packet_name=packet.name if packet is not None else "",
        account_id=account_id,
        coupon_code=chosen.coupon_code if chosen is not None else "",
        revenue=_money(revenue), total_trade_price=_money(total),
        voucher_cost=breakdown.get("voucher_cost", ""),
        pay_cost=breakdown.get("pay_cost", ""),
        overhead=breakdown.get("overhead", ""),
        total_cost=breakdown.get("total_cost", ""),
        profit=breakdown.get("profit", ""),
        margin=breakdown.get("margin", ""),
        verdict=verdict, blocked_reason=blocked_reason[:255],
        threshold_json=threshold_json,
        plan_json={
            "cost_breakdown": breakdown, "coupon": coupon,
            "alternatives": alternatives, "price_source": price_source,
            "store_no": body.store_no, "sku_id": body.sku_id, "quantity": quantity,
            "packet_matched_ids": [p.id for p in matched],
            # 下单方案快照（§11）：create 自动切换据此按方案优先级链降级
            "plan_id": plan.id if plan is not None else 0,
            "plan_name": plan.name if plan is not None else "",
            "strategy": strategy,
        },
    )
    db.add(log_row)
    db.commit()

    response = {
        "verdict": verdict,
        "blocked_reason": blocked_reason,
        "packet": _packet_summary(packet, len(packet.items or [])) if packet is not None else None,
        "item": _packet_item_row(item) if item is not None else None,
        "account_id": account_id, "account_label": account_label,
        "coupon": coupon,
        "cost_breakdown": breakdown,
        "threshold": threshold_json,
        "plan": {"plan_id": plan.id, "plan_name": plan.name, "strategy": strategy,
                 "strategy_label": PLAN_STRATEGY_LABELS.get(strategy, strategy)}
                if plan is not None else None,
        "alternatives": alternatives,
        "settle_prefill": settle_prefill,
        "decision_log_id": log_row.id,
    }
    log_audit(db, request, user, "feature.order_decide",
              f"{body.store_no}/{body.sku_id}",
              {"verdict": verdict, "packet": packet.name if packet is not None else None,
               "account_id": account_id, "coupon": coupon["coupon_code"] if coupon else None,
               "profit": breakdown.get("profit"), "margin": breakdown.get("margin"),
               "price_source": price_source})
    log_op(action="feature.order_decide", actor=user.username,
           target=f"{body.store_no}/{body.sku_id}",
           params={"verdict": verdict, "revenue": _money(revenue),
                   "total": _money(total), "price_source": price_source,
                   "profit": breakdown.get("profit"), "coupon": coupon["coupon_code"] if coupon else None,
                   "decision_log_id": log_row.id, "deep": body.deep},
           duration_ms=int((time.time() - started) * 1000))
    return response
