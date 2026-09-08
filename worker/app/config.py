import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/database/app.db")
SQLITE_BUSY_TIMEOUT_SECONDS = float(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30"))
API_KEY = os.getenv("API_KEY", "")
BASE_URL = os.getenv("BASE_URL", "")
MODEL = os.getenv("MODEL", "")
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "0.25"))
OCR_CONCURRENCY = int(os.getenv("OCR_CONCURRENCY", "1"))
AI_CONCURRENCY = int(os.getenv("AI_CONCURRENCY", "20"))
WORKER_BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "50"))
PROCESSING_LEASE_SECONDS = int(os.getenv("PROCESSING_LEASE_SECONDS", "900"))
