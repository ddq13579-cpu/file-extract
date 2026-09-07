from pathlib import Path
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/database/app.db")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "/data/exports"))
SQLITE_BUSY_TIMEOUT_SECONDS = float(os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30"))
SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}

AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "ocr-cloud-secret-key-2026")
