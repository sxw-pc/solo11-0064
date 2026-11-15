"""声明式有序漏斗计算。

计算分为两层，保证全量与增量路径共用同一套聚合语义：

* :func:`user_funnel_contribution` 只处理单个用户的事件，输出可缓存的用户级贡献；
* :func:`aggregate_funnel` 汇总所有用户级贡献，生成完整漏斗报告。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..config import parse_duration
from ..models import Event, scalar_token
from ..spec import AnalysisSpecification, StepSpec
from .order import canonical_key, group_by_user


@dataclass(frozen=True, slots=True)
class UserPath:
    reached: int
    first_event: Event


@dataclass(frozen=True, slots=True)
class FunnelContribution:
    """单个用户对漏斗的贡献，JSON 可序列化以便增量缓存复用。"""

    user_id: str
    reached: int
    first_event_payload: dict[str, Any]


def _matches(event: Event, step: StepSpec) -> bool:
    if event.event_name != step.event:
        return False
    for path, expected in step.where.items():
        if scalar_token(event.value_for(path)) != scalar_token(expected):
            return False
    return True


def _match_user(
    events: list[Event],
    steps: list[StepSpec],
    window: timedelta,
    order: str,
) -> UserPath | None:
    ordered = sorted(events, key=lambda item: canonical_key(item, order))
    state = 0
    start = None
    first_event: Event | None = None
    for event in ordered:
        if state >= len(steps):
            break
        if not _matches(event, steps[state]):
            continue
        if state == 0:
            start = event.occurred_at
            first_event = event
            state = 1
            continue
        assert start is not None
        if event.occurred_at - start > window:
            break
        state += 1
    if first_event is None or state == 0:
        return None
    return UserPath(reached=state, first_event=first_event)


def user_funnel_contribution(
    user_id: str,
    events: list[Event],
    spec: AnalysisSpecification,
) -> FunnelContribution | None:
    """计算单个用户的漏斗贡献；用户没有任何步骤命中时返回 None。"""
    funnel_spec = spec.funnel
    window_seconds = parse_duration(funnel_spec.window, field="funnel.window")
    window = timedelta(seconds=window_seconds)
    outcome = _match_user(
        events,
        funnel_spec.steps,
        window,
        spec.events.same_timestamp_order,
    )
    if outcome is None:
        return None
    return FunnelContribution(
        user_id=user_id,
        reached=outcome.reached,
        first_event_payload=outcome.first_event.canonical_payload(),
    )


def _payload_segments(payload: dict[str, Any], fields: list[str]) -> dict[str, str]:
    from ..models import MISSING_SEGMENT

    output: dict[str, str] = {}
    for field in fields:
        current: Any = payload.get("attributes", {})
        for part in field.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        token = scalar_token(current)
        output[field] = token if token else MISSING_SEGMENT
    return output


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _step_payload(
    counts: list[int], steps: list[StepSpec], order: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous = 0
    entered = counts[0] if counts else 0
    for index, step in enumerate(steps):
        users = counts[index] if index < len(steps) else 0
        previous_rate = _rate(users, previous) if index > 0 else _rate(users, entered)
        rows.append(
            {
                "index": index + 1,
                "name": step.name,
                "event": step.event,
                "where": dict(step.where),
                "users": users,
                "conversion_from_first": _rate(users, entered),
                "conversion_from_previous": previous_rate,
                "dropoff_from_previous": max(previous - users, 0) if index > 0 else 0,
            }
        )
        previous = users
    return rows


def _segment_payload(
    outcomes: list[FunnelContribution],
    steps: list[StepSpec],
    fields: list[str],
    order: str,
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        values: dict[str, list[int]] = {}
        for outcome in outcomes:
            value = _payload_segments(outcome.first_event_payload, [field])[field]
            counts = values.setdefault(value, [0] * len(steps))
            for index in range(outcome.reached):
                counts[index] += 1
        rows: list[dict[str, Any]] = []
        for value in sorted(values):
            counts = values[value]
            rows.append(
                {
                    "value": value,
                    "entered_users": counts[0],
                    "converted_users": counts[-1],
                    "overall_conversion": _rate(counts[-1], counts[0]),
                    "steps": _step_payload(counts, steps, order),
                }
            )
        output[field] = rows
    return output


def aggregate_funnel(
    contributions: list[FunnelContribution],
    spec: AnalysisSpecification,
) -> dict[str, Any]:
    """从（可来自缓存的）用户级贡献汇总完整漏斗报告。"""
    funnel_spec = spec.funnel
    window_seconds = parse_duration(funnel_spec.window, field="funnel.window")
    outcomes = sorted(contributions, key=lambda item: item.user_id)

    counts = [0] * len(funnel_spec.steps)
    for outcome in outcomes:
        for index in range(outcome.reached):
            counts[index] += 1
    entered = counts[0]
    converted = counts[-1]
    fields = [item.field for item in spec.segments]
    return {
        "name": funnel_spec.name,
        "window": funnel_spec.window,
        "window_seconds": window_seconds,
        "entered_users": entered,
        "converted_users": converted,
        "overall_conversion": _rate(converted, entered),
        "steps": _step_payload(counts, funnel_spec.steps, spec.events.same_timestamp_order),
        "by_segment": _segment_payload(
            outcomes, funnel_spec.steps, fields, spec.events.same_timestamp_order
        ),
        "methodology": {
            "sequence": "按步骤顺序匹配，每个步骤至少消耗一个匹配事件",
            "window_scope": "从首个步骤事件起算，含端点",
            "event_order": spec.events.same_timestamp_order,
            "segment_assignment": "取命中首个步骤的事件字段",
            "window_seconds": window_seconds,
        },
    }


def funnel_contributions(
    events: Iterable[Event], spec: AnalysisSpecification
) -> list[FunnelContribution]:
    """全量路径：对每个用户计算漏斗贡献。"""
    grouped = group_by_user(events)
    contributions: list[FunnelContribution] = []
    for user_id in sorted(grouped):
        contribution = user_funnel_contribution(user_id, grouped[user_id], spec)
        if contribution is not None:
            contributions.append(contribution)
    return contributions


def compute_funnel(
    events: Iterable[Event], spec: AnalysisSpecification
) -> dict[str, Any]:
    return aggregate_funnel(funnel_contributions(events, spec), spec)
