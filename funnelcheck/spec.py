"""分析规格的 Pydantic 模型、YAML 加载与语义校验。"""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from .config import MAX_SPEC_BYTES, canonical_json, parse_duration, sha256_text
from .errors import ErrorCode, FunnelCheckError, SpecError
from .models import SpecBundle, is_scalar
from .timeutil import as_utc, get_zone

_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StepSpec(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    event: str = Field(min_length=1, max_length=200)
    where: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "event")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("where")
    @classmethod
    def _check_where(cls, value: dict[str, Any]) -> dict[str, Any]:
        for path, expected in value.items():
            if not _PATH_RE.match(path):
                raise ValueError(f"where 字段路径无效：{path!r}")
            if not is_scalar(expected):
                raise ValueError(f"where.{path} 只支持标量比较值")
            if isinstance(expected, float) and not math.isfinite(expected):
                raise ValueError(f"where.{path} 不能是 NaN/Infinity")
        return value


class FunnelSpec(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    window: str = "7d"
    steps: list[StepSpec] = Field(min_length=2, max_length=20)

    @field_validator("window")
    @classmethod
    def _check_window(cls, value: str) -> str:
        parse_duration(value, field="funnel.window")
        return value

    @model_validator(mode="after")
    def _unique_names(self) -> "FunnelSpec":
        names = [step.name for step in self.steps]
        if len(names) != len(set(names)):
            raise ValueError("funnel.steps 的 name 必须唯一")
        return self


class RetentionSpec(StrictModel):
    cohort_event: str = Field(min_length=1, max_length=200)
    return_event: str = Field(min_length=1, max_length=200)
    unit: Literal["day", "week"] = "day"
    periods: list[int] = Field(min_length=1, max_length=30)
    mode: Literal["exact", "cumulative"] = "exact"

    @field_validator("periods", mode="before")
    @classmethod
    def _check_periods(cls, value: Any) -> list[int]:
        if not isinstance(value, list):
            raise ValueError("retention.periods 必须是列表")
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or item < 1 or item > 365:
                raise ValueError("retention.periods 必须为 1..365 的整数")
        if len(value) != len(set(value)):
            raise ValueError("retention.periods 不能重复")
        return sorted(value)


class SegmentSpec(StrictModel):
    field: str = Field(min_length=1, max_length=120)

    @field_validator("field")
    @classmethod
    def _check_field(cls, value: str) -> str:
        if not _PATH_RE.match(value):
            raise ValueError(f"segment.field 路径无效：{value!r}")
        return value


class EventRules(StrictModel):
    dedup_key: Literal["event_id", "content"] = "event_id"
    dedup_keep: Literal["first_received", "last_received", "first_line"] = "first_received"
    same_timestamp_order: Literal["received_then_id", "id_then_received"] = "received_then_id"


class AnalysisSettings(StrictModel):
    timezone: str = "UTC"
    as_of: datetime
    future_grace: str = "0h"
    late_after: str = "48h"
    late_policy: Literal["exclude", "include"] = "exclude"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            get_zone(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("future_grace", "late_after")
    @classmethod
    def _valid_duration(cls, value: str, info: Any) -> str:
        parse_duration(
            value,
            field=f"analysis.{info.field_name}",
            allow_zero=True,
        )
        return value

    @model_validator(mode="after")
    def _normalize_as_of(self) -> "AnalysisSettings":
        try:
            self.as_of = as_utc(self.as_of, get_zone(self.timezone))
        except ValueError as exc:
            raise ValueError(f"analysis.as_of 无效：{exc}") from exc
        return self

    @property
    def future_grace_seconds(self) -> int:
        return parse_duration(
            self.future_grace, field="analysis.future_grace", allow_zero=True
        )

    @property
    def late_after_seconds(self) -> int:
        return parse_duration(
            self.late_after, field="analysis.late_after", allow_zero=True
        )


class AnalysisSpecification(StrictModel):
    spec_version: Literal[1] = 1
    title: str = Field(default="漏斗与留存验算", min_length=1, max_length=120)
    analysis: AnalysisSettings
    events: EventRules = Field(default_factory=EventRules)
    funnel: FunnelSpec
    retention: RetentionSpec
    segments: list[SegmentSpec] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _unique_segments(self) -> "AnalysisSpecification":
        fields = [item.field for item in self.segments]
        if len(fields) != len(set(fields)):
            raise ValueError("segments 的 field 不能重复")
        return self


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒绝 YAML 中的重复键，避免静默覆盖。"""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"发现重复键 {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:5]:
        path = ".".join(str(item) for item in error.get("loc", ())) or "spec"
        message = str(error.get("msg", "无效值")).replace("Value error, ", "")
        parts.append(f"{path}: {message}")
    return "；".join(parts)


def load_spec(path: str | Path, *, max_bytes: int = MAX_SPEC_BYTES) -> SpecBundle:
    spec_path = Path(path)
    try:
        stat = spec_path.stat()
    except OSError as exc:
        raise FunnelCheckError(
            ErrorCode.IO, f"无法读取规格文件：{spec_path}", hint=str(exc)
        ) from exc
    if stat.st_size > max_bytes:
        raise FunnelCheckError(
            ErrorCode.SPEC_TOO_LARGE,
            f"规格文件超过 {max_bytes} 字节限制",
            hint="请删除无关内容或拆分规格。",
        )
    try:
        raw = spec_path.read_bytes()
    except OSError as exc:
        raise FunnelCheckError(ErrorCode.IO, f"无法读取规格：{spec_path}", hint=str(exc)) from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SpecError("规格文件不是有效 UTF-8", hint=str(exc)) from exc
    try:
        loaded = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise SpecError("YAML 解析失败", hint=str(exc)) from exc
    if not isinstance(loaded, dict):
        raise SpecError("规格根节点必须是映射对象")
    try:
        spec = AnalysisSpecification.model_validate(loaded)
    except ValidationError as exc:
        raise SpecError(_format_validation_error(exc), hint="请检查字段名、类型与枚举值。") from exc
    document = spec.model_dump(mode="json")
    return SpecBundle(
        spec=spec,
        document=document,
        sha256=sha256_text(canonical_json(document)),
        byte_count=len(raw),
    )
