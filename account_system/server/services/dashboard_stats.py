"""仪表盘统计聚合（2026-09-28 自 routers/audit.py 抽出，逻辑不变）。

同一聚合供两路消费：
- REST 端点 GET /api/dashboard/stats（首次拉取 + SSE 不可用时的兜底轮询）；
- services/dashboard_push.py 推送线程（指纹变化经 events_bus 推给 SSE 订阅端），
保证「统计与仪表盘数据联动、实时同步」的双通道同源。
"""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import (AuditLog, ChageeAccount, CouponRecord, CouponUsageLog,
                    OrderRecord, STATUS_LABELS, SystemUser)

# 近 N 日趋势窗口（含当日）
TREND_DAYS = 7


def _fmt_money(v) -> str:
    try:
        return f"{Decimal(str(v)).quantize(Decimal('0.01'))}"
    except Exception:
        return str(v)


def _day_series(days: int = TREND_DAYS) -> list[str]:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return [(today - timedelta(days=i)).strftime("%m-%d") for i in range(days - 1, -1, -1)]


def collect_dashboard_stats(db: Session) -> dict:
    """仪表盘统一数据源：账号域 / 券域 / 订单域 / 审计域 每次调用实时聚合。"""
    # ---- 账号域 ----
    by_status = dict(db.query(ChageeAccount.status, func.count(ChageeAccount.id)).group_by(ChageeAccount.status).all())
    total_accounts = sum(by_status.values())
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    trend_start = today - timedelta(days=TREND_DAYS - 1)
    recent_audit = (db.query(AuditLog).order_by(AuditLog.id.desc()).limit(8).all())
    logins_7d = (db.query(AuditLog)
                   .filter(AuditLog.action.in_(["account.login", "auth.login"]),
                           AuditLog.created_at >= trend_start)
                   .count())

    # ---- 券域（券档案 = 全量同步收集到的优惠券 ID 集合）----
    by_bucket = dict(db.query(CouponRecord.bucket, func.count(CouponRecord.id)).group_by(CouponRecord.bucket).all())
    coupons_total = sum(by_bucket.values())
    coupons_used = db.query(CouponRecord).filter(CouponRecord.last_order_no != "").count()
    usage_by_result = dict(db.query(CouponUsageLog.result, func.count(CouponUsageLog.id))
                           .group_by(CouponUsageLog.result).all())
    deduction_total = Decimal("0")
    for (d,) in (db.query(CouponUsageLog.deduction)
                 .filter(CouponUsageLog.result == "success").all()):
        try:
            deduction_total += Decimal(str(d or "0"))
        except Exception:
            pass
    # 使用范围分布（自取/外卖/团餐自提按 LIKE 命中统计）
    scenes = [{"scene": s, "value": db.query(CouponRecord)
               .filter(CouponRecord.usable_scenes.like(f"%{s}%")).count()}
              for s in ("自取", "外卖", "团餐")]

    # ---- 券使用趋势（近 N 日：成功/被拒/失败 + 抵扣金额）----
    day_keys = _day_series()
    usage_rows = (db.query(func.strftime("%m-%d", CouponUsageLog.used_at),
                           CouponUsageLog.result, func.count(CouponUsageLog.id))
                  .filter(CouponUsageLog.used_at >= trend_start)
                  .group_by(func.strftime("%m-%d", CouponUsageLog.used_at), CouponUsageLog.result).all())
    deduction_rows = (db.query(func.strftime("%m-%d", CouponUsageLog.used_at), CouponUsageLog.deduction)
                      .filter(CouponUsageLog.used_at >= trend_start,
                              CouponUsageLog.result == "success").all())
    usage_map: dict[str, dict] = {k: {"success": 0, "rejected": 0, "failed": 0} for k in day_keys}
    for day, result, cnt in usage_rows:
        if day in usage_map and result in usage_map[day]:
            usage_map[day][result] = cnt
    deduction_map: dict[str, Decimal] = {k: Decimal("0") for k in day_keys}
    for day, d in deduction_rows:
        if day in deduction_map:
            try:
                deduction_map[day] += Decimal(str(d or "0"))
            except Exception:
                pass

    # ---- 订单域（状态分布 + 近 N 日下单趋势）----
    order_by_status = dict(db.query(OrderRecord.status, func.count(OrderRecord.id))
                           .group_by(OrderRecord.status).all())
    orders_total = sum(order_by_status.values())
    order_rows = (db.query(func.strftime("%m-%d", OrderRecord.created_at), func.count(OrderRecord.id))
                  .filter(OrderRecord.created_at >= trend_start)
                  .group_by(func.strftime("%m-%d", OrderRecord.created_at)).all())
    orders_map = {k: 0 for k in day_keys}
    for day, cnt in order_rows:
        if day in orders_map:
            orders_map[day] = cnt
    scenario_by = dict(db.query(OrderRecord.scenario, func.count(OrderRecord.id))
                       .group_by(OrderRecord.scenario).all())

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cards": {
            # 账号域
            "total_accounts": total_accounts,
            "online": by_status.get("online", 0),
            "pending": by_status.get("pending", 0),
            "expired": by_status.get("expired", 0),
            "disabled": by_status.get("disabled", 0),
            "logins_7d": logins_7d,
            "users": db.query(SystemUser).count(),
            # 券域（与优惠券全量查询/模糊搜索联动）
            "coupons_total": coupons_total,
            "coupons_effective": by_bucket.get("effective", 0),
            "coupons_historical": by_bucket.get("historical", 0),
            "coupons_settle_available": by_bucket.get("settle_available", 0),
            "coupons_used": coupons_used,
            "coupon_used_success": usage_by_result.get("success", 0),
            "coupon_rejected": usage_by_result.get("rejected", 0),
            "coupon_failed": usage_by_result.get("failed", 0),
            "coupon_deduction_total": _fmt_money(deduction_total),
            # 订单域（与下单工作台/取餐查询联动）
            "orders_total": orders_total,
            "orders_today": orders_map.get(day_keys[-1], 0),
            "orders_pending_pay": order_by_status.get(1, 0),
            "orders_making": order_by_status.get(3, 0),
            "orders_done": order_by_status.get(6, 0),
            "orders_canceled": order_by_status.get(7, 0),
            "orders_zero": scenario_by.get("zero", 0),
            "orders_partial": scenario_by.get("partial", 0),
        },
        "status_distribution": [
            {"status": s, "label": STATUS_LABELS[s], "value": by_status.get(s, 0)}
            for s in ("online", "pending", "expired", "disabled")
        ],
        "coupon_scenes": [s for s in scenes if s["value"] > 0],
        "coupon_usage_trend": [
            {"day": k, "success": v["success"], "rejected": v["rejected"] + v["failed"],
             "deduction": _fmt_money(deduction_map[k])}
            for k, v in usage_map.items()
        ],
        "orders_trend": [{"day": k, "count": v} for k, v in orders_map.items()],
        "recent_audit": [
            {"id": r.id, "username": r.username, "action": r.action, "target": r.target, "created_at": r.created_at}
            for r in recent_audit
        ],
    }
