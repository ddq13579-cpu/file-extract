from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(1024))
    file_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duplicate_of_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Template(Base):
    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))


class TemplateField(Base):
    __tablename__ = "template_fields"
    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    field_name: Mapped[str] = mapped_column(String(120))
    field_key: Mapped[str] = mapped_column(String(80))
    field_type: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(Text)
    required: Mapped[bool] = mapped_column(Boolean)
    sort_order: Mapped[int] = mapped_column(Integer)
    extraction_type: Mapped[str] = mapped_column(String(20), default="ai_extract")
    calc_rule: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_field_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    calc_params: Mapped[str | None] = mapped_column(Text, nullable=True)


class OCRResult(Base):
    __tablename__ = "ocr_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    engine: Mapped[str] = mapped_column(String(40))
    raw_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Record(Base):
    __tablename__ = "records"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    json_data: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProcessingLog(Base):
    __tablename__ = "processing_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    stage: Mapped[str] = mapped_column(String(60))
    level: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidates_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    request_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
