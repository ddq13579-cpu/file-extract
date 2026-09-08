# 文档智能整理系统

通用的本地文档结构化工具：上传 PDF 或图片后，Worker 从 PDF 提取文字或交给支持视觉输入的模型识别图片；模型按用户定义模板提取严格 JSON，并保存到 SQLite，可在网页修改与导出 Excel。模型统一走 OpenAI 兼容接口，可自由更换为任意兼容的服务商与模型。

## 架构

- **nginx**：唯一的 Web 入口，提供 Vue 静态文件并将 `/api/` 代理至 backend。
- **backend**：FastAPI API、SQLite 初始化、文件安全保存、模板/结果管理与 Excel 导出。
- **worker**：轮询 pending 文档，执行 PyMuPDF 文字提取，并通过 OpenAI 兼容接口调用模型。

关系：`templates 1--n template_fields`，`documents 1--1 ocr_results`，`documents n--1 templates`，`documents + templates 1--1 records`，`documents 1--n processing_logs`。

## 启动

```bash
cp .env.example .env
# 在 .env 中填写 API_KEY、BASE_URL 和 MODEL（任意 OpenAI 兼容服务与模型）
docker compose up -d --build
docker compose ps
```

访问 <http://localhost:6868>。查看处理日志：

```bash
docker compose logs -f worker
```

停止服务：`docker compose down`。持久化数据在 [data/uploads](./data/uploads)、[data/database](./data/database) 和 [data/exports](./data/exports)。

## 队列与 SQLite 配置

- 上传文件以 SHA-256 内容地址保存，且每个任务独占一个子目录；同名不同内容、同内容多次上传都不会互相覆盖，删除任一任务也不会影响其他任务的原文件。
- Worker 每次最多领取 `WORKER_BATCH_SIZE`（默认 50）个任务。处理进程异常时，超过 `PROCESSING_LEASE_SECONDS`（默认 900 秒）的任务会安全回收并重试。
- SQLite 默认启用 WAL 与 30 秒忙等待（`SQLITE_BUSY_TIMEOUT_SECONDS`）。单机小规模部署可保持此配置；需要多个 Worker 或更高并发时建议迁移 PostgreSQL。

## 数据位置与模板迁移

所有持久化数据都在宿主机的 [data](./data) 目录（`docker-compose.yml` 把 `./data` 挂载为容器内的 `/data`）：

| 内容 | 位置 | 是否在 git 里 |
| --- | --- | --- |
| 模板与字段（`templates`、`template_fields`）、任务、OCR 文字、结构化结果、处理日志 | `data/database/app.db`（SQLite，由 `DATABASE_URL` 指定） | 否，`.gitignore` 忽略 |
| 上传的原文件 | `data/uploads/` | 否 |
| 导出的 Excel | `data/exports/` | 否 |
| 模板 JSON 备份 | `data/templates.json`（由脚本生成） | 是，可提交 |

模板没有单独的文件，全部存在 `app.db` 的 `templates` + `template_fields` 两张表里，所以**换机器时只 `git clone` 是拿不到模板的**，必须把数据带过去。有三种做法：

### 方式一：整体搬迁 data 目录（模板 + 任务 + 结果全部带走）

```bash
docker compose down                                       # 先停服务，避免复制到写了一半的库
rsync -av --progress data/ 用户@新机器:/路径/OCR云项目/data/   # 或者直接拷 U 盘
# 新机器：拉代码、放好 .env，再 docker compose up -d --build
```

SQLite 开了 WAL，除 `app.db` 外还有 `app.db-wal`、`app.db-shm`。停服后再复制最保险；服务还在跑时先合并一次 WAL：

```bash
docker compose exec backend python -c "import sqlite3;c=sqlite3.connect('/data/database/app.db');c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close()"
```

### 方式二：只搬模板（推荐，跨机器最稳）

两个脚本只用 Python 标准库，宿主机上直接跑，不需要启动 Docker：

```bash
# 旧机器：导出模板
python3 scripts/export_templates.py                     # 默认写到 data/templates.json

# 把 data/templates.json 传到新机器（scp / U 盘 / git），然后在新机器上：
docker compose stop backend worker                      # 建议先停服务
python3 scripts/import_templates.py --file data/templates.json --dry-run   # 先预览
python3 scripts/import_templates.py --file data/templates.json             # 正式导入
docker compose up -d --build
```

- 导出文件不含数据库自增 id，字段顺序、类型、必填、说明以及“AI 提取 / 程序自动计算”配置都会完整保留。
- 目标库不存在或缺列时，脚本会自动建表、补列；同名模板默认整体覆盖（`--on-conflict update`），也可用 `skip` 跳过或 `rename` 另存为新名字。
- 覆盖时保留原 `template_id`，已有任务与结构化结果的关联不会断；导入前会先整体校验，任一模板非法则一条都不写入。
- 常用参数：`--db` 指定数据库路径、`--out` 指定导出路径、`--dry-run` 只预览不写库。

### 方式三：让模板随 git 一起走

`data/templates.json` 没有被 `.gitignore` 忽略。每次改完模板执行 `python3 scripts/export_templates.py`，再 `git add data/templates.json && git commit`，新机器 `git clone` 后只需运行一次 `python3 scripts/import_templates.py --file data/templates.json` 就能恢复模板。

## 使用

1. 在“模板管理”创建模板并配置字段名称、JSON `field_key`、类型、是否必填和说明。
2. 返回“文件处理”，选择模板，选择文件或使用目录选择器；拖放文件时保留浏览器提供的相对路径。
3. 点击“开始处理”。PDF 只通过 PyMuPDF 提取前两页的嵌入文字；没有文字的扫描 PDF 会失败（V1 不会隐式 OCR PDF）。图片由支持视觉输入的模型直接识别。
4. 在“处理结果”查看自动刷新的状态、受控的原文件链接与原始文字，选择结构化记录进行修改，或按当前选择的模板导出 Excel。失败的文件可在修复模板或配置后点击“重新处理”；非处理中任务可点击“删除任务”，同时删除上传原文件、OCR 文字、结构化结果和处理日志。

上传按 SHA-256 识别重复文件，但**不再跳过**：重复文件同样会建立独立任务、出现在“处理结果”并参与导出，只是直接复用最早那份相同文件的 OCR 文字与提取结果（先处于 `duplicate_waiting` 状态，复用成功后置为 `completed`，不再调用 OCR 和模型，不消耗 Token）。“处理结果”表格用橙色“重复文件”标记提示，悬停可看到与哪个文件重复；处理日志会写入一条 `duplicate` 记录；导出的 Excel 追加 `是否重复文件`、`重复源文件` 两列，并给重复行加浅黄底色。若源任务没有可复用结果（已删除、失败，或使用的是其他模板），该重复任务会自动回退为完整重新处理，重复标记保留。仅支持 PDF、JPG/JPEG、PNG、WEBP、BMP、TIF/TIFF、HEIC；其他文件会跳过。所有原始文件只经受控 API 访问，上传路径会拒绝路径穿越。

## 依赖说明

Worker 保持 PDF 与图片分流：PDF 使用 PyMuPDF 提取文字后，通过 OpenAI 兼容接口提取结构化结果；图片直接以本地文件的 Base64 data URL 提交给同一个支持视觉输入的模型完成识别和结构化提取。服务地址、模型名和密钥分别由 `BASE_URL`、`MODEL`、`API_KEY` 配置，只从环境变量读取，可替换为任意 OpenAI 兼容的服务商；模型请求并发数由 `AI_CONCURRENCY` 配置。每个请求的实际模型和 SDK 返回的输入、输出、总 Token 会写入 `processing_logs`。结构化结果包含大陆身份证号时，会校验末位校验码；校验失败会完整重识别一次，仍失败则任务报错。HEIC 图片会在内存中转换为 PNG，以保持既有支持的上传格式。
