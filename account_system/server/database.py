"""SQLAlchemy 数据库引擎与会话管理（SQLite，文件位于 account_system/data/）。"""

import logging
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")


def enable_wal(target_engine) -> None:
    """为引擎启用 SQLite WAL 日志模式。

    为什么：H5 收银台（8010 独立进程）与主 API（8000）+ 后台 watcher 会并发读写同一
    app.db，默认 journal 模式下写锁会阻塞读；WAL 允许读写并存，短事务天然容忍并发
    （SQLite 的 database is locked 由 WAL + busy 重试大幅缓解）。幂等：已是 WAL 时无操作。
    """
    from sqlalchemy import text

    try:
        with target_engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode=WAL")).scalar()
        logger.info("SQLite journal_mode=%s (db=%s)", mode, DB_PATH)
    except Exception:   # 异常安全：WAL 失败不阻断启动（回退默认 journal 仍可用，仅并发性差）
        logger.warning("启用 WAL 失败（不影响功能，并发读写性能回退）", exc_info=True)


engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
# 每个新连接同步 PRAGMA（WAL 是库级持久属性，但连接级同步可覆盖被外部工具改掉的场景；
# busy_timeout 给短事务加 5 秒等待窗口，配合 WAL 把 locked 错误压到最低）
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    try:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
    except Exception:
        logger.warning("连接级 PRAGMA 设置失败", exc_info=True)


enable_wal(engine)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
