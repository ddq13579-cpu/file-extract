import hashlib
import hmac
import json
import mimetypes
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, selectinload
from openpyxl import Workbook
from openpyxl.styles import PatternFill

from .config import AUTH_PASSWORD, AUTH_USERNAME, EXPORT_DIR, SECRET_KEY, SUPPORTED_EXTENSIONS, UPLOAD_DIR
from .database import Base, engine, get_db
from .models import Document, OCRResult, ProcessingLog, Record, Template, TemplateField
from .schemas import LoginInput, RecordUpdate, TemplateInput, TemplateOutput

app = FastAPI(title="Document AI")


def current_auth_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def generate_valid_token():
    token_data = f"{AUTH_USERNAME}:{AUTH_PASSWORD}:{current_auth_date()}"
    return hmac.new(SECRET_KEY.encode(), token_data.encode(), hashlib.sha256).hexdigest()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in {"/api/health", "/api/auth/login"}:
        auth_header = request.headers.get("Authorization")
        query_token = request.query_params.get("token")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
        elif query_token:
            token = query_token

        valid_token = generate_valid_token()
        if not token or not hmac.compare_digest(token, valid_token):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    return await call_next(request)


@app.on_event("startup")
def startup():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        inspector = inspect(engine)
        columns = [c["name"] for c in inspector.get_columns("documents")]
        if "folder_level_3" not in columns:
            conn.execute(text("ALTER TABLE documents ADD COLUMN folder_level_3 VARCHAR(255)"))
            conn.commit()
        for name, definition in {
            "claim_token": "VARCHAR(64)",
            "claimed_at": "DATETIME",
            "attempts": "INTEGER DEFAULT 0",
            "is_duplicate": "BOOLEAN DEFAULT 0",
            "duplicate_of_id": "INTEGER",
            "duplicate_of_path": "VARCHAR(1024)",
        }.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE documents ADD COLUMN {name} {definition}"))
                conn.commit()
        # Duplicates are kept as separate tasks now, so sha256 must stop being
        # unique.  SQLite cannot drop a column constraint, but the uniqueness was
        # created as a standalone index, which can simply be recreated.
        for index in inspector.get_indexes("documents"):
            if index.get("unique") and index.get("column_names") == ["sha256"]:
                conn.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_sha256 ON documents (sha256)"))
                conn.commit()
                break
        log_columns = [c["name"] for c in inspector.get_columns("processing_logs")]
        for name, definition in {
            "model_name": "VARCHAR(120)",
            "prompt_tokens": "INTEGER",
            "candidates_tokens": "INTEGER",
            "total_tokens": "INTEGER",
            "attempt": "INTEGER",
            "request_started_at": "DATETIME",
            "request_completed_at": "DATETIME",
            "duration_ms": "INTEGER",
            "response_json": "TEXT",
        }.items():
            if name not in log_columns:
                conn.execute(text(f"ALTER TABLE processing_logs ADD COLUMN {name} {definition}"))
                conn.commit()

        field_columns = [c["name"] for c in inspector.get_columns("template_fields")]
        for name, definition in {
            "extraction_type": "VARCHAR(20) DEFAULT 'ai_extract'",
            "calc_rule": "VARCHAR(40)",
            "source_field_key": "VARCHAR(80)",
            "calc_params": "TEXT",
        }.items():
            if name not in field_columns:
                conn.execute(text(f"ALTER TABLE template_fields ADD COLUMN {name} {definition}"))
                conn.commit()


def template_or_404(template_id: int, db: Session) -> Template:
    template = db.scalar(select(Template).options(selectinload(Template.fields)).where(Template.id == template_id))
    if not template:
        raise HTTPException(404, "Template not found")
    return template


def assign_template(template: Template, payload: TemplateInput, db: Session):
    template.name, template.description = payload.name, payload.description
    template.fields.clear()
    db.flush()
    for index, field in enumerate(payload.fields):
        template.fields.append(TemplateField(**field.model_dump(exclude={"sort_order"}), sort_order=index))


def safe_relative_path(value: str, fallback: str) -> PurePosixPath:
    value = value.replace("\\", "/").strip("/")
    path = PurePosixPath(value or fallback)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise HTTPException(400, "Invalid relative path")
    return path


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(payload: LoginInput):
    if payload.username == AUTH_USERNAME and payload.password == AUTH_PASSWORD:
        token = generate_valid_token()
        return {"token": token, "username": payload.username}
    raise HTTPException(status_code=401, detail="账号或密码错误")


@app.get("/api/auth/check")
def check_auth():
    return {"status": "authenticated", "username": AUTH_USERNAME}


@app.get("/api/templates", response_model=list[TemplateOutput])
def list_templates(db: Session = Depends(get_db)):
    return db.scalars(select(Template).options(selectinload(Template.fields)).order_by(Template.name)).all()


@app.post("/api/templates", response_model=TemplateOutput, status_code=201)
def create_template(payload: TemplateInput, db: Session = Depends(get_db)):
    if db.scalar(select(Template).where(Template.name == payload.name)):
        raise HTTPException(409, "Template name already exists")
    template = Template()
    assign_template(template, payload, db)
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@app.get("/api/templates/{template_id}", response_model=TemplateOutput)
def get_template(template_id: int, db: Session = Depends(get_db)):
    return template_or_404(template_id, db)


@app.put("/api/templates/{template_id}", response_model=TemplateOutput)
def update_template(template_id: int, payload: TemplateInput, db: Session = Depends(get_db)):
    template = template_or_404(template_id, db)
    duplicate = db.scalar(select(Template).where(Template.name == payload.name, Template.id != template_id))
    if duplicate:
        raise HTTPException(409, "Template name already exists")
    assign_template(template, payload, db)
    db.commit()
    db.refresh(template)
    return template


@app.delete("/api/templates/{template_id}", status_code=204)
def delete_template(template_id: int, db: Session = Depends(get_db)):
    template = template_or_404(template_id, db)
    db.query(Record).filter(Record.template_id == template_id).delete(synchronize_session=False)
    db.query(Document).filter(Document.template_id == template_id).update({Document.template_id: None}, synchronize_session=False)
    db.delete(template)
    db.commit()


@app.post("/api/files/upload")
async def upload_files(
    template_id: Annotated[int, Form()],
    files: Annotated[list[UploadFile], File()],
    relative_paths: Annotated[list[str], Form()],
    db: Session = Depends(get_db),
):
    template_or_404(template_id, db)
    if len(files) != len(relative_paths):
        raise HTTPException(400, "Each file needs one relative path")
    uploaded, skipped, duplicates = [], [], []
    for file, supplied_path in zip(files, relative_paths):
        filename = Path(file.filename or "").name
        relative_path = safe_relative_path(supplied_path, filename)
        suffix = Path(filename).suffix.lower()
        if filename in {".DS_Store"} or "__MACOSX" in relative_path.parts:
            skipped.append(str(relative_path))
            continue
        if suffix not in SUPPORTED_EXTENSIONS:
            skipped.append(str(relative_path))
            continue
        # Stream to a private temporary file first.  The final location is content
        # addressed, so a later upload with the same relative path cannot replace
        # an earlier document's original file.
        temporary_dir = UPLOAD_DIR / ".tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = temporary_dir / uuid.uuid4().hex
        digest = hashlib.sha256()
        size = 0
        with temporary_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
        checksum = digest.hexdigest()
        # A duplicate is no longer skipped: it becomes its own task, reuses the
        # earliest identical file's result and stays marked as a duplicate.
        existing = db.scalar(
            select(Document).where(Document.sha256 == checksum).order_by(Document.created_at, Document.id)
        )
        # Every task owns its physical file, so deleting one duplicate can never
        # unlink the original file another task still points at.
        target = UPLOAD_DIR / checksum[:2] / checksum / uuid.uuid4().hex / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.replace(target)
        parts = relative_path.parts[:-1]
        document = Document(
            filename=filename, relative_path=str(relative_path), file_path=str(target), file_type=suffix[1:].upper(),
            mime_type=file.content_type or mimetypes.guess_type(filename)[0] or "", file_size=size, sha256=checksum,
            folder_level_1=parts[0] if parts else None,
            folder_level_2=parts[1] if len(parts) > 1 else None,
            folder_level_3=parts[2] if len(parts) > 2 else None,
            status="duplicate_waiting" if existing else "pending", template_id=template_id,
            is_duplicate=bool(existing),
            duplicate_of_id=existing.id if existing else None,
            duplicate_of_path=existing.relative_path if existing else None,
        )
        db.add(document)
        db.flush()
        uploaded.append({"id": document.id, "path": str(relative_path)})
        if existing:
            duplicates.append({
                "path": str(relative_path), "document_id": document.id,
                "duplicate_of_id": existing.id, "duplicate_of_path": existing.relative_path,
            })
    db.commit()
    return {"uploaded": uploaded, "skipped": skipped, "duplicates": duplicates}


@app.post("/api/files/process")
def process_files(document_ids: list[int], db: Session = Depends(get_db)):
    documents = db.scalars(select(Document).where(Document.id.in_(document_ids))).all()
    for document in documents:
        if document.status in {"failed", "skipped", "completed"}:
            document.status, document.error_message = "pending", None
    db.commit()
    return {"queued": len(documents)}


@app.post("/api/documents/retry-failed")
def retry_failed_documents(db: Session = Depends(get_db)):
    """Put every failed task back in the queue without touching other states."""
    documents = db.scalars(select(Document).where(Document.status == "failed")).all()
    for document in documents:
        document.status, document.error_message = "pending", None
    db.commit()
    return {"queued": len(documents)}


@app.get("/api/documents")
def list_documents(db: Session = Depends(get_db)):
    documents = db.scalars(select(Document).order_by(Document.created_at.desc())).all()
    return [{"id": d.id, "filename": d.filename, "relative_path": d.relative_path, "file_type": d.file_type,
             "status": d.status, "template_id": d.template_id, "created_at": d.created_at, "updated_at": d.updated_at,
             "error_message": d.error_message, "is_duplicate": bool(d.is_duplicate),
             "duplicate_of_id": d.duplicate_of_id, "duplicate_of_path": d.duplicate_of_path} for d in documents]


@app.get("/api/documents/{document_id}")
def get_document(document_id: int, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "Document not found")
    return {"id": document.id, "filename": document.filename, "relative_path": document.relative_path,
            "status": document.status, "is_duplicate": bool(document.is_duplicate),
            "duplicate_of_id": document.duplicate_of_id, "duplicate_of_path": document.duplicate_of_path,
            "raw_text": document.ocr_result.raw_text if document.ocr_result else None,
            "logs": [{"id": log.id, "stage": log.stage, "level": log.level, "message": log.message,
                      "model_name": log.model_name,
                      "prompt_tokens": log.prompt_tokens,
                      "candidates_tokens": log.candidates_tokens, "total_tokens": log.total_tokens,
                      "attempt": log.attempt, "request_started_at": log.request_started_at,
                      "request_completed_at": log.request_completed_at, "duration_ms": log.duration_ms,
                      "response_json": log.response_json,
                      "created_at": log.created_at}
                     for log in db.scalars(select(ProcessingLog).where(
                         ProcessingLog.document_id == document_id
                     ).order_by(ProcessingLog.created_at)).all()],
            "error_message": document.error_message}


@app.get("/api/documents/{document_id}/file")
def download_document(document_id: int, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if not document or not Path(document.file_path).is_file():
        raise HTTPException(404, "Document file not found")
    return FileResponse(document.file_path, filename=document.filename, media_type=document.mime_type)


@app.delete("/api/documents", status_code=200)
def delete_all_documents(db: Session = Depends(get_db)):
    documents = db.scalars(select(Document)).all()
    deleted_count = 0
    skipped_count = 0
    upload_root = UPLOAD_DIR.resolve()
    for document in documents:
        if document.status in {"processing", "ai_processing"}:
            skipped_count += 1
            continue
        file_path = Path(document.file_path).resolve()
        db.query(ProcessingLog).filter(ProcessingLog.document_id == document.id).delete()
        db.delete(document)
        deleted_count += 1
        if file_path.is_relative_to(upload_root):
            file_path.unlink(missing_ok=True)
    db.commit()
    return {"deleted": deleted_count, "skipped": skipped_count}


@app.delete("/api/documents/{document_id}", status_code=204)
def delete_document(document_id: int, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "Document not found")
    if document.status in {"processing", "ai_processing"}:
        raise HTTPException(409, "A processing document cannot be deleted; retry after processing finishes")

    file_path = Path(document.file_path).resolve()
    upload_root = UPLOAD_DIR.resolve()
    if not file_path.is_relative_to(upload_root):
        raise HTTPException(500, "Document path is outside the upload directory")

    db.query(ProcessingLog).filter(ProcessingLog.document_id == document_id).delete()
    db.delete(document)
    db.commit()
    file_path.unlink(missing_ok=True)
    return None


@app.get("/api/records")
def list_records(db: Session = Depends(get_db)):
    records = db.scalars(select(Record).order_by(Record.updated_at.desc())).all()
    return [{"id": r.id, "document_id": r.document_id, "template_id": r.template_id, "status": r.status,
             "json_data": json.loads(r.json_data), "updated_at": r.updated_at} for r in records]


@app.get("/api/records/{record_id}")
def get_record(record_id: int, db: Session = Depends(get_db)):
    record = db.get(Record, record_id)
    if not record:
        raise HTTPException(404, "Record not found")
    return {"id": record.id, "document_id": record.document_id, "template_id": record.template_id,
            "json_data": json.loads(record.json_data), "status": record.status}


@app.put("/api/records/{record_id}")
def update_record(record_id: int, payload: RecordUpdate, db: Session = Depends(get_db)):
    record = db.get(Record, record_id)
    if not record:
        raise HTTPException(404, "Record not found")
    template = template_or_404(record.template_id, db)
    allowed = {field.field_key for field in template.fields}
    if set(payload.json_data) != allowed:
        raise HTTPException(422, "Record keys must exactly match the template fields")
    record.json_data = json.dumps(payload.json_data, ensure_ascii=False)
    db.commit()
    return {"id": record.id, "json_data": payload.json_data}


@app.get("/api/export/excel")
def export_excel(template_id: int, db: Session = Depends(get_db)):
    template = template_or_404(template_id, db)
    # Export from document tasks rather than records: a failed task has no usable
    # record, but still needs an empty, traceable row in the spreadsheet.
    documents = db.scalars(
        select(Document)
        .where(Document.template_id == template_id, Document.status.in_(("completed", "failed")))
        .order_by(Document.created_at)
    ).all()
    completed_records = {
        record.document_id: record
        for record in db.scalars(
            select(Record).where(Record.template_id == template_id, Record.status == "completed")
        ).all()
    }
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = template.name[:31]
    fields = sorted(template.fields, key=lambda field: field.sort_order)
    
    doc_folder_parts = []
    max_levels = 2
    for document in documents:
        parts = list(PurePosixPath(document.relative_path).parts[:-1]) if document.relative_path else []
        # Failed documents deliberately have no exported field values, even if a
        # previous successful run left a record behind.
        record = completed_records.get(document.id) if document.status == "completed" else None
        doc_folder_parts.append((record, document, parts))
        if len(parts) > max_levels:
            max_levels = len(parts)

    folder_headers = [f"folder_level_{i+1}" for i in range(max_levels)]
    sheet.append([*folder_headers, *[field.field_name for field in fields], "是否重复文件", "重复源文件"])

    # Duplicate tasks are exported like any other row, but get two extra marking
    # columns and a highlighted row so they stand out in the spreadsheet.
    duplicate_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    for record, document, parts in doc_folder_parts:
        data = json.loads(record.json_data) if record else {}
        folder_values = [parts[i] if i < len(parts) else "" for i in range(max_levels)]
        duplicate_source = document.duplicate_of_path or (
            f"任务#{document.duplicate_of_id}" if document.duplicate_of_id else ""
        )
        sheet.append([
            *folder_values,
            *[data.get(field.field_key) for field in fields],
            "是" if document.is_duplicate else "",
            duplicate_source if document.is_duplicate else "",
        ])
        if document.is_duplicate:
            for cell in sheet[sheet.max_row]:
                cell.fill = duplicate_fill

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output = EXPORT_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', template.name)}-{datetime.utcnow():%Y%m%d%H%M%S}.xlsx"
    workbook.save(output)
    return FileResponse(output, filename=output.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
