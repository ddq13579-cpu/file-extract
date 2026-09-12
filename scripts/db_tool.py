#!/usr/bin/env python3
"""SQLite 运维工具：在容器内运行，由 scripts/db.sh 从宿主机调用。

数据库放在 Docker 命名卷里，宿主机看不到也不应该直接打开它（macOS 上从宿主机
打开容器正在使用的库会破坏 WAL 一致性，正是之前 "database disk image is
malformed" 的成因之一）。所以备份、修复、恢复都在容器内完成，备份文件写到
/data/backups，也就是宿主机项目目录下的 ./data/backups。

只用标准库，因此不依赖 app 包，可以在 backend 或 worker 镜像里运行。
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_URL = os.getenv("DATABASE_URL", "sqlite:////data/database/app.db")
BACKUP_DIR = Path(os.getenv("DB_BACKUP_DIR", "/data/backups"))
TABLES = ("templates", "template_fields", "documents", "ocr_results", "records", "processing_logs")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def resolve_path(url: str) -> Path:
    if not url.startswith("sqlite"):
        sys.exit(f"只支持 SQLite，当前 DATABASE_URL={url}")
    return Path(url.split("///", 1)[1])


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=60)
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def integrity(path: Path) -> str:
    connection = connect(path)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def command_status(path: Path, _args) -> None:
    if not path.exists():
        sys.exit(f"数据库不存在：{path}")
    connection = connect(path)
    print(f"数据库    : {path}（{path.stat().st_size / 1024:.1f} KB）")
    print(f"journal   : {connection.execute('PRAGMA journal_mode').fetchone()[0]}")
    print(f"完整性    : {connection.execute('PRAGMA integrity_check').fetchone()[0]}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    print(f"外键检查  : {'无违规' if not violations else violations[:10]}")
    for table in TABLES:
        try:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<16}: {count} 行")
        except sqlite3.Error as error:
            print(f"  {table:<16}: 读取失败 -> {error}")
    connection.close()
    wal = sibling(path, "-wal")
    print(f"WAL       : {wal.stat().st_size / 1024:.1f} KB" if wal.exists() else "WAL       : 无")


def command_backup(path: Path, args) -> None:
    if not path.exists():
        sys.exit(f"数据库不存在：{path}")
    destination = Path(args.destination) if args.destination else BACKUP_DIR / f"app-{timestamp()}.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # SQLite 在线备份 API：服务运行中也能拿到一致快照，不需要停服。
    source = connect(path)
    target = sqlite3.connect(destination)
    try:
        with target:
            source.backup(target)
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        target.close()
        source.close()
    print(f"已备份 {destination}（{destination.stat().st_size / 1024:.1f} KB），完整性：{integrity(destination)}")


def command_repair(path: Path, _args) -> None:
    if not path.exists():
        sys.exit(f"数据库不存在：{path}")
    raw_dir = BACKUP_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp()
    snapshot = raw_dir / f"app-{stamp}-before-repair.db"
    for suffix in ("", "-wal", "-shm"):
        original = sibling(path, suffix)
        if original.exists():
            shutil.copy2(original, sibling(snapshot, suffix))
    print(f"原始文件已快照到 {snapshot}")

    connection = connect(path)
    print(f"wal_checkpoint(TRUNCATE): {connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()}")
    result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    connection.close()
    print(f"integrity_check: {result}")
    if result == "ok":
        for suffix in ("-wal", "-shm"):
            sibling(path, suffix).unlink(missing_ok=True)
        print("数据库正常，已清理残留的 -wal/-shm，无需重建")
        return

    print("仍有坏页，用 SQL dump 重建 ...")
    rebuilt = sibling(path, f"-rebuilt-{stamp}.db")
    source = sqlite3.connect(path)
    target = sqlite3.connect(rebuilt)
    skipped = 0
    try:
        with target:
            for statement in source.iterdump():
                try:
                    target.execute(statement)
                except sqlite3.Error as error:
                    skipped += 1
                    print(f"  跳过损坏内容：{error}")
    except sqlite3.DatabaseError as error:
        target.close()
        source.close()
        rebuilt.unlink(missing_ok=True)
        sys.exit(f"重建失败，损坏范围过大：{error}\n"
                 f"请改用最近的备份恢复：bash scripts/db.sh restore <备份文件>\n"
                 f"（备份位于 ./data/backups，原始快照在 {snapshot}）")
    finally:
        target.close()
        source.close()

    result = integrity(rebuilt)
    print(f"重建后 integrity_check: {result}，跳过 {skipped} 条损坏语句")
    if result != "ok":
        rebuilt.unlink(missing_ok=True)
        sys.exit("重建结果仍不完整，已保留原库，请改用备份恢复")

    corrupt = sibling(path, f"-corrupt-{stamp}.db")
    shutil.move(path, corrupt)
    shutil.move(rebuilt, path)
    for suffix in ("-wal", "-shm"):
        sibling(path, suffix).unlink(missing_ok=True)
    print(f"修复完成。损坏的旧库保留为 {corrupt}")
    command_status(path, _args)


def command_restore(path: Path, args) -> None:
    source = Path(args.source)
    if not source.is_file():
        sys.exit(f"备份文件不存在：{source}")
    result = integrity(source)
    if result != "ok":
        sys.exit(f"备份文件本身不完整（{result}），已取消恢复")
    if path.exists():
        replaced = sibling(path, f"-replaced-{timestamp()}.db")
        shutil.move(path, replaced)
        print(f"当前数据库已移开保存为 {replaced}")
    for suffix in ("-wal", "-shm"):
        sibling(path, suffix).unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, path)
    print(f"已从 {source} 恢复到 {path}，完整性：{integrity(path)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite 运维工具（在容器内运行）")
    parser.add_argument("command", choices=("status", "backup", "repair", "restore"))
    parser.add_argument("argument", nargs="?", default=None,
                        help="backup: 目标文件（默认 ./data/backups/app-<时间戳>.db）；restore: 备份文件")
    parser.add_argument("--database-url", default=DEFAULT_URL)
    args = parser.parse_args()

    path = resolve_path(args.database_url)
    handlers = {"status": command_status, "backup": command_backup,
                "repair": command_repair, "restore": command_restore}
    handlers[args.command](path, argparse.Namespace(destination=args.argument, source=args.argument))


if __name__ == "__main__":
    main()
