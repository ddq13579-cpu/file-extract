import base64
import json
import mimetypes
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionUserMessageParam,
)
from PIL import Image
from pillow_heif import register_heif_opener

from ...config import API_KEY, BASE_URL, MODEL
from .base import AIExtraction, AIProvider, AIUsage

ID_PATTERN = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
ID_CHECK_CODES = "10X98765432"
register_heif_opener()


def _is_valid_id_number(value: str) -> bool:
    digits = value.upper()
    if not ID_PATTERN.fullmatch(digits):
        return True
    checksum = sum(int(digit) * weight for digit, weight in zip(digits[:17], ID_WEIGHTS)) % 11
    return digits[-1] == ID_CHECK_CODES[checksum]


def _invalid_id_numbers(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, list):
        values = value
    elif isinstance(value, str):
        return [match.group(1) for match in ID_PATTERN.finditer(value) if not _is_valid_id_number(match.group(1))]
    else:
        return []
    invalid: list[str] = []
    for item in values:
        invalid.extend(_invalid_id_numbers(item))
    return invalid


def invalid_id_numbers(value: Any) -> list[str]:
    return _invalid_id_numbers(value)


class ModelResponseError(ValueError):
    def __init__(self, message: str, response_json: str):
        super().__init__(message)
        self.response_json = response_json


class OpenAICompatibleProvider(AIProvider):
    def __init__(self):
        if not API_KEY:
            raise RuntimeError("API_KEY is not configured")
        if not MODEL:
            raise RuntimeError("MODEL is not configured")
        # BASE_URL 未配置时交给 SDK 使用自身默认地址，避免空字符串导致构造失败。
        self.client = OpenAI(api_key=API_KEY, base_url=BASE_URL or None)

    def extract(
        self,
        raw_text: str,
        fields: list[dict[str, Any]],
        document_path: Path | None = None,
    ) -> AIExtraction:
        prompt = self._build_prompt(fields, raw_text)
        model = MODEL
        request_message: ChatCompletionUserMessageParam
        if document_path:
            content: list[ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam] = [
                ChatCompletionContentPartTextParam(type="text", text=prompt),
                ChatCompletionContentPartImageParam(
                    type="image_url",
                    image_url={"url": self._image_data_url(document_path)},
                ),
            ]
            request_message = ChatCompletionUserMessageParam(role="user", content=content)
        else:
            request_message = ChatCompletionUserMessageParam(role="user", content=prompt)

        response = self.client.chat.completions.create(
            model=model,
            messages=[request_message],
            response_format={"type": "json_object"},
        )
        response_content = response.choices[0].message.content
        if not response_content:
            raise RuntimeError("The model returned an empty response")
        try:
            data = json.loads(response_content)
        except json.JSONDecodeError as error:
            raise ModelResponseError(f"Invalid JSON response: {error}", response_content) from error
        if not isinstance(data, dict):
            raise ModelResponseError("The model response must be a JSON object", response_content)
        usage = response.usage
        return AIExtraction(
            data=data,
            response_json=response_content,
            usage=AIUsage(
                actual_model=getattr(response, "model", None),
                prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
                candidates_tokens=getattr(usage, "completion_tokens", None) if usage else None,
                total_tokens=getattr(usage, "total_tokens", None) if usage else None,
            ),
        )

    @staticmethod
    def _build_prompt(fields: list[dict[str, Any]], raw_text: str) -> str:
        field_description = json.dumps(fields, ensure_ascii=False)
        source = raw_text or "请直接识别图片中的文字和内容。"
        current_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        return (
            f"当前日期（北京时间）是 {current_date}。涉及年龄或日期计算时，必须以此日期为准。"
            "请根据文档内容提取结构化数据。只返回 JSON 对象，键必须与字段定义完全一致；"
            "没有值时使用 null，不要编造值。字段定义如下：\n"
            f"{field_description}\n\n文档文字：\n{source}"
        )

    @staticmethod
    def _image_data_url(path: Path) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        image_bytes = path.read_bytes()
        if path.suffix.lower() == ".heic":
            with Image.open(BytesIO(image_bytes)) as image:
                output = BytesIO()
                image.save(output, format="PNG")
                image_bytes = output.getvalue()
            mime_type = "image/png"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"
