import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import or_, select, text

from .config import (
    AI_CONCURRENCY,
    OCR_CONCURRENCY,
    PROCESSING_LEASE_SECONDS,
    WORKER_BATCH_SIZE,
    WORKER_POLL_SECONDS,
)
from .database import SessionLocal, checkpoint_sqlite
from .models import Document, OCRResult, ProcessingLog, Record, TemplateField
from .services.ai.openai_compatible import ModelResponseError, OpenAICompatibleProvider, invalid_id_numbers
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


def log_model_request_failure(document_id: int, attempt_number: int, error: Exception,
                              request_started_at: datetime, started_monotonic: float,
                              will_retry: bool) -> None:
    """把失败请求的耗时、Token 和模型原始返回写进处理日志；还会重试时降级为 warning。"""
    usage = getattr(error, "usage", None)
    with SessionLocal() as db:
        db.add(ProcessingLog(
            document_id=document_id,
            stage="ai",
            level="warning" if will_retry else "error",
            message=(f"Model request failed (attempt {attempt_number}): {error}"
                     + ("，将重试一次" if will_retry else "")),
            model_name=getattr(usage, "actual_model", None),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            candidates_tokens=getattr(usage, "candidates_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            attempt=attempt_number,
            request_started_at=request_started_at,
            request_completed_at=datetime.utcnow(),
            duration_ms=round((time.monotonic() - started_monotonic) * 1000),
            response_json=getattr(error, "response_json", None),
        ))
        db.commit()


def validate_data(data: dict[str, Any], fields: Sequence[TemplateField]) -> dict[str, Any]:
    expected = {field.field_key for field in fields}
    if set(data) != expected:
        missing = sorted(expected - set(data))
        unexpected = sorted(set(data) - expected)
        raise ValueError(
            "Model response keys do not exactly match template fields"
            f" (missing: {missing or '无'}, unexpected: {unexpected or '无'})"
        )
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
            raise ValueError(
                f"Invalid type for {field.field_key}: expected {field.field_type}, got {value!r}"
            )
    return data


NUMBER_NOISE_PATTERN = re.compile(r"[,\s￥¥$元]")
TRUE_WORDS = {"是", "真", "有", "对", "y", "yes", "true", "t"}
FALSE_WORDS = {"否", "假", "无", "错", "n", "no", "false", "f"}
DATE_PATTERN = re.compile(r"(\d{4})\s*[年/\-.\s]\s*(\d{1,2})\s*[月/\-.\s]\s*(\d{1,2})\s*日?")


def _to_number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    cleaned = NUMBER_NOISE_PATTERN.sub("", value)
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if number.is_integer() and not re.search(r"[.eE]", cleaned):
        return int(number)
    return number


def _to_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in TRUE_WORDS or word == "1":
            return True
        if word in FALSE_WORDS or word == "0":
            return False
    return None


def _to_iso_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = DATE_PATTERN.fullmatch(text)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def coerce_field_types(data: dict[str, Any], fields: Sequence[TemplateField]) -> dict[str, Any]:
    """把模型偶发写错的类型纠正回来，避免已经识别成功的数据因为格式被判失败。

    实测 qwen 会把 number 字段写成 "100.00"，date 字段写成 "2026年9月3日"。值本身
    是对的，Token 也花掉了，直接报 Invalid type 太浪费。这里只做无损转换：拿不准
    的值原样保留，交给 validate_data 报错。
    """
    result = dict(data)
    for field in fields:
        key = getattr(field, "field_key", None)
        if not key or result.get(key) is None:
            continue
        value = result[key]
        field_type = getattr(field, "field_type", "text")
        if field_type == "number":
            converted = _to_number(value)
        elif field_type == "boolean":
            converted = _to_boolean(value)
        elif field_type == "date":
            converted = _to_iso_date(value)
        elif field_type == "text":
            converted = _to_text(value)
        else:
            converted = None
        if converted is not None:
            result[key] = converted
    return result


def _to_text(value: Any) -> str | None:
    """身份证号之类被模型当成数字返回时，转回精确的文字。"""
    if isinstance(value, str):
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    return None


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
                raw_text, engine = "", "vision_model"
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
            log(db, document_id, "ai", "info", "Model request started")
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

        provider = OpenAICompatibleProvider()
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
                except ModelResponseError as error:
                    # 返回格式跑偏（数组、字段清单、代码块等）带有随机性：同样的图片
                    # 上一轮就是好的，所以格式错误单独重试一次，而不是立刻判失败。
                    log_model_request_failure(document_id, attempt_number, error, request_started_at,
                                              started_monotonic, will_retry=attempt == 0)
                    if attempt == 0:
                        continue
                    raise
                except Exception as error:
                    log_model_request_failure(document_id, attempt_number, error, request_started_at,
                                              started_monotonic, will_retry=False)
                    raise
                usage = extraction.usage
                request_completed_at = datetime.utcnow()
                duration_ms = round((time.monotonic() - started_monotonic) * 1000)
                with SessionLocal() as db:
                    db.add(ProcessingLog(
                        document_id=document_id,
                        stage="ai",
                        level="info",
                        message=f"Model request completed (attempt {attempt_number})",
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

            data = coerce_field_types(data, fields)
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


def resolve_duplicate(document_id: int):
    """Reuse an identical earlier task's result instead of re-running OCR/AI.

    A duplicate task is uploaded with status ``duplicate_waiting``, which the claim
    query never picks up.  Resolution happens here so an unfinished source task
    simply keeps the copy waiting, while an unusable source (deleted, failed, or
    extracted with a different template) falls back to full processing and keeps
    its duplicate mark.
    """
    with SessionLocal() as db:
        document = db.get(Document, document_id)
        if not document or document.status != "duplicate_waiting":
            return
        source = db.get(Document, document.duplicate_of_id) if document.duplicate_of_id else None
        source_label = document.duplicate_of_path or (
            f"任务#{document.duplicate_of_id}" if document.duplicate_of_id else "未知文件"
        )
        record = None
        if source:
            record = db.scalar(
                select(Record).where(
                    Record.document_id == source.id,
                    Record.template_id == document.template_id,
                    Record.status == "completed",
                )
            )

        if record:
            source_ocr = db.scalar(select(OCRResult).where(OCRResult.document_id == source.id))
            engine_name = source_ocr.engine if source_ocr else "duplicate_reuse"
            raw_text = source_ocr.raw_text if source_ocr else ""
            ocr_result = db.scalar(select(OCRResult).where(OCRResult.document_id == document_id))
            if ocr_result:
                ocr_result.engine, ocr_result.raw_text = engine_name, raw_text
            else:
                db.add(OCRResult(document_id=document_id, engine=engine_name, raw_text=raw_text))
            existing_record = db.scalar(
                select(Record).where(
                    Record.document_id == document_id, Record.template_id == document.template_id
                )
            )
            if existing_record:
                existing_record.json_data, existing_record.status = record.json_data, "completed"
            else:
                db.add(Record(
                    document_id=document_id,
                    template_id=document.template_id,
                    json_data=record.json_data,
                    status="completed",
                ))
            document.status, document.error_message = "completed", None
            log(db, document_id, "duplicate", "info",
                f"内容与 {source_label}（任务#{source.id}）完全相同，已复用其文字和提取结果，未调用 OCR/AI")
            db.commit()
            logger.info("document=%s reused the result of document=%s", document_id, source.id)
            return

        if not source:
            log(db, document_id, "duplicate", "warning",
                f"重复源任务已不存在（{source_label}），改为完整重新处理")
        elif source.status in {"completed", "failed", "skipped"}:
            log(db, document_id, "duplicate", "warning",
                f"{source_label}（任务#{source.id}，状态 {source.status}）没有可复用的同模板结果，改为完整重新处理")
        else:
            # The source task is still queued or in progress; check again next poll.
            return
        document.status = "pending"
        db.commit()


def resolve_duplicates():
    with SessionLocal() as db:
        waiting_ids = list(db.scalars(
            select(Document.id)
            .where(Document.status == "duplicate_waiting")
            .order_by(Document.created_at)
        ).all())
    for document_id in waiting_ids:
        try:
            resolve_duplicate(document_id)
        except Exception:
            logger.exception("document=%s duplicate resolution failed", document_id)


def process_batch(claims: list[tuple[int, str]], ocr_executor: ThreadPoolExecutor):
    batch_started_at = time.monotonic()
    logger.info("Starting batch: %s documents, preparation concurrency=%s, model concurrency=%s",
                len(claims), OCR_CONCURRENCY, AI_CONCURRENCY)
    ocr_completed: list[tuple[int, str]] = []
    futures = [ocr_executor.submit(extract_ocr, document_id, claim_token) for document_id, claim_token in claims]
    for future in as_completed(futures):
        document_id = future.result()
        if document_id:
            claim_token = next(token for claimed_id, token in claims if claimed_id == document_id)
            ocr_completed.append((document_id, claim_token))

    logger.info("OCR batch completed: %s/%s documents; starting model batch", len(ocr_completed), len(claims))
    with ThreadPoolExecutor(max_workers=AI_CONCURRENCY, thread_name_prefix="ai") as executor:
        futures = [
            executor.submit(extract_with_model, document_id, claim_token, batch_started_at)
            for document_id, claim_token in ocr_completed
        ]
        for future in as_completed(futures):
            future.result()
    logger.info("Batch completed in %.2fs", time.monotonic() - batch_started_at)
    # Duplicates of files from this very batch can now reuse what was just written.
    resolve_duplicates()


def run():
    # 上一轮可能异常退出，先把 WAL 折回主库，再开始领取任务。
    checkpoint_sqlite()
    with ThreadPoolExecutor(max_workers=OCR_CONCURRENCY, thread_name_prefix="ocr") as ocr_executor:
        while True:
            resolve_duplicates()
            claims = claim_pending_batch()
            if claims:
                process_batch(claims, ocr_executor)
            else:
                time.sleep(WORKER_POLL_SECONDS)


if __name__ == "__main__":
    run()
