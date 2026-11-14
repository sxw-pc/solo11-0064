"""跨模块共享的事件与输入数据模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .timeutil import iso_utc

SCALAR_TYPES = (str, int, float, bool)
MISSING_SEGMENT = "(missing)"


def is_scalar(value: object) -> bool:
    return value is None or isinstance(value, SCALAR_TYPES)


def scalar_token(value: object) -> str:
    """把声明值与事件值归一为可比较文本，避免 1 与 "1" 的歧义。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    user_id: str
    event_name: str
    occurred_at: datetime
    received_at: datetime
    attributes: Mapping[str, Any]
    line_no: int
    flags: tuple[str, ...] = ()

    def value_for(self, path: str) -> Any:
        """支持 channel、properties.plan 等字段路径。"""
        current: Any = self.attributes
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        return current

    def segment_value(self, path: str) -> str:
        value = self.value_for(path)
        token = scalar_token(value)
        return token if token else MISSING_SEGMENT

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "user_id": self.user_id,
            "event_name": self.event_name,
            "occurred_at": iso_utc(self.occurred_at),
            "received_at": iso_utc(self.received_at),
            "attributes": _sorted_mapping(self.attributes),
        }


def _sorted_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in sorted(value):
        item = value[key]
        if isinstance(item, Mapping):
            output[key] = _sorted_mapping(item)
        elif is_scalar(item):
            output[key] = item
    return output


@dataclass(frozen=True, slots=True)
class RawRow:
    line_no: int
    payload: dict[str, Any] | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.payload is not None and self.error is None


@dataclass(frozen=True, slots=True)
class RawInput:
    rows: tuple[RawRow, ...]
    byte_count: int
    sha256: str
    line_count: int
    blank_lines: int


@dataclass(frozen=True, slots=True)
class NormalizedBatch:
    events: tuple[Event, ...]
    quality: dict[str, Any]
    normalized_sha256: str
    raw_sha256: str


@dataclass(frozen=True, slots=True)
class SpecBundle:
    spec: Any
    document: dict[str, Any]
    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    artifact_dir: Path
    report: dict[str, Any]
    written: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CompareOutcome:
    comparison_id: str
    artifact_dir: Path
    comparison: dict[str, Any]
    written: tuple[str, ...] = field(default_factory=tuple)
