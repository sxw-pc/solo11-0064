"""声明式有序漏斗计算，含逐步转化耗时与右删失估计。"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from ..config import parse_duration
from ..models import (
    DurationObservation,
    Event,
    StepHit,
    UserPath,
    format_duration_seconds,
    scalar_token,
)
from ..spec import AnalysisSpecification, StepSpec
from ..timeutil import iso_utc
from .order import canonical_key, group_by_user

# 观察时点取窗口的固定比例；均为二进制精确小数，避免时点本身引入浮点误差。
_TIME_POINT_FRACTIONS = (0.25, 0.5, 0.75, 1.0)
_EPSILON = 1e-9


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
    as_of: datetime,
) -> UserPath | None:
    """按排序键扫描并记录每个成功步骤的命中事件与累计耗时。

    - 每个步骤必须消耗一个不同事件；扫描只向前推进，因此后一步的排序键
      严格晚于前一步。
    - 窗口锚定首步事件；``occurred_at`` 恰好落在端点时算转化。
    - 观察截止为 ``min(as_of, 首步 occurred_at + window)``；截止之后不再匹配，
      未完成的路径按右删失处理，而不是记为 0 耗时或直接丢弃。
    """
    ordered = sorted(events, key=lambda item: canonical_key(item, order))
    state = 0
    start: datetime | None = None
    first_event: Event | None = None
    hits: list[StepHit] = []

    for event in ordered:
        if state >= len(steps):
            break
        # 观察截止是 min(as_of, 首步 + window)；as_of 之后的事件即使落在
        # normalize 的未来宽限内，也不再参与漏斗匹配。
        if event.occurred_at > as_of:
            break
        if state == 0:
            if not _matches(event, steps[state]):
                continue
            start = event.occurred_at
            first_event = event
            hits.append(StepHit(step_index=1, event=event, elapsed_seconds=0.0))
            state = 1
            continue

        assert start is not None
        elapsed = (event.occurred_at - start).total_seconds()
        if elapsed > window.total_seconds():
            # 同刻事件仍由 canonical_key 决定先后；发生时间超出窗口即停止。
            break
        if not _matches(event, steps[state]):
            continue
        hits.append(
            StepHit(step_index=state + 1, event=event, elapsed_seconds=elapsed)
        )
        state += 1

    if first_event is None:
        return None

    window_end = first_event.occurred_at + window
    observation_cutoff = min(as_of, window_end)
    return UserPath(
        user_id=first_event.user_id,
        reached=len(hits),
        first_event=first_event,
        hits=tuple(hits),
        observation_cutoff=observation_cutoff,
        window_censored=window_end <= as_of,
    )


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _ratio(value: float | None) -> float | None:
    """概率/比例统一保留 6 位；None 继续表示“无法估计”。"""
    if value is None:
        return None
    return round(float(value), 6)


def _seconds(value: float | None) -> int | float | None:
    """输出用的秒数：整秒给 int，其余保留 6 位小数。"""
    if value is None:
        return None
    rounded = round(float(value), 6)
    return int(rounded) if rounded.is_integer() else rounded


def _quantile(values: list[float], probability: float) -> float | None:
    """完成者样本的线性插值分位数；样本为空时返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _observations(
    paths: list[UserPath], source_step: int
) -> list[DurationObservation]:
    """构造 source_step → source_step+1 的耗时观测（未完成者右删失）。"""
    observations: list[DurationObservation] = []
    for path in paths:
        if path.reached < source_step:
            continue
        previous = path.hits[source_step - 1]
        if path.reached >= source_step + 1:
            following = path.hits[source_step]
            duration = following.elapsed_seconds - previous.elapsed_seconds
            observations.append(
                DurationObservation(duration_seconds=duration, converted=True)
            )
            continue
        observed = (
            path.observation_cutoff - previous.event.occurred_at
        ).total_seconds()
        observations.append(
            DurationObservation(
                duration_seconds=max(observed, 0.0),
                converted=False,
                censored_reason="window" if path.window_censored else "observation",
            )
        )
    return observations


def _survival_curve(
    observations: list[DurationObservation],
) -> list[dict[str, Any]]:
    """Kaplan–Meier 乘积极限；同刻先处理转化，再移出删失。"""
    buckets: dict[float, list[int]] = {}
    for observation in observations:
        bucket = buckets.setdefault(observation.duration_seconds, [0, 0])
        bucket[0 if observation.converted else 1] += 1

    remaining = len(observations)
    survival = 1.0
    converted_by = 0
    censored_by = 0
    curve: list[dict[str, Any]] = []
    for elapsed in sorted(buckets):
        converted, censored = buckets[elapsed]
        at_risk = remaining
        if converted and at_risk:
            survival *= 1.0 - converted / at_risk
        converted_by += converted
        censored_by += censored
        curve.append(
            {
                "time": elapsed,
                "at_risk": at_risk,
                "converted": converted,
                "censored": censored,
                "survival": survival,
                "converted_by": converted_by,
                "censored_by": censored_by,
            }
        )
        remaining -= converted + censored
    return curve


def _survival_at(
    curve: list[dict[str, Any]], observations: list[DurationObservation], elapsed: float
) -> tuple[float | None, int, int, int]:
    """任意时点的 (KM 仍未转化比例, 风险集, 累计已转化, 累计删失)。

    风险集为空表示该时点已无可观察用户，比例记为 ``None``；累计计数仍
    如实给出此前发生的转化与删失。
    """
    at_risk = sum(
        1 for item in observations if item.duration_seconds >= elapsed - _EPSILON
    )
    survival = 1.0
    converted_by = 0
    censored_by = 0
    for row in curve:
        if row["time"] <= elapsed + _EPSILON:
            if row["converted"] and row["at_risk"]:
                survival *= 1.0 - row["converted"] / row["at_risk"]
            converted_by = row["converted_by"]
            censored_by = row["censored_by"]
    return (survival if at_risk else None), at_risk, converted_by, censored_by


def _survival_median(curve: list[dict[str, Any]]) -> float | None:
    for row in curve:
        if row["survival"] <= 0.5 + 1e-12:
            return row["time"]
    return None


def _time_points(
    window_seconds: int, horizon_seconds: float | None
) -> list[dict[str, Any]]:
    """给出若干可观察时点。

    名义窗口是 ``funnel.window``，但实际可观察范围还会被观察截止（as_of）
    压缩。时点取实际可观察范围的 25%/50%/75%/100%，保证每个时点都有
    风险集可查；无任何观测时按名义窗口给出，比例留空。
    """
    span = (
        float(window_seconds)
        if horizon_seconds is None
        else min(float(window_seconds), max(float(horizon_seconds), 0.0))
    )
    return [
        {
            "elapsed_seconds": _seconds(span * fraction),
            "label": format_duration_seconds(span * fraction),
            "fraction": fraction,
            "window_fraction": round((span * fraction) / window_seconds, 6)
            if window_seconds
            else None,
        }
        for fraction in _TIME_POINT_FRACTIONS
    ]


def _transition_payload(
    paths: list[UserPath],
    steps: list[StepSpec],
    source_step: int,
    window_seconds: int,
) -> dict[str, Any]:
    observations = _observations(paths, source_step)
    converted = [item for item in observations if item.converted]
    censored = [item for item in observations if not item.converted]
    durations = [item.duration_seconds for item in converted]
    curve = _survival_curve(observations)
    # 时点只覆盖真正被观察到的范围（含删失观测的截止时长）；这保证最后一个
    # 时点至少仍有一名用户在风险集中，而不是对外推的未来空集报比例。
    horizon = max(
        (item.duration_seconds for item in observations), default=None
    )
    points = _time_points(window_seconds, horizon)

    point_rows: list[dict[str, Any]] = []
    for point in points:
        elapsed = float(point["elapsed_seconds"])
        survival, at_risk, converted_by, censored_by = _survival_at(
            curve, observations, elapsed
        )
        point_rows.append(
            {
                "elapsed_seconds": point["elapsed_seconds"],
                "label": point["label"],
                "fraction": point["fraction"],
                "window_fraction": point["window_fraction"],
                "at_risk_users": at_risk,
                "converted_by": converted_by,
                "censored_by": censored_by,
                "not_converted_ratio": _ratio(survival),
            }
        )

    observed_median = _quantile(durations, 0.5)
    survival_median = _survival_median(curve)
    previous_step = steps[source_step - 1]
    next_step = steps[source_step]
    return {
        "from_step": source_step,
        "to_step": source_step + 1,
        "from_name": previous_step.name,
        "to_name": next_step.name,
        "from_event": previous_step.event,
        "to_event": next_step.event,
        "entered_users": len(observations),
        "converted_users": len(converted),
        "censored_users": len(censored),
        "censored_window_users": sum(
            1 for item in censored if item.censored_reason == "window"
        ),
        "censored_observation_users": sum(
            1 for item in censored if item.censored_reason == "observation"
        ),
        "conversion_rate": _rate(len(converted), len(observations)),
        "observed_quantiles_seconds": {
            "min": _seconds(_quantile(durations, 0.0)),
            "p25": _seconds(_quantile(durations, 0.25)),
            "p50": _seconds(observed_median),
            "p75": _seconds(_quantile(durations, 0.75)),
            "max": _seconds(_quantile(durations, 1.0)),
        },
        "observed_median_seconds": _seconds(observed_median),
        "observed_median_label": format_duration_seconds(observed_median),
        "survival": {
            "method": "kaplan_meier",
            "censoring_tie_order": "converted_before_censored",
            "median_seconds": _seconds(survival_median),
            "median_label": format_duration_seconds(survival_median),
            "points": point_rows,
        },
    }


def _step_payload(
    counts: list[int],
    steps: list[StepSpec],
    order: str,
    transitions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous = 0
    entered = counts[0] if counts else 0
    for index, step in enumerate(steps):
        users = counts[index] if index < len(counts) else 0
        previous_rate = _rate(users, previous) if index > 0 else _rate(users, entered)
        row: dict[str, Any] = {
            "index": index + 1,
            "name": step.name,
            "event": step.event,
            "where": dict(step.where),
            "users": users,
            "conversion_from_first": _rate(users, entered),
            "conversion_from_previous": previous_rate,
            "dropoff_from_previous": max(previous - users, 0) if index > 0 else 0,
        }
        if transitions is not None and index > 0:
            # 第 2 步起给出一份精简摘要；完整曲线见 duration.transitions，
            # 避免在 steps 与 duration 中重复大块数据。
            transition = transitions[index - 1]
            row["time_to_convert"] = {
                "from_step": transition["from_step"],
                "to_step": transition["to_step"],
                "observed_median_seconds": transition["observed_median_seconds"],
                "observed_median_label": transition["observed_median_label"],
                "survival_median_seconds": transition["survival"]["median_seconds"],
                "survival_median_label": transition["survival"]["median_label"],
                "converted_users": transition["converted_users"],
                "censored_users": transition["censored_users"],
            }
        rows.append(row)
        previous = users
    return rows


def _segment_payload(
    outcomes: list[UserPath],
    steps: list[StepSpec],
    fields: list[str],
    order: str,
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        values: dict[str, list[int]] = {}
        for outcome in outcomes:
            value = outcome.first_event.segment_value(field)
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


def _duration_methodology(
    window_seconds: int, as_of: datetime, order: str
) -> dict[str, Any]:
    return {
        "unit": "seconds",
        "population": "到达上一步的每个用户；未完成者按右删失纳入",
        "event_time": "下一步命中事件与上一步命中事件的 occurred_at 之差",
        "step_pairing": "每个步骤消耗不同事件，后一步排序键严格晚于前一步",
        "window_anchor": "首步命中事件的 occurred_at",
        "window_boundary": "occurred_at 差 <= window 算转化，端点含；之后算删失",
        "observation_cutoff": "min(as_of, 首步 occurred_at + funnel.window)",
        "as_of_boundary": (
            "as_of 之后的事件即使落在 future_grace 内也不参与漏斗匹配，"
            "否则会在观察截止之后记录“转化”"
        ),
        "censoring": "窗口结束或观察截止时仍未到达下一步，计为右删失而非零耗时",
        "censoring_tie_order": "同一 elapsed 的转化先进入风险集，删失随后移出",
        "estimator": "Kaplan-Meier 乘积极限；中位数取 S(t) <= 0.5 的最早时点，否则 null",
        "observed_quantiles": "仅完成者的线性插值分位数，不能代表含删失的总体",
        "time_points": (
            "取该转换实际观察到的最长时长（不超过 funnel.window）的 "
            "25%/50%/75%/100%；window_fraction 标注它在名义窗口中的位置"
        ),
        "same_timestamp_order": order,
        "window_seconds": window_seconds,
        "as_of": iso_utc(as_of),
    }


def compute_funnel(
    events: Iterable[Event], spec: AnalysisSpecification
) -> dict[str, Any]:
    funnel_spec = spec.funnel
    window_seconds = parse_duration(funnel_spec.window, field="funnel.window")
    window = timedelta(seconds=window_seconds)
    grouped = group_by_user(events)
    outcomes: list[UserPath] = []
    for user_id in sorted(grouped):
        outcome = _match_user(
            grouped[user_id],
            funnel_spec.steps,
            window,
            spec.events.same_timestamp_order,
            spec.analysis.as_of,
        )
        if outcome is not None:
            outcomes.append(outcome)

    counts = [0] * len(funnel_spec.steps)
    for outcome in outcomes:
        for index in range(outcome.reached):
            counts[index] += 1
    entered = counts[0]
    converted = counts[-1]
    fields = [item.field for item in spec.segments]

    transitions = [
        _transition_payload(outcomes, funnel_spec.steps, index, window_seconds)
        for index in range(1, len(funnel_spec.steps))
    ]
    return {
        "name": funnel_spec.name,
        "window": funnel_spec.window,
        "window_seconds": window_seconds,
        "entered_users": entered,
        "converted_users": converted,
        "overall_conversion": _rate(converted, entered),
        "steps": _step_payload(
            counts,
            funnel_spec.steps,
            spec.events.same_timestamp_order,
            transitions,
        ),
        "by_segment": _segment_payload(
            outcomes, funnel_spec.steps, fields, spec.events.same_timestamp_order
        ),
        "duration": {
            "transitions": transitions,
            "methodology": _duration_methodology(
                window_seconds, spec.analysis.as_of, spec.events.same_timestamp_order
            ),
        },
        "methodology": {
            "sequence": "按步骤顺序匹配，每个步骤至少消耗一个匹配事件",
            "window_scope": "从首个步骤事件起算，含端点",
            "event_order": spec.events.same_timestamp_order,
            "segment_assignment": "取命中首个步骤的事件字段",
            "window_seconds": window_seconds,
            "observation_cutoff": "min(as_of, 首步事件 + 窗口)",
        },
    }
