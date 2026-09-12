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
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        # 与 backend 保持一致：WAL 越小越好，减少读写两端看到不一致索引的机会。
        cursor.execute("PRAGMA wal_autocheckpoint = 200")
        cursor.close()


def checkpoint_sqlite() -> None:
    """worker 启动时把 WAL 合并回主库；失败只告警，不阻断轮询。"""
    if not IS_SQLITE:
        return
    try:
        with engine.connect() as connection:
            connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    except Exception:  # noqa: BLE001 - 库有瑕疵时也要让 worker 起来报错，而不是崩溃循环
        logger.exception("SQLite WAL checkpoint failed; run: bash scripts/db.sh repair")


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass

