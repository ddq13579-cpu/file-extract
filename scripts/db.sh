#!/usr/bin/env bash
# 在容器内操作命名卷里的 SQLite（宿主机访问不到该卷，也不应该直接打开库文件）。
#
#   bash scripts/db.sh status                      查看完整性与各表行数
#   bash scripts/db.sh backup [目标文件]           在线备份到 ./data/backups（服务可以不停）
#   bash scripts/db.sh repair                      先 docker compose down，再 checkpoint/重建
#   bash scripts/db.sh restore <备份文件>          先 docker compose down，再整库恢复
#
# 备份/恢复用的路径是容器内路径，./data 对应 /data，例如
#   bash scripts/db.sh restore /data/backups/app-20260912-090000.db
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE=backend
COMMAND="${1:-status}"
shift || true

case "$COMMAND" in
  status|backup)
    ;;
  repair|restore)
    if docker compose ps --status running --services 2>/dev/null | grep -qx "$SERVICE"; then
      echo "执行 $COMMAND 前请先停止服务：docker compose down" >&2
      exit 1
    fi
    ;;
  *)
    echo "未知命令：$COMMAND（可用：status | backup | repair | restore）" >&2
    exit 2
    ;;
esac

# 脚本本身不在镜像里，通过标准管道入给容器内的 python 执行。
docker compose run --rm --no-deps -T "$SERVICE" python - "$COMMAND" "$@" < scripts/db_tool.py
