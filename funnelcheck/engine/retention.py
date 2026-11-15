"""按本地日历周期计算队列留存。

与漏斗模块相同，计算拆成用户级贡献（可缓存）与全局聚合两层，
保证全量重跑与增量复用经过完全一致的聚合语义。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..models import Event
from ..spec import AnalysisSpecification
from ..timeutil import (
    eligible_on,
    get_zone,
    period_index,
    period_label,
    to_local_date,
)
from .order import canonical_key, group_by_user


@dataclass(frozen=True, slots=True)
class CohortMember:
    user_id: str
    cohort_date: date
    return_periods: frozenset[int]
    segments: dict[str, str]


@dataclass(frozen=True, slots=True)
class RetentionContribution:
    """单个用户对留存的贡献，JSON 可序列化以便增量缓存复用。"""

    user_id: str
    cohort_date: str
    return_periods: tuple[int, ...]
    segments: dict[str, str]

    def as_member(self) -> CohortMember:
        return CohortMember(
            user_id=self.user_id,
            cohort_date=date.fromisoformat(self.cohort_date),
            return_periods=frozenset(self.return_periods),
            segments=dict(self.segments),
        )


def _is_retained(member: CohortMember, period: int, mode: str) -> bool:
    if mode == "cumulative":
        return any(1 <= item <= period for item in member.return_periods)
    return period in member.return_periods


def _period_rows(
    members: list[CohortMember],
    periods: list[int],
    unit: str,
    mode: str,
    observation_date: date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for period in periods:
        eligible = [
            member
            for member in members
            if eligible_on(observation_date, member.cohort_date, period, unit)
        ]
        retained = sum(1 for member in eligible if _is_retained(member, period, mode))
        rows.append(
            {
                "period": period,
                "label": period_label(period, unit),
                "unit": unit,
                "eligible_users": len(eligible),
                "retained_users": retained,
                "retention_rate": round(retained / len(eligible), 6) if eligible else None,
            }
        )
    return rows


def _aggregate(
    members: list[CohortMember],
    periods: list[int],
    unit: str,
    mode: str,
    observation_date: date,
) -> dict[str, Any]:
    return {
        "cohort_size": len(members),
        "periods": _period_rows(members, periods, unit, mode, observation_date),
    }


def user_retention_contribution(
    user_id: str,
    events: list[Event],
    spec: AnalysisSpecification,
    *,
    observation_date: date | None = None,
) -> RetentionContribution | None:
    """计算单个用户的留存贡献；没有有效 cohort 事件时返回 None。"""
    retention_spec = spec.retention
    zone = get_zone(spec.analysis.timezone)
    fields = [item.field for item in spec.segments]
    max_period = max(retention_spec.periods)
    if observation_date is None:
        observation_date = to_local_date(
            spec.analysis.as_of
            + timedelta(seconds=spec.analysis.future_grace_seconds),
            zone,
        )
    ordered = sorted(
        events,
        key=lambda item: canonical_key(item, spec.events.same_timestamp_order),
    )
    cohort_event = next(
        (item for item in ordered if item.event_name == retention_spec.cohort_event),
        None,
    )
    if cohort_event is None:
        return None
    cohort_date = to_local_date(cohort_event.occurred_at, zone)
    if cohort_date > observation_date:
        return None
    return_periods: set[int] = set()
    for item in ordered:
        if item.event_name != retention_spec.return_event:
            continue
        if item.occurred_at < cohort_event.occurred_at:
            continue
        index = period_index(
            to_local_date(item.occurred_at, zone), cohort_date, retention_spec.unit
        )
        if index is not None and 1 <= index <= max_period:
            return_periods.add(index)
    return RetentionContribution(
        user_id=user_id,
        cohort_date=cohort_date.isoformat(),
        return_periods=tuple(sorted(return_periods)),
        segments={field: cohort_event.segment_value(field) for field in fields},
    )


def retention_contributions(
    events: Iterable[Event],
    spec: AnalysisSpecification,
    *,
    observation_date: date | None = None,
) -> list[RetentionContribution]:
    """全量路径：对每个用户计算留存贡献。"""
    grouped = group_by_user(events)
    contributions: list[RetentionContribution] = []
    for user_id in sorted(grouped):
        contribution = user_retention_contribution(
            user_id,
            grouped[user_id],
            spec,
            observation_date=observation_date,
        )
        if contribution is not None:
            contributions.append(contribution)
    return contributions


def aggregate_retention(
    contributions: list[RetentionContribution],
    spec: AnalysisSpecification,
) -> dict[str, Any]:
    """从（可来自缓存的）用户级贡献汇总完整留存报告。"""
    retention_spec = spec.retention
    periods = list(retention_spec.periods)
    unit = retention_spec.unit
    mode = retention_spec.mode
    zone = get_zone(spec.analysis.timezone)
    observation_at = spec.analysis.as_of + timedelta(
        seconds=spec.analysis.future_grace_seconds
    )
    observation_date = to_local_date(observation_at, zone)
    members = [contribution.as_member() for contribution in contributions]
    overall = _aggregate(members, periods, unit, mode, observation_date)

    cohorts: list[dict[str, Any]] = []
    by_date: dict[date, list[CohortMember]] = defaultdict(list)
    for member in members:
        by_date[member.cohort_date].append(member)
    for cohort_date in sorted(by_date):
        aggregate = _aggregate(by_date[cohort_date], periods, unit, mode, observation_date)
        cohorts.append(
            {
                "date": cohort_date.isoformat(),
                "cohort_size": aggregate["cohort_size"],
                "periods": {
                    row["label"]: {
                        "eligible_users": row["eligible_users"],
                        "retained_users": row["retained_users"],
                        "retention_rate": row["retention_rate"],
                    }
                    for row in aggregate["periods"]
                },
            }
        )

    by_segment: dict[str, list[dict[str, Any]]] = {}
    for field in [item.field for item in spec.segments]:
        values: dict[str, list[CohortMember]] = defaultdict(list)
        for member in members:
            values[member.segments[field]].append(member)
        rows: list[dict[str, Any]] = []
        for value in sorted(values):
            aggregate = _aggregate(values[value], periods, unit, mode, observation_date)
            rows.append({"value": value, **aggregate})
        by_segment[field] = rows

    return {
        "cohort_event": retention_spec.cohort_event,
        "return_event": retention_spec.return_event,
        "unit": unit,
        "mode": mode,
        "cohort_size": overall["cohort_size"],
        "periods": overall["periods"],
        "cohorts": cohorts,
        "by_segment": by_segment,
        "methodology": {
            "cohort": "每个用户首次发生的 cohort_event",
            "return": "cohort 日期之后命中的 return_event，按本地日历归入周期",
            "eligibility": "周期完整结束后的次日才进入分母",
            "observation_cutoff": observation_date.isoformat(),
            "same_timestamp_order": spec.events.same_timestamp_order,
        },
    }


def compute_retention(
    events: Iterable[Event], spec: AnalysisSpecification
) -> dict[str, Any]:
    return aggregate_retention(retention_contributions(events, spec), spec)
