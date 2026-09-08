#!/usr/bin/env python3
"""把 scripts/export_templates.py 导出的 JSON 写入另一台机器的数据库。

只依赖 Python 标准库。典型迁移流程：

    # 旧机器
    python3 scripts/export_templates.py --out ~/templates-backup.json
    # 把 templates-backup.json 拷到新机器（U 盘 / scp / 网盘均可），然后在新机器上：
    docker compose stop backend worker  # 建议先停服务
    python3 scripts/import_templates.py --file ~/templates-backup.json

同名模板默认按导入内容整体覆盖（--on-conflict update），也可以选 skip 跳过或 rename 另存新名字。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "database" / "app.db"

FIELD_COLUMNS = (
    "field_name",
    "field_key",
    "field_type",
    "description",
    "required",
    "extraction_type",
    "calc_rule",
    "source_field_key",
    "calc_params",
)
FIELD_TYPES = {"text", "number", "date", "boolean"}
EXTRACTION_TYPES = {"ai_extract", "calculated"}
KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS templates (
        id INTEGER NOT NULL,
        name VARCHAR(120) NOT NULL,
        description TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS template_fields (
        id INTEGER NOT NULL,
        template_id INTEGER NOT NULL,
        field_name VARCHAR(120) NOT NULL,
        field_key VARCHAR(80) NOT NULL,
        field_type VARCHAR(20) NOT NULL,
        description TEXT NOT NULL,
        required BOOLEAN NOT NULL,
        sort_order INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        extraction_type VARCHAR(20) DEFAULT 'ai_extract',
        calc_rule VARCHAR(40),
        source_field_key VARCHAR(80),
        calc_params TEXT,
        PRIMARY KEY (id),
        UNIQUE (template_id, field_key),
        FOREIGN KEY(template_id) REFERENCES templates (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_template_fields_template_id ON template_fields (template_id)",
]

# 老版本数据库可能缺少这几列，和 backend/app/main.py 的 startup 迁移保持一致。
FIELD_COLUMN_MIGRATIONS = {
    "extraction_type": "VARCHAR(20) DEFAULT 'ai_extract'",
    "calc_rule": "VARCHAR(40)",
    "source_field_key": "VARCHAR(80)",
    "calc_params": "TEXT",
}


def sqlite_now() -> str:
    # 和 SQLAlchemy 写入 SQLite 的 UTC  naive datetime 格式保持一致。
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def validate(template: dict, index: int) -> list[str]:
    """返回错误列表；空列表表示这个模板可以导入。"""
    errors: list[str] = []
    name = template.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"第 {index + 1} 个模板：name 缺失或为空")
        label = f"模板 #{index + 1}"
    else:
        label = f"模板「{name}」"

    fields = template.get("fields")
    if not isinstance(fields, list):
        errors.append(f"{label}：fields 必须是数组")
        return errors

    seen_keys: set[str] = set()
    for position, field in enumerate(fields, start=1):
        if not isinstance(field, dict):
            errors.append(f"{label} 第 {position} 个字段不是对象")
            continue
        key = field.get("field_key")
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            errors.append(f"{label} 第 {position} 个字段 field_key 非法：{key!r}")
        elif key in seen_keys:
            errors.append(f"{label} field_key 重复：{key}")
        else:
            seen_keys.add(key)
        if not str(field.get("field_name") or "").strip():
            errors.append(f"{label} 字段 {key or position} 缺少 field_name")
        if field.get("field_type") not in FIELD_TYPES:
            errors.append(f"{label} 字段 {key or position} field_type 非法：{field.get('field_type')!r}")
        if field.get("extraction_type", "ai_extract") not in EXTRACTION_TYPES:
            errors.append(f"{label} 字段 {key or position} extraction_type 非法：{field.get('extraction_type')!r}")
    return errors


def ensure_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(template_fields)")}
    for name, definition in FIELD_COLUMN_MIGRATIONS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE template_fields ADD COLUMN {name} {definition}")


def unique_name(connection: sqlite3.Connection, name: str) -> str:
    candidate = f"{name} (导入)"
    suffix = 2
    while connection.execute("SELECT 1 FROM templates WHERE name = ?", (candidate,)).fetchone():
        candidate = f"{name} (导入 {suffix})"
        suffix += 1
    return candidate


def write_fields(connection: sqlite3.Connection, template_id: int, fields: list[dict]) -> None:
    connection.execute("DELETE FROM template_fields WHERE template_id = ?", (template_id,))
    now = sqlite_now()
    for sort_order, field in enumerate(fields):
        values = {column: field.get(column) for column in FIELD_COLUMNS}
        values["description"] = values["description"] or ""
        values["required"] = 1 if values["required"] else 0
        values["extraction_type"] = values["extraction_type"] or "ai_extract"
        connection.execute(
            "INSERT INTO template_fields (template_id, field_name, field_key, field_type, description,"
            " required, sort_order, created_at, extraction_type, calc_rule, source_field_key, calc_params)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                template_id,
                values["field_name"],
                values["field_key"],
                values["field_type"],
                values["description"],
                values["required"],
                sort_order,
                field.get("created_at") or now,
                values["extraction_type"],
                values["calc_rule"],
                values["source_field_key"],
                values["calc_params"],
            ),
        )


def insert_template(connection: sqlite3.Connection, name: str, template: dict, created_at: str | None) -> int:
    cursor = connection.execute(
        "INSERT INTO templates (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, template.get("description") or "", created_at or sqlite_now(), sqlite_now()),
    )
    write_fields(connection, cursor.lastrowid, template["fields"])
    return cursor.lastrowid


def import_templates(db_path: Path, payload: dict, on_conflict: str, dry_run: bool) -> None:
    templates = payload.get("templates")
    if not isinstance(templates, list) or not templates:
        sys.exit("JSON 里没有可导入的模板（templates 为空）")

    errors: list[str] = []
    for index, template in enumerate(templates):
        errors.extend(validate(template, index))
    if errors:
        print("模板数据校验失败，未写入任何内容：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        ensure_schema(connection)

        created = updated = skipped = renamed = 0
        with connection:  # 出错自动回滚，正常结束提交
            for template in templates:
                name = template["name"].strip()
                field_count = len(template["fields"])
                row = connection.execute("SELECT id FROM templates WHERE name = ?", (name,)).fetchone()

                if row is None:
                    action = "新建"
                    if not dry_run:
                        insert_template(connection, name, template, template.get("created_at"))
                    created += 1
                elif on_conflict == "skip":
                    print(f"  跳过（已存在同名模板）：{name}")
                    skipped += 1
                    continue
                elif on_conflict == "rename":
                    new_name = unique_name(connection, name)
                    action = f"另存为「{new_name}」"
                    if not dry_run:
                        insert_template(connection, new_name, template, None)
                    renamed += 1
                else:
                    action = "覆盖更新"
                    if not dry_run:
                        connection.execute(
                            "UPDATE templates SET description = ?, updated_at = ? WHERE id = ?",
                            (template.get("description") or "", sqlite_now(), row["id"]),
                        )
                        write_fields(connection, row["id"], template["fields"])
                    updated += 1

                prefix = "[dry-run] " if dry_run else ""
                print(f"  {prefix}{action}：{name}（{field_count} 个字段）")

        summary = f"新建 {created}，覆盖 {updated}，另存 {renamed}，跳过 {skipped}"
        note = "（dry-run，未写入）" if dry_run else ""
        print(f"导入完成{note}：{summary} -> {db_path}")
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="从 JSON 导入模板")
    parser.add_argument("--file", type=Path, required=True, help="export_templates.py 生成的 JSON 文件")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"目标 SQLite 数据库（默认 {DEFAULT_DB}）")
    parser.add_argument(
        "--on-conflict",
        choices=("update", "skip", "rename"),
        default="update",
        help="同名模板的处理方式：update 整体覆盖（默认）、skip 跳过、rename 另存新名字",
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验并打印将执行的操作，不写库")
    args = parser.parse_args()

    if not args.file.exists():
        sys.exit(f"JSON 文件不存在：{args.file}")
    try:
        payload = json.loads(args.file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        sys.exit(f"JSON 解析失败：{error}")

    if isinstance(payload, list):  # 兼容直接就是模板数组的文件
        payload = {"templates": payload}
    import_templates(args.db, payload, args.on_conflict, args.dry_run)


if __name__ == "__main__":
    main()

