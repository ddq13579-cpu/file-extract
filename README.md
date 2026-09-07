# 文档智能整理系统

通用的本地文档结构化工具：上传 PDF 或图片后，Worker 从 PDF 提取文字或使用阿里百炼视觉模型识别图片；阿里百炼文本/视觉模型按用户定义模板提取严格 JSON，并保存到 SQLite，可在网页修改与导出 Excel。

## 架构

- **nginx**：唯一的 Web 入口，提供 Vue 静态文件并将 `/api/` 代理至 backend。
- **backend**：FastAPI API、SQLite 初始化、文件安全保存、模板/结果管理与 Excel 导出。
- **worker**：轮询 pending 文档，执行 PyMuPDF 和阿里百炼 OpenAI 兼容接口。

关系：`templates 1--n template_fields`，`documents 1--1 ocr_results`，`documents n--1 templates`，`documents + templates 1--1 records`，`documents 1--n processing_logs`。

## 启动

```bash
cp .env.example .env
# 在 .env 中填写 DASHSCOPE_API_KEY 和 DASHSCOPE_MODEL
docker compose up -d --build
docker compose ps
```

访问 <http://localhost:6868>。查看处理日志：

```bash
docker compose logs -f worker
```

停止服务：`docker compose down`。持久化数据在 [data/uploads](./data/uploads)、[data/database](./data/database) 和 [data/exports](./data/exports)。

## 队列与 SQLite 配置

- 上传文件以 SHA-256 内容地址保存；同一目录下的同名不同内容文件不会互相覆盖。
- Worker 每次最多领取 `WORKER_BATCH_SIZE`（默认 50）个任务。处理进程异常时，超过 `PROCESSING_LEASE_SECONDS`（默认 900 秒）的任务会安全回收并重试。
- SQLite 默认启用 WAL 与 30 秒忙等待（`SQLITE_BUSY_TIMEOUT_SECONDS`）。单机小规模部署可保持此配置；需要多个 Worker 或更高并发时建议迁移 PostgreSQL。

## 使用

1. 在“模板管理”创建模板并配置字段名称、JSON `field_key`、类型、是否必填和说明。
2. 返回“文件处理”，选择模板，选择文件或使用目录选择器；拖放文件时保留浏览器提供的相对路径。
3. 点击“开始处理”。PDF 只通过 PyMuPDF 提取前两页的嵌入文字；没有文字的扫描 PDF 会失败（V1 不会隐式 OCR PDF）。图片由阿里云 OCR 处理。
4. 在“处理结果”查看自动刷新的状态、受控的原文件链接与原始文字，选择结构化记录进行修改，或按当前选择的模板导出 Excel。失败的文件可在修复模板或配置后点击“重新处理”；非处理中任务可点击“删除任务”，同时删除上传原文件、OCR 文字、结构化结果和处理日志。

上传以 SHA-256 去重，重复的文件不会重新 OCR 或调用 Gemini。仅支持 PDF、JPG/JPEG、PNG、WEBP、BMP、TIF/TIFF、HEIC；其他文件会跳过。所有原始文件只经受控 API 访问，上传路径会拒绝路径穿越。

## 依赖说明

Worker 保持 PDF 与图片分流：PDF 使用 PyMuPDF 提取文字后，通过阿里百炼 OpenAI 兼容接口提取结构化结果；图片直接以本地文件的 Base64 data URL 提交给同一个支持视觉输入的百炼模型完成识别和结构化提取。模型由 `DASHSCOPE_MODEL` 配置，API Key 仅从环境变量读取。每个请求的实际模型和 SDK 返回的输入、输出、总 Token 会写入 `processing_logs`。结构化结果包含大陆身份证号时，会校验末位校验码；校验失败会完整重识别一次，仍失败则任务报错。HEIC 图片会在内存中转换为 PNG，以保持既有支持的上传格式。
