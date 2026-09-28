"""下单决策域路由（波2-C，契约 docs/decision_api_contract.md §5/§6）。

四组职责（金额字段全 string，时间 YYYY-MM-DD HH:MM:SS，列表 {total, items}）：
  套餐/成本规则/配置管理 —— PacketConfig（items 级联 delete-orphan 全量替换）、
    VoucherCostRule（四类 match_type）、data/decision_config.json 读写；
  扫描与库存 —— POST /scan 复用 ops.coupons_sync_all 的全账号遍历语义
    （status!=disabled 且 token!=""，单账号失败不中断）拉券入 coupon_records，
    再对全库 effective/settle_available 券跑 resolve_cost 盘点；GET /coupon-inventory
    每张券带分型（classify_coupon）、成本（resolve_cost）与本地可用性判定；
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
from models import (ChageeAccount, CouponRecord, DecisionLog, PacketConfig, PacketItem,
                    SystemUser, VoucherCostRule)
from oplog import log_op
from schemas import (CostRuleImportRequest, CostRuleRequest, DecideRequest,
                     DecisionConfigRequest, PacketCreateRequest, ScanRequest)
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
        "min_profit": p.min_profit, "note": p.note,
        "item_count": int(item_count or 0),
        "created_at": _fmt_dt(p.created_at), "updated_at": _fmt_dt(p.updated_at),
    }


def _packet_detail(p: PacketConfig) -> dict:
    detail = _packet_summary(p, len(p.items or []))
    detail["items"] = [_packet_item_row(it) for it in (p.items or [])]
    return detail


def _cost_rule_row(r: VoucherCostRule) -> dict:
    return {
        "id": r.id, "name": r.name, "match_type": r.match_type,
        "match_value": r.match_value, "face_value": r.face_value,
        "cost_price": r.cost_price, "priority": r.priority,
        "enabled": bool(r.enabled), "note": r.note,
        "created_at": _fmt_dt(r.created_at), "updated_at": _fmt_dt(r.updated_at),
    }


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


@router.get("/cost-rules")
def cost_rules_list(db: Session = Depends(get_db),
                    _: SystemUser = Depends(require_perm("decision:manage"))):
    rows = (db.query(VoucherCostRule)
              .order_by(VoucherCostRule.priority.asc(), VoucherCostRule.id.asc()).all())
    return {"items": [_cost_rule_row(r) for r in rows]}


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
              {"name": rule.name, "match_type": rule.match_type, "cost_price": rule.cost_price})
    log_op(action="decision.cost_rule_create", actor=user.username, target=f"规则#{rule.id}",
           params={"name": rule.name, "cost_price": rule.cost_price})
    return _cost_rule_row(rule)


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
              {"name": rule.name, "cost_price": rule.cost_price})
    log_op(action="decision.cost_rule_update", actor=user.username, target=f"规则#{rule.id}",
           params={"name": rule.name, "cost_price": rule.cost_price})
    return _cost_rule_row(rule)


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
    """批量导入：逐条校验（非法条目进 errors 不中断），name 与库内重复跳过。"""
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


# ---------------- 券库存扫描与盘点 ----------------

@router.post("/scan")
def decision_scan(body: ScanRequest, request: Request, db: Session = Depends(get_db),
                  user: SystemUser = Depends(require_perm("decision:manage"))):
    """券库存扫描：复用 ops.coupons_sync_all 的全账号遍历语义（status!=disabled 且
    token!=""，account_ids 可圈定范围；单账号凭证失效/协议异常不中断整体），拉取
    effective/historical 两桶券入 coupon_records 后，对全库 effective/settle_available
    券跑 resolve_cost 成本盘点汇总。"""
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

    # 成本盘点：全库两桶可用券逐张 resolve_cost（规则匹配链 + fallback）
    rules = db.query(VoucherCostRule).all()
    cfg = decision_svc.load_config()
    coupons = (db.query(CouponRecord)
                 .filter(CouponRecord.bucket.in_(("effective", "settle_available"))).all())
    coupons_total = len(coupons)
    with_cost = unknown_cost = 0
    total_face = Decimal("0")
    total_cost = Decimal("0")
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
    summary = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "accounts_scanned": len(accounts), "accounts_ok": ok,
        "accounts_expired": expired, "accounts_failed": failed,
        "coupons_total": coupons_total, "coupons_with_cost": with_cost,
        "coupons_unknown_cost": unknown_cost,
        "total_face": _money(total_face), "total_cost_estimate": _money(total_cost),
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
    """券库存：coupon_records 每张带分型（classify_coupon）、成本（resolve_cost）与
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
    cfg = decision_svc.load_config()
    now_ms = int(time.time() * 1000)
    items = []
    for r in rows:
        kind = decision_svc.classify_coupon(r.benefit_text, r.benefit2_text,
                                            r.template_name, r.biz_type)
        cost = decision_svc.resolve_cost(rules, r, cfg)
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

def _item_rule_hit(rule: dict | None, record: CouponRecord) -> bool:
    """套餐 item 券规则过滤（匹配语义与 resolve_cost 一致）：rule 为 null=不限；
    match_type 四类（template_exact/template_contains/benefit_regex/coupon_prefix），
    face_value 非空须等于券面额；任何异常（regex 非法等）视为不命中。"""
    if not rule:
        return True
    try:
        match_type = str(rule.get("match_type") or "")
        match_value = str(rule.get("match_value") or "")
        if not match_value:
            return True
        template_name = str(record.template_name or "")
        if match_type == "template_exact":
            hit = template_name == match_value
        elif match_type == "template_contains":
            hit = match_value in template_name
        elif match_type == "benefit_regex":
            hit = bool(re.search(match_value, f"{template_name} {record.benefit_text or ''}"))
        elif match_type == "coupon_prefix":
            hit = str(record.coupon_code or "").startswith(match_value)
        else:
            hit = False
        if not hit:
            return False
        face_check = str(rule.get("face_value") or "").strip()
        if face_check:
            face = _to_dec_or_none(record.amount)
            if face is None or _to_dec(face_check) != face:
                return False
        return True
    except Exception:
        return False


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

    # ③ 套餐命中：价格区间 + 时段 + 商品圈定；packet_id 指定时校验其在命中集内
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
                           "kind": cls["kind"], "face": cls["face"], "rate": cls["rate"]})

    # ⑥ 套餐 item 券规则过滤：is_premium 商品只留 premium 规则命中者，规则 null=不限
    if item is not None:
        rule = item.premium_coupon_rule if item.is_premium else item.normal_coupon_rule
        if rule:
            candidates = [c for c in candidates if _item_rule_hit(rule, c["record"])]

    # 阈值来源：套餐级 min_profit 覆盖全局；min_margin 恒取全局
    threshold_json = {"min_profit": "", "min_margin": str(cfg.get("min_margin") or ""),
                      "source": "global"}
    if packet is not None and str(packet.min_profit or "").strip():
        threshold_json["min_profit"] = str(packet.min_profit).strip()
        threshold_json["source"] = "packet"
    else:
        threshold_json["min_profit"] = str(cfg.get("min_profit") or "")
    overhead = str(cfg.get("overhead") or "0")

    def _rank(t: Decimal) -> list[dict]:
        return decision_svc.rank_candidates(candidates, _money(revenue), _money(t),
                                            overhead, threshold_json["min_profit"],
                                            threshold_json["min_margin"])

    ranked = _rank(total)
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
            ranked = _rank(total)

    # 判定：pass 候选优先；无 pass 时按 allow_full_price 决定原价单或 blocked
    top = ranked[0] if ranked else None
    verdict, blocked_reason, chosen, breakdown = "pass", "", None, None

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
                                                 threshold_json["min_margin"])
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
                                                     threshold_json["min_margin"])
            if best is None or _to_dec(b["total_cost"]) < _to_dec(best[2]["total_cost"]):
                best = (c["record"], (v, reason), b)
        chosen, (_v, reason), breakdown = best
        verdict = "blocked"
        blocked_reason = f"最佳券候选未过阈值（{reason}），且 allow_full_price=false 不允许原价单"
    else:
        verdict = "blocked"
        blocked_reason = "无可用券候选（在线账号无未使用的有效券），且 allow_full_price=false 不允许原价单"
        breakdown = _full_price_breakdown()
    breakdown["price_source"] = price_source

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
        alternatives.append({
            "account_id": rec.account_id,
            "account_label": f"{acc.label}#{acc.id}" if acc else "",
            "coupon_code": rec.coupon_code, "template_name": rec.template_name,
            "total_cost": alt["cost_breakdown"]["total_cost"],
            "profit": alt["cost_breakdown"]["profit"],
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
