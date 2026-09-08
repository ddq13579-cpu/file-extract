#!/usr/bin/env python3
"""把模板（templates + template_fields）导出成 JSON，用于备份或迁移到另一台机器。

只依赖 Python 标准库，宿主机上直接运行即可，不需要启动 Docker：

    python3 scripts/export_templates.py
    python3 scripts/export_templates.py --db data/database/app.db --out ~/templates-backup.json

默认导出到 data/templates.json。这个路径没有被 .gitignore 忽略，所以可以随仓库一起
提交，换机器时 git clone 后再执行 scripts/import_templates.py 即可恢复模板。
导出的 JSON 不含数据库自增 id，因此可以在任意机器上重新写入，不会与已有 id 冲突。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "database" / "app.db"
DEFAULT_OUT = REPO_ROOT / "data" / "templates.json"

# 字段顺序即导入后的 sort_order，和后端 assign_template() 的行为保持一致。
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


def export_templates(db_path: Path) -> list[dict]:
    if not db_path.exists():
        sys.exit(f"数据库文件不存在：{db_path}")

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        existing = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "templates" not in existing or "template_fields" not in existing:
            sys.exit(f"{db_path} 里没有 templates / template_fields 表，请确认这是本项目的数据库")

        select_fields = ", ".join(FIELD_COLUMNS)
        templates: list[dict] = []
        for template in connection.execute(
            "SELECT id, name, description, created_at, updated_at FROM templates ORDER BY name"
        ):
            fields = [
                {column: row[column] for column in FIELD_COLUMNS}
                for row in connection.execute(
                    f"SELECT {select_fields} FROM template_fields"
                    " WHERE template_id = ? ORDER BY sort_order, id",
                    (template["id"],),
                )
            ]
            for field in fields:
                field["required"] = bool(field["required"])
            templates.append(
                {
                    "name": template["name"],
                    "description": template["description"] or "",
                    "created_at": template["created_at"],
                    "updated_at": template["updated_at"],
                    "fields": fields,
                }
            )
        return templates
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="导出模板为 JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite 数据库路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"输出 JSON 路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--indent", type=int, default=2, help="JSON 缩进，0 表示单行输出")
    args = parser.parse_args()

    templates = export_templates(args.db)
    payload = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source_database": str(args.db),
        "templates": templates,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=args.indent or None) + "\n", encoding="utf-8"
    )

    field_total = sum(len(template["fields"]) for template in templates)
    print(f"已导出 {len(templates)} 个模板、{field_total} 个字段 -> {args.out}")
    for template in templates:
        print(f"  - {template['name']}（{len(template['fields'])} 个字段）")


if __name__ == "__main__":
    main()
