import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/database/app.db")
SQLITE_BUSY_TIMEOUT_SECONDS = float(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30"))
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
DASHSCOPE_MODEL = os.getenv("DASHSCOPE_MODEL", "")
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "0.25"))
OCR_CONCURRENCY = int(os.getenv("OCR_CONCURRENCY", "1"))
GEMINI_CONCURRENCY = int(os.getenv("GEMINI_CONCURRENCY", "20"))
WORKER_BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "50"))
PROCESSING_LEASE_SECONDS = int(os.getenv("PROCESSING_LEASE_SECONDS", "900"))
