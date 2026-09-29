"""初始化：建表 + 内置角色 + 默认管理员（admin / Admin@123，首登后请修改）。"""

import sys

from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine
from models import Role, SystemUser
from permissions import BUILTIN_ROLES
from security import hash_password

DEFAULT_ADMIN = {"username": "admin", "password": "Admin@123", "display_name": "系统管理员"}

# 轻量列迁移（SQLite ALTER TABLE ADD COLUMN）：模型加列后旧库平滑升级，缺哪列补哪列
_COLUMN_MIGRATIONS = [
    ("coupon_records", "usable_scenes", "VARCHAR(64) DEFAULT '' NOT NULL"),
    # H5 收银台：下单时的 settle target 快照（switch-full-price 原价重下依据）
    ("order_records", "order_target", "JSON"),
    # 支付宝官方 H5 收银台 URL 快照（2026-09-27 新增，旧 pay_sessions 表补列）
    ("pay_sessions", "alipay_cashier_url", "VARCHAR(512) DEFAULT '' NOT NULL"),
    # 全量取餐码扫描（2026-09-28）：官方 orderTime 原文 + 履约方式（businessTypeText）
    ("order_records", "order_time", "VARCHAR(32) DEFAULT '' NOT NULL"),
    ("order_records", "biz_type", "VARCHAR(16) DEFAULT '' NOT NULL"),
    # 券成本子类（2026-09-28）：成本规则软关联子类（0=未分类，子类删除时回退 0）
    ("voucher_cost_rules", "category_id", "INTEGER DEFAULT 0 NOT NULL"),
    # 最大承受下单金额（2026-09-29 §10）：套餐级成本上限覆盖
    ("packet_configs", "max_order_cost", "VARCHAR(16) DEFAULT '' NOT NULL"),
    # 下单方案饮品信息（2026-09-29 §11）：方案级必填编辑框，下单时自动带入
    ("order_plans", "drink_info", "VARCHAR(255) DEFAULT '' NOT NULL"),
    # 下单方案支付金额上限（2026-09-29）：方案级差额实付上限（空=未配置，fail-closed 拒单）
    ("order_plans", "max_pay_amount", "VARCHAR(32) DEFAULT '' NOT NULL"),
    # 券使用日志状态机（2026-09-30 §18）：状态流转轨迹 JSON
    ("coupon_usage_logs", "state_history", "TEXT DEFAULT '' NOT NULL"),
]


def _migrate_columns() -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            if table not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if column not in existing:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                    print(f"[seed] 迁移：{table} 补列 {column}")
                except Exception as e:   # 异常安全：单列迁移失败不阻断启动（缺列的旧库仍可读写旧行）
                    print(f"[seed] 迁移失败（忽略）：{table}.{column}: {e}")


def _hist_append(log, from_state: str, to_state: str, by: str, reason: str = "") -> None:
    """state_history 追加一条流转（自包含实现，避免 seed 反向依赖 services 层）。"""
    import json as _json
    from datetime import datetime as _dt
    try:
        hist = _json.loads(log.state_history or "[]")
        if not isinstance(hist, list):
            hist = []
    except Exception:
        hist = []
    hist.append({"at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "from": from_state, "to": to_state,
                 "by": by or "seed-migration", "reason": (reason or "")[:200]})
    log.state_history = _json.dumps(hist, ensure_ascii=False)


def _migrate_usage_log_lifecycle() -> None:
    """券使用日志状态机数据回填（§18，幂等，启动时执行）。

    修正两类历史脏数据（旧「乐观预记 success + 回滚补行」形态造成的分类失真）：
      ① 旧双行形态：同券同单同时存在 success 行与 rolled_back 行 → 合并为单行
         rolled_back（金额快照保留在原 success 行，历史并入 state_history，删除重复行）；
      ② 残留 success 行按订单实况校正：order.status=7（已取消）→ rolled_back；
         order.status=1（仍待支付）→ pending。订单缺失/已支付(3/6) 不动（零元单 born 3 合法）。
    """
    from models import CouponUsageLog, OrderRecord

    with SessionLocal() as db:
        # ① 合并旧双行（success + rolled_back 同券同单）
        merged = 0
        pairs = (db.query(CouponUsageLog.coupon_code, CouponUsageLog.order_no)
                   .filter(CouponUsageLog.result.in_(("success", "rolled_back")),
                           CouponUsageLog.order_no != "")
                   .distinct().all())
        for code, order_no in pairs:
            rows = (db.query(CouponUsageLog)
                      .filter(CouponUsageLog.coupon_code == code,
                              CouponUsageLog.order_no == order_no)
                      .order_by(CouponUsageLog.id).all())
            succ = [r for r in rows if r.result == "success"]
            rolled = [r for r in rows if r.result == "rolled_back"]
            if not (succ and rolled):
                continue
            keep = succ[0]
            keep.result = "rolled_back"
            keep.fail_reason = rolled[0].fail_reason or "历史回填：订单已取消，券未核销"
            _hist_append(keep, "success", "rolled_back", "seed-migration",
                         "历史双行合并（订单取消回滚）")
            for extra in rolled + succ[1:]:
                db.delete(extra)
            merged += 1
        # ② 残留 success 行按订单状态校正
        fixed = 0
        leftovers = (db.query(CouponUsageLog)
                       .filter(CouponUsageLog.result == "success",
                               CouponUsageLog.order_no != "").all())
        if leftovers:
            status_map = {o.order_no: int(o.status or 0) for o in
                          db.query(OrderRecord).filter(OrderRecord.order_no.in_(
                              [r.order_no for r in leftovers])).all()}
            for r in leftovers:
                st = status_map.get(r.order_no)
                if st == 7:
                    r.result = "rolled_back"
                    r.fail_reason = r.fail_reason or "历史回填：订单已取消，券未核销"
                    _hist_append(r, "success", "rolled_back", "seed-migration", "订单已取消")
                    fixed += 1
                elif st == 1:
                    r.result = "pending"
                    _hist_append(r, "success", "pending", "seed-migration", "订单待支付")
                    fixed += 1
        db.commit()
        if merged or fixed:
            print(f"[seed] 券日志状态机回填：合并旧双行 {merged} 组，校正误记成功 {fixed} 行")


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_columns()
    _migrate_usage_log_lifecycle()
    with SessionLocal() as db:  # type: Session
        changed = False
        for spec in BUILTIN_ROLES:
            role = db.query(Role).filter(Role.name == spec["name"]).first()
            if not role:
                role = Role(name=spec["name"], is_builtin=True)
                db.add(role)
                changed = True
            role.description = spec["description"]
            role.permissions = spec["permissions"]
        db.commit()
        if not db.query(SystemUser).filter(SystemUser.username == DEFAULT_ADMIN["username"]).first():
            admin_role = db.query(Role).filter(Role.name == "admin").one()
            db.add(SystemUser(
                username=DEFAULT_ADMIN["username"],
                password_hash=hash_password(DEFAULT_ADMIN["password"]),
                display_name=DEFAULT_ADMIN["display_name"],
                role_id=admin_role.id,
            ))
            db.commit()
            changed = True
        if changed:
            print("[seed] 初始数据已就绪（admin / Admin@123）")


if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    init_db()
