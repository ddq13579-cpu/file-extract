import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL, SQLITE_BUSY_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

IS_SQLITE = DATABASE_URL.startswith("sqlite")

connect_args = (
    {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
    if IS_SQLITE else {}
)
engine = create_engine(DATABASE_URL, connect_args=connect_args)


if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _connection_record):
        """Make concurrent API/worker access as reliable as SQLite permits."""
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        # 每 200 页（约 800KB）就把 WAL 折回主库：WAL 越小，异常退出后要重放的内容
        # 越少，读端撞上过期 WAL 索引的窗口也越短。
        cursor.execute("PRAGMA wal_autocheckpoint = 200")
        cursor.close()


def checkpoint_sqlite() -> None:
    """进程启动时把 WAL 合并回主库；失败只告警，避免库有瑕疵时容器反复重启。"""
    if not IS_SQLITE:
        return
    try:
        with engine.connect() as connection:
            connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    except Exception:  # noqa: BLE001 - 启动阶段的修复动作不应该让服务起不来
        logger.exception("SQLite WAL checkpoint failed; run: bash scripts/db.sh repair")


def sqlite_integrity_check() -> str:
    """返回 integrity_check 结果，供 /api/health 与运维脚本使用。"""
    if not IS_SQLITE:
        return "skipped"
    with engine.connect() as connection:
        return str(connection.execute(text("PRAGMA integrity_check")).scalar())


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

