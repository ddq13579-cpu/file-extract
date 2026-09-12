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
    """模型返回了内容但不符合约定；带上 usage，失败请求也能统计 Token 花费。"""

    def __init__(self, message: str, response_json: str, usage: "AIUsage | None" = None):
        super().__init__(message)
        self.response_json = response_json
        self.usage = usage


CODE_FENCE_PATTERN = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
FIELD_ENTRY_KEYS = ("field_key", "key", "name", "field")


def _flatten_field_entries(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """把 [{"field_key": "field_1", "value": "x"}, ...] 这种字段清单写法压平成对象。

    返回 None 表示这不是字段清单，交给调用方按别的形状处理。
    """
    data: dict[str, Any] = {}
    for entry in entries:
        key = next((entry[name] for name in FIELD_ENTRY_KEYS if isinstance(entry.get(name), str)), None)
        if key is None:
            return None
        if "value" in entry:
            data[key] = entry["value"]
        elif len(entry) == 1:
            data[key] = next(iter(entry.values()))
        else:
            return None
    return data or None


def parse_model_object(response_content: str, usage: AIUsage | None = None) -> dict[str, Any]:
    """把模型返回解析成 JSON 对象，容忍几种常见的跑偏写法。

    实测即使带了 response_format=json_object，模型仍会偶发返回数组：要么把正确
    对象裹进单元素数组，要么改成 [{"field_key": ..., "value": ...}] 的字段清单。
    这两种都已经包含完整识别结果（Token 也花掉了），直接判失败太浪费，所以先
    尝试还原成对象，还原不了才报错。
    """
    text = CODE_FENCE_PATTERN.sub("", response_content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ModelResponseError(f"Invalid JSON response: {error}", response_content, usage) from error

    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        flattened = _flatten_field_entries(data)
        if flattened is not None:
            return flattened
        if len(data) == 1:
            return data[0]
        raise ModelResponseError(
            f"The model returned a JSON array of {len(data)} objects instead of one object",
            response_content, usage,
        )
    raise ModelResponseError(
        f"The model response must be a JSON object, got {type(data).__name__}", response_content, usage
    )


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
        raw_usage = response.usage
        usage = AIUsage(
            actual_model=getattr(response, "model", None),
            prompt_tokens=getattr(raw_usage, "prompt_tokens", None) if raw_usage else None,
            candidates_tokens=getattr(raw_usage, "completion_tokens", None) if raw_usage else None,
            total_tokens=getattr(raw_usage, "total_tokens", None) if raw_usage else None,
        )
        response_content = response.choices[0].message.content
        if not response_content:
            # 空返回也是偶发的，按格式错误交给上层重试一次，同时把 Token 记下来。
            raise ModelResponseError("The model returned an empty response", "", usage)
        return AIExtraction(
            data=parse_model_object(response_content, usage),
            response_json=response_content,
            usage=usage,
        )

    @staticmethod
    def _build_prompt(fields: list[dict[str, Any]], raw_text: str) -> str:
        field_description = json.dumps(fields, ensure_ascii=False)
        source = raw_text or "请直接识别图片中的文字和内容。"
        current_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        # 给出确切的返回骨架：实测只说“返回 JSON 对象”时，模型仍会偶发返回数组
        # 或 [{"field_key":..., "value":...}] 清单，把示例写死能显著降低概率。
        skeleton = json.dumps(
            {field["field_key"]: None for field in fields if isinstance(field.get("field_key"), str)},
            ensure_ascii=False,
        )
        return (
            f"当前日期（北京时间）是 {current_date}。涉及年龄或日期计算时，必须以此日期为准。"
            "请根据文档内容提取结构化数据。"
            "严格要求：只返回一个 JSON 对象，不要返回数组，不要用 Markdown 代码块包裹，不要添加任何解释文字；"
            "键必须与字段定义完全一致，一个都不能多也不能少；"
            "数字类型的字段必须返回数字本身（例如 100 或 100.00），不要加引号、千分位或货币符号；"
            "日期类型的字段必须返回 YYYY-MM-DD 格式；"
            "没有值时使用 null，不要编造值。"
            f"\n返回格式示例（把 null 替换成实际值）：{skeleton}"
            f"\n\n字段定义如下：\n{field_description}\n\n文档文字：\n{source}"
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
