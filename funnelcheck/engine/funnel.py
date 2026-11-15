"""声明式有序漏斗计算，支持可配置的复入归属口径。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..config import parse_duration
from ..models import Event, scalar_token
from ..spec import AnalysisSpecification, StepSpec
from .order import canonical_key, group_by_user

ENTRY_MODE_FIRST = "first_entry"
ENTRY_MODE_EVERY = "every_entry"

ENTRY_MODE_LABELS = {
    ENTRY_MODE_FIRST: "每用户只认首次入口",
    ENTRY_MODE_EVERY: "每次入口单独计数",
}

_ENTRY_RULES = {
    ENTRY_MODE_FIRST: (
        "每用户只认首次入口：以该用户首个命中步骤 1 的事件为唯一入口；"
        "窗口内未完成不重新进入，后续命中步骤 1 的事件被忽略。"
    ),
    ENTRY_MODE_EVERY: (
        "每次入口单独计数：每个命中步骤 1 的事件都开启一条独立尝试；"
        "每个事件至多被一条尝试消耗（先开启的尝试优先），"
        "窗口之外的后续事件不补入已超时的旧尝试。"
    ),
}

_SEGMENT_RULES = {
    ENTRY_MODE_FIRST: "取命中首个步骤的事件字段",
    ENTRY_MODE_EVERY: "每条尝试按其入口事件字段归属；同一用户的不同尝试可进入不同分群",
}

_RATE_DENOMINATORS = (
    "conversion_from_first/conversion_from_previous 以尝试数为分母；"
    "user_conversion_from_first/user_conversion_from_previous 以去重用户数为分母"
)


@dataclass(frozen=True, slots=True)
class UserPath:
    user_id: str
    reached: int
    first_event: Event


@dataclass(slots=True)
class _Attempt:
    """every_entry 模式下的一条进行中尝试。"""

    user_id: str
    state: int
    start: datetime
    first_event: Event


def _matches(event: Event, step: StepSpec) -> bool:
    if event.event_name != step.event:
        return False
    for path, expected in step.where.items():
        if scalar_token(event.value_for(path)) != scalar_token(expected):
            return False
    return True


def _match_user_first_entry(
    user_id: str,
    events: list[Event],
    steps: list[StepSpec],
    window: timedelta,
    order: str,
) -> list[UserPath]:
    """每用户只认首次入口：最早命中步骤 1 的事件是唯一入口，不重试。"""
    ordered = sorted(events, key=lambda item: canonical_key(item, order))
    state = 0
    start: datetime | None = None
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
        return []
    return [UserPath(user_id=user_id, reached=state, first_event=first_event)]


def _match_user_every_entry(
    user_id: str,
    events: list[Event],
    steps: list[StepSpec],
    window: timedelta,
    order: str,
) -> list[UserPath]:
    """每次入口单独计数：每个命中步骤 1 的事件开启一条独立尝试。

    消耗规则（确定性）：
    - 命中步骤 1 的事件总是开启新尝试，并被该尝试消耗；
    - 其余事件按扫描顺序提供给仍在窗口内的未完成尝试，先开启者优先，
      每个事件至多被一条尝试消耗一次；
    - 事件按 occurred_at 单调扫描，尝试一旦超出窗口即终结，
      窗口外事件不会补入旧尝试。
    """
    ordered = sorted(events, key=lambda item: canonical_key(item, order))
    open_attempts: list[_Attempt] = []
    finished: list[_Attempt] = []
    for event in ordered:
        still_open: list[_Attempt] = []
        for attempt in open_attempts:
            if event.occurred_at - attempt.start > window:
                finished.append(attempt)
            else:
                still_open.append(attempt)
        open_attempts = still_open
        if _matches(event, steps[0]):
            open_attempts.append(
                _Attempt(
                    user_id=user_id,
                    state=1,
                    start=event.occurred_at,
                    first_event=event,
                )
            )
            continue
        for attempt in open_attempts:
            if attempt.state >= len(steps):
                continue
            if not _matches(event, steps[attempt.state]):
                continue
            attempt.state += 1
            if attempt.state >= len(steps):
                finished.append(attempt)
                open_attempts.remove(attempt)
            break
    finished.extend(open_attempts)
    return [
        UserPath(user_id=attempt.user_id, reached=attempt.state, first_event=attempt.first_event)
        for attempt in finished
    ]


def _match_user(
    user_id: str,
    events: list[Event],
    steps: list[StepSpec],
    window: timedelta,
    order: str,
    entry_mode: str,
) -> list[UserPath]:
    if entry_mode == ENTRY_MODE_EVERY:
        return _match_user_every_entry(user_id, events, steps, window, order)
    return _match_user_first_entry(user_id, events, steps, window, order)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _step_payload(
    attempt_counts: list[int],
    user_counts: list[int],
    steps: list[StepSpec],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_attempts = 0
    previous_users = 0
    entered_attempts = attempt_counts[0] if attempt_counts else 0
    entered_users = user_counts[0] if user_counts else 0
    for index, step in enumerate(steps):
        attempts = attempt_counts[index] if index < len(attempt_counts) else 0
        users = user_counts[index] if index < len(user_counts) else 0
        rows.append(
            {
                "index": index + 1,
                "name": step.name,
                "event": step.event,
                "where": dict(step.where),
                "attempts": attempts,
                "users": users,
                "conversion_from_first": _rate(attempts, entered_attempts),
                "conversion_from_previous": _rate(attempts, previous_attempts)
                if index > 0
                else _rate(attempts, entered_attempts),
                "user_conversion_from_first": _rate(users, entered_users),
                "user_conversion_from_previous": _rate(users, previous_users)
                if index > 0
                else _rate(users, entered_users),
                "dropoff_from_previous": max(previous_attempts - attempts, 0)
                if index > 0
                else 0,
            }
        )
        previous_attempts = attempts
        previous_users = users
    return rows


def _reached_counts(outcomes: list[UserPath], step_total: int) -> tuple[list[int], list[int]]:
    """按尝试与按去重用户两个口径统计每步到达数。"""
    attempt_counts = [0] * step_total
    user_best: dict[str, int] = {}
    for outcome in outcomes:
        for index in range(outcome.reached):
            attempt_counts[index] += 1
        if outcome.reached > user_best.get(outcome.user_id, 0):
            user_best[outcome.user_id] = outcome.reached
    user_counts = [0] * step_total
    for reached in user_best.values():
        for index in range(reached):
            user_counts[index] += 1
    return attempt_counts, user_counts


def _segment_payload(
    outcomes: list[UserPath],
    steps: list[StepSpec],
    fields: list[str],
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        grouped: dict[str, list[UserPath]] = {}
        for outcome in outcomes:
            value = outcome.first_event.segment_value(field)
            grouped.setdefault(value, []).append(outcome)
        rows: list[dict[str, Any]] = []
        for value in sorted(grouped):
            attempt_counts, user_counts = _reached_counts(grouped[value], len(steps))
            rows.append(
                {
                    "value": value,
                    "attempts": attempt_counts[0],
                    "entered_users": user_counts[0],
                    "converted_attempts": attempt_counts[-1],
                    "converted_users": user_counts[-1],
                    "overall_conversion": _rate(user_counts[-1], user_counts[0]),
                    "attempt_conversion": _rate(attempt_counts[-1], attempt_counts[0]),
                    "steps": _step_payload(attempt_counts, user_counts, steps),
                }
            )
        output[field] = rows
    return output


def compute_funnel(
    events: Iterable[Event], spec: AnalysisSpecification
) -> dict[str, Any]:
    funnel_spec = spec.funnel
    entry_mode = funnel_spec.entry_mode
    window_seconds = parse_duration(funnel_spec.window, field="funnel.window")
    window = timedelta(seconds=window_seconds)
    grouped = group_by_user(events)
    outcomes: list[UserPath] = []
    for user_id in sorted(grouped):
        outcomes.extend(
            _match_user(
                user_id,
                grouped[user_id],
                funnel_spec.steps,
                window,
                spec.events.same_timestamp_order,
                entry_mode,
            )
        )

    attempt_counts, user_counts = _reached_counts(outcomes, len(funnel_spec.steps))
    entered_attempts = attempt_counts[0]
    entered_users = user_counts[0]
    converted_attempts = attempt_counts[-1]
    converted_users = user_counts[-1]
    fields = [item.field for item in spec.segments]
    return {
        "name": funnel_spec.name,
        "window": funnel_spec.window,
        "window_seconds": window_seconds,
        "entry_mode": entry_mode,
        "attempts": entered_attempts,
        "entered_users": entered_users,
        "converted_attempts": converted_attempts,
        "converted_users": converted_users,
        "overall_conversion": _rate(converted_users, entered_users),
        "attempt_conversion": _rate(converted_attempts, entered_attempts),
        "steps": _step_payload(attempt_counts, user_counts, funnel_spec.steps),
        "by_segment": _segment_payload(outcomes, funnel_spec.steps, fields),
        "methodology": {
            "sequence": "按步骤顺序匹配，每个步骤至少消耗一个匹配事件",
            "window_scope": "从首个步骤事件起算，含端点",
            "event_order": spec.events.same_timestamp_order,
            "entry_mode": entry_mode,
            "entry_mode_label": ENTRY_MODE_LABELS[entry_mode],
            "entry_rule": _ENTRY_RULES[entry_mode],
            "segment_assignment": _SEGMENT_RULES[entry_mode],
            "rate_denominators": _RATE_DENOMINATORS,
            "window_seconds": window_seconds,
        },
    }
