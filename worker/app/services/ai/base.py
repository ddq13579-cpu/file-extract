from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AIUsage:
    actual_model: str | None
    prompt_tokens: int | None
    candidates_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class AIExtraction:
    data: dict[str, Any]
    usage: AIUsage
    response_json: str


class AIProvider(ABC):
    @abstractmethod
    def extract(
        self,
        raw_text: str,
        fields: list[dict[str, Any]],
        document_path: Path | None = None,
    ) -> AIExtraction:
        """Extract strictly the requested fields from plain document text."""
