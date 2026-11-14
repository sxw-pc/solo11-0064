"""按本地日历周期计算队列留存。"""

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


def _members(
    events: Iterable[Event], spec: AnalysisSpecification, observation_date: date
) -> list[CohortMember]:
    spec_retention = spec.retention
    zone = get_zone(spec.analysis.timezone)
    grouped = group_by_user(events)
    fields = [item.field for item in spec.segments]
    max_period = max(spec_retention.periods)
    members: list[CohortMember] = []
    for user_id in sorted(grouped):
        ordered = sorted(
            grouped[user_id],
            key=lambda item: canonical_key(item, spec.events.same_timestamp_order),
        )
        cohort_event = next(
            (item for item in ordered if item.event_name == spec_retention.cohort_event),
            None,
        )
        if cohort_event is None:
            continue
        cohort_date = to_local_date(cohort_event.occurred_at, zone)
        if cohort_date > observation_date:
            continue
        return_periods: set[int] = set()
        for item in ordered:
            if item.event_name != spec_retention.return_event:
                continue
            if item.occurred_at < cohort_event.occurred_at:
                continue
            index = period_index(to_local_date(item.occurred_at, zone), cohort_date, spec_retention.unit)
            if index is not None and 1 <= index <= max_period:
                return_periods.add(index)
        members.append(
            CohortMember(
                user_id=user_id,
                cohort_date=cohort_date,
                return_periods=frozenset(return_periods),
                segments={
                    field: cohort_event.segment_value(field) for field in fields
                },
            )
        )
    return members


def compute_retention(
    events: Iterable[Event], spec: AnalysisSpecification
) -> dict[str, Any]:
    retention_spec = spec.retention
    periods = list(retention_spec.periods)
    unit = retention_spec.unit
    mode = retention_spec.mode
    zone = get_zone(spec.analysis.timezone)
    observation_at = spec.analysis.as_of + timedelta(
        seconds=spec.analysis.future_grace_seconds
    )
    observation_date = to_local_date(observation_at, zone)
    members = _members(events, spec, observation_date)
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
