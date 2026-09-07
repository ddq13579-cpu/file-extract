import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import or_, select, text

from .config import (
    GEMINI_CONCURRENCY,
    OCR_CONCURRENCY,
    PROCESSING_LEASE_SECONDS,
    WORKER_BATCH_SIZE,
    WORKER_POLL_SECONDS,
)
from .database import SessionLocal
from .models import Document, OCRResult, ProcessingLog, Record, TemplateField
from .services.ai.dashscope import DashScopeProvider, invalid_id_numbers
from .services.calculator import calculate_template_fields
from .services.pdf_extractor import extract_pdf_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def log(db, document_id: int, stage: str, level: str, message: str):
    logger.log(getattr(logging, level.upper(), logging.INFO), "document=%s %s", document_id, message)
    db.add(ProcessingLog(document_id=document_id, stage=stage, level=level, message=message))


def mark_failed(document_id: int, claim_token: str, stage: str, error: Exception):
    with SessionLocal() as db:
        document = db.get(Document, document_id)
        # A reclaimed task may still have an old worker completing in the
        # background.  That worker must not overwrite the current attempt.
        if document and document.claim_token == claim_token:
            document.status, document.error_message = "failed", str(error)
            log(db, document_id, stage, "error", str(error))
            db.commit()


def validate_data(data: dict[str, Any], fields: Sequence[TemplateField]) -> dict[str, Any]:
    expected = {field.field_key for field in fields}
    if set(data) != expected:
        raise ValueError("DashScope response keys do not exactly match template fields")
    for field in fields:
        value = data[field.field_key]
        if value is None:
            continue
        valid = {
            "text": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "date": isinstance(value, str) and len(value) == 10,
            "boolean": isinstance(value, bool),
        }[field.field_type]
        if not valid:
            raise ValueError(f"Invalid type for {field.field_key}")
    return data


def claim_pending_batch() -> list[tuple[int, str]]:
    """Atomically claim a bounded batch and reclaim expired leases.

    ``BEGIN IMMEDIATE`` is deliberate for SQLite: it serializes the small claim
    transaction across worker processes while all expensive OCR/AI work remains
    outside the database lock.
    """
    with SessionLocal() as db:
        now = datetime.utcnow()
        expired_before = now - timedelta(seconds=PROCESSING_LEASE_SECONDS)
        db.execute(text("BEGIN IMMEDIATE"))
        documents = db.scalars(
            select(Document)
            .where(
                or_(
                    Document.status == "pending",
                    (Document.status.in_(("processing", "ocr_completed", "ai_processing")))
                    & or_(Document.claimed_at.is_(None), Document.claimed_at < expired_before),
                )
            )
            .order_by(Document.created_at)
            .limit(WORKER_BATCH_SIZE)
        ).all()
        claimed: list[tuple[int, str]] = []
        for document in documents:
            was_expired = document.status != "pending"
            claim_token = uuid.uuid4().hex
            document.status = "processing"
            document.claim_token = claim_token
            document.claimed_at = now
            document.attempts = (document.attempts or 0) + 1
            message = f"Added to OCR batch: {document.filename} ({document.file_type})"
            if was_expired:
                message = f"Recovered expired processing lease; {message}"
            log(db, document.id, "start", "warning" if was_expired else "info", message)
            claimed.append((document.id, claim_token))
        db.commit()
        return claimed


def extract_ocr(document_id: int, claim_token: str) -> int | None:
    try:
        with SessionLocal() as db:
            document = db.get(Document, document_id)
            if not document or document.status != "processing" or document.claim_token != claim_token:
                return None
            path = Path(document.file_path)
            if document.file_type.lower() == "pdf":
                raw_text, engine = extract_pdf_text(path), "pymupdf"
                if not raw_text:
                    raise ValueError("PDF has no extractable text; scanned PDF OCR is not supported in V1")
            else:
                raw_text, engine = "", "dashscope_vision"
            ocr_result = db.scalar(select(OCRResult).where(OCRResult.document_id == document_id))
            if ocr_result:
                ocr_result.engine, ocr_result.raw_text = engine, raw_text
            else:
                db.add(OCRResult(document_id=document_id, engine=engine, raw_text=raw_text))
            document.status = "ocr_completed"
            log(db, document_id, "ocr", "info", f"{engine} completed; text length: {len(raw_text)}")
            db.commit()
            return document_id
    except Exception as error:
        mark_failed(document_id, claim_token, "ocr", error)
        return None


def extract_with_model(document_id: int, claim_token: str, batch_started_at: float):
    try:
        with SessionLocal() as db:
            document = db.get(Document, document_id)
            ocr_result = db.scalar(select(OCRResult).where(OCRResult.document_id == document_id))
            if (not document or not ocr_result or document.status != "ocr_completed"
                    or document.claim_token != claim_token):
                return
            template_id = document.template_id
            document_file_type = document.file_type.lower()
            document_path = Path(document.file_path)
            fields = db.scalars(
                select(TemplateField)
                .where(TemplateField.template_id == template_id)
                .order_by(TemplateField.sort_order)
            ).all()
            if not fields:
                raise ValueError("Template has no fields")
            document.status = "ai_processing"
            log(db, document_id, "ai", "info", "DashScope request started")
            db.commit()
            raw_text = ocr_result.raw_text
            ai_fields = [f for f in fields if getattr(f, "extraction_type", "ai_extract") != "calculated"]
            field_data = [
                {
                    "field_key": field.field_key,
                    "field_type": field.field_type,
                    "description": field.description,
                    "required": field.required,
                }
                for field in ai_fields
            ]

        provider = DashScopeProvider()
        source_path = None if document_file_type == "pdf" else document_path
        extraction = None
        data = None
        for attempt in range(2):
            attempt_number = attempt + 1
            if field_data:
                request_started_at = datetime.utcnow()
                started_monotonic = time.monotonic()
                try:
                    extraction = provider.extract(raw_text, field_data, source_path)
                except Exception as error:
                    request_completed_at = datetime.utcnow()
                    duration_ms = round((time.monotonic() - started_monotonic) * 1000)
                    with SessionLocal() as db:
                        db.add(ProcessingLog(
                            document_id=document_id,
                            stage="ai",
                            level="error",
                            message=f"DashScope request failed (attempt {attempt_number}): {error}",
                            attempt=attempt_number,
                            request_started_at=request_started_at,
                            request_completed_at=request_completed_at,
                            duration_ms=duration_ms,
                            response_json=getattr(error, "response_json", None),
                        ))
                        db.commit()
                    raise
                usage = extraction.usage
                request_completed_at = datetime.utcnow()
                duration_ms = round((time.monotonic() - started_monotonic) * 1000)
                with SessionLocal() as db:
                    db.add(ProcessingLog(
                        document_id=document_id,
                        stage="ai",
                        level="info",
                        message=f"DashScope request completed (attempt {attempt_number})",
                        model_name=usage.actual_model,
                        prompt_tokens=usage.prompt_tokens,
                        candidates_tokens=usage.candidates_tokens,
                        total_tokens=usage.total_tokens,
                        response_json=extraction.response_json,
                        attempt=attempt_number,
                        request_started_at=request_started_at,
                        request_completed_at=request_completed_at,
                        duration_ms=duration_ms,
                    ))
                    db.commit()
                data = extraction.data
            else:
                data = {}

            data = calculate_template_fields(data, fields)
            validate_data(data, fields)
            invalid_ids = invalid_id_numbers(data)
            if not invalid_ids:
                break
            if attempt == 0:
                with SessionLocal() as db:
                    log(db, document_id, "ai", "warning",
                        f"Identity checksum failed; retrying recognition: {', '.join(invalid_ids)}")
                    db.commit()
            else:
                raise ValueError(f"Mainland ID checksum validation failed: {', '.join(invalid_ids)}")

        with SessionLocal() as db:
            document = db.get(Document, document_id)
            if not document or document.claim_token != claim_token:
                return
            record = db.scalar(
                select(Record).where(Record.document_id == document_id, Record.template_id == template_id)
            )
            if record:
                record.json_data, record.status = json.dumps(data, ensure_ascii=False), "completed"
            else:
                db.add(Record(
                    document_id=document_id,
                    template_id=template_id,
                    json_data=json.dumps(data, ensure_ascii=False),
                    status="completed",
                ))
            document.status, document.error_message = "completed", None
            elapsed_seconds = time.monotonic() - batch_started_at
            log(db, document_id, "complete", "info", f"Batch processing completed in {elapsed_seconds:.2f}s")
            db.commit()
    except Exception as error:
        mark_failed(document_id, claim_token, "ai", error)


def process_batch(claims: list[tuple[int, str]], ocr_executor: ThreadPoolExecutor):
    batch_started_at = time.monotonic()
    logger.info("Starting batch: %s documents, preparation concurrency=%s, DashScope concurrency=%s",
                len(claims), OCR_CONCURRENCY, GEMINI_CONCURRENCY)
    ocr_completed: list[tuple[int, str]] = []
    futures = [ocr_executor.submit(extract_ocr, document_id, claim_token) for document_id, claim_token in claims]
    for future in as_completed(futures):
        document_id = future.result()
        if document_id:
            claim_token = next(token for claimed_id, token in claims if claimed_id == document_id)
            ocr_completed.append((document_id, claim_token))

    logger.info("OCR batch completed: %s/%s documents; starting DashScope batch", len(ocr_completed), len(claims))
    with ThreadPoolExecutor(max_workers=GEMINI_CONCURRENCY, thread_name_prefix="gemini") as executor:
        futures = [
            executor.submit(extract_with_model, document_id, claim_token, batch_started_at)
            for document_id, claim_token in ocr_completed
        ]
        for future in as_completed(futures):
            future.result()
    logger.info("Batch completed in %.2fs", time.monotonic() - batch_started_at)


def run():
    with ThreadPoolExecutor(max_workers=OCR_CONCURRENCY, thread_name_prefix="ocr") as ocr_executor:
        while True:
            claims = claim_pending_batch()
            if claims:
                process_batch(claims, ocr_executor)
            else:
                time.sleep(WORKER_POLL_SECONDS)


if __name__ == "__main__":
    run()
