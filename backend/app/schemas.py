from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


FieldType = Literal["text", "number", "date", "boolean"]
ExtractionType = Literal["ai_extract", "calculated"]


class TemplateFieldInput(BaseModel):
    field_name: str = Field(min_length=1, max_length=120)
    field_key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=80)
    field_type: FieldType
    description: str = ""
    required: bool = False
    sort_order: int = 0
    extraction_type: ExtractionType = "ai_extract"
    calc_rule: str | None = None
    source_field_key: str | None = None
    calc_params: str | None = None


class TemplateInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    fields: list[TemplateFieldInput] = []

    @field_validator("fields")
    @classmethod
    def unique_keys(cls, fields: list[TemplateFieldInput]):
        if len({field.field_key for field in fields}) != len(fields):
            raise ValueError("field_key must be unique within a template")
        return fields


class TemplateOutput(TemplateInput):
    id: int
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


# 跨机器导入模板时，同名模板的处理策略：整体覆盖、跳过、另存为新名字。
ConflictStrategy = Literal["update", "skip", "rename"]


class TemplateImportInput(BaseModel):
    templates: list[TemplateInput] = Field(min_length=1)
    on_conflict: ConflictStrategy = "update"


class RecordUpdate(BaseModel):
    json_data: dict[str, Any]


class LoginInput(BaseModel):
    username: str
    password: str
