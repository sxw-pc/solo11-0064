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


def format_duration_seconds(value: float | None) -> str:
    """把秒数格式化为便于审阅的短标签；不参与计算与哈希。"""
    if value is None:
        return "—"
    seconds = float(value)
    if seconds < 60:
        return f"{seconds:g}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.2f}d"


@dataclass(frozen=True, slots=True)
class StepHit:
    """某用户按排序键依次命中的一个漏斗步骤。"""

    step_index: int
    event: Event
    elapsed_seconds: float  # 相对首步 occurred_at 的差值


@dataclass(frozen=True, slots=True)
class UserPath:
    """一个用户的漏斗路径与右删失观察边界。

    ``observation_cutoff`` = min(as_of, 首步 occurred_at + funnel.window)。
    normalize 已排除 as_of 之后的事件，因此该截止表达的是“观察到哪里”，
    而不是“用户明确失败”。
    """

    user_id: str
    reached: int
    first_event: Event
    hits: tuple[StepHit, ...]
    observation_cutoff: datetime
    window_censored: bool


@dataclass(frozen=True, slots=True)
class DurationObservation:
    """一次“上一步 → 下一步”的耗时观测；未完成者为右删失。"""

    duration_seconds: float
    converted: bool
    censored_reason: str | None = None  # window | observation


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
