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


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_columns()
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
