import json
import re
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo

ID_PATTERN = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")


def calculate_template_fields(data: dict[str, Any], fields: Sequence[Any]) -> dict[str, Any]:
    """
    Given extraction data (from AI or prior step) and template fields,
    compute calculated fields in multi-pass until stable.
    """
    result = dict(data)

    calculated_fields = [
        f for f in fields
        if getattr(f, "extraction_type", "ai_extract") == "calculated"
    ]
    if not calculated_fields:
        return result

    for _ in range(len(calculated_fields)):
        changed = False
        for field in calculated_fields:
            key = getattr(field, "field_key", None)
            if not key:
                continue
            rule = getattr(field, "calc_rule", None)
            source_key = getattr(field, "source_field_key", None)
            params_raw = getattr(field, "calc_params", None)

            if not rule:
                continue

            params = {}
            if isinstance(params_raw, str) and params_raw.strip():
                try:
                    params = json.loads(params_raw)
                except Exception:
                    params = {}
            elif isinstance(params_raw, dict):
                params = params_raw

            source_val = result.get(source_key) if source_key else None
            computed_val = None

            field_type = getattr(field, "field_type", "text")
            if rule == "age_from_id":
                computed_val = _calc_age_from_id(source_val, field_type)
            elif rule == "range_from_value":
                computed_val = _calc_range_from_value(source_val, params)
            elif rule == "value_map":
                computed_val = _calc_value_map(source_val, params, field_type)

            if computed_val is not None and result.get(key) != computed_val:
                result[key] = computed_val
                changed = True

        if not changed:
            break

    for field in calculated_fields:
        key = getattr(field, "field_key", None)
        if key and key not in result:
            result[key] = None

    return result


def _calc_age_from_id(source_val: Any, field_type: str) -> int | str | None:
    if source_val is None:
        return None
    val_str = str(source_val)
    match = ID_PATTERN.search(val_str)
    if not match:
        return None
    digits = match.group(1)
    try:
        birth_year = int(digits[6:10])
        birth_month = int(digits[10:12])
        birth_day = int(digits[12:14])
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()

        if not (1 <= birth_month <= 12 and 1 <= birth_day <= 31):
            return None

        age = today.year - birth_year - (1 if (today.month, today.day) < (birth_month, birth_day) else 0)
        if age < 0:
            return None
        return age if field_type == "number" else str(age)
    except Exception:
        return None


def _calc_range_from_value(source_val: Any, params: dict[str, Any]) -> str | None:
    if source_val is None:
        return None
    try:
        val = float(source_val)
    except (ValueError, TypeError):
        return None

    threshold = float(params.get("threshold", 5))
    gt_val = str(params.get("gt_value", "大"))
    lte_val = str(params.get("lte_value", "小"))

    if val > threshold:
        return gt_val
    else:
        return lte_val


def _calc_value_map(source_val: Any, params: dict[str, Any], field_type: str) -> Any:
    if source_val is None:
        return None
    mapping = params.get("map", {"小": 18, "大": 44})
    if not isinstance(mapping, dict):
        mapping = {"小": 18, "大": 44}

    key = str(source_val).strip()
    mapped = mapping.get(key)

    if mapped is None:
        mapped = params.get("default")

    if mapped is None:
        return None

    if field_type == "number":
        try:
            if isinstance(mapped, (int, float)):
                return mapped
            val = float(mapped)
            return int(val) if val.is_integer() else val
        except (ValueError, TypeError):
            return None
    elif field_type == "text":
        return str(mapped)
    elif field_type == "boolean":
        return bool(mapped)
    return mapped
