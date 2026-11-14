"""原始 JSONL 行到规范化事件的质量处理管线。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from .config import sha256_json
from .engine.dedup import deduplicate
from .engine.order import canonical_key
from .engine.quality import QualityTracker
from .models import Event, NormalizedBatch, RawInput, RawRow, is_scalar
from .spec import AnalysisSpecification
from .timeutil import get_zone, iso_utc, parse_timestamp

_REQUIRED = ("event_id", "user_id", "event_name", "occurred_at", "received_at")


def _text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _attributes(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    attributes = {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and key != "properties" and is_scalar(value)
    }
    properties = payload.get("properties")
    if properties is None:
        attributes["properties"] = {}
        return attributes, False
    if not isinstance(properties, dict):
        attributes["properties"] = {}
        return attributes, True
    clean: dict[str, Any] = {}
    invalid = False
    for key, value in properties.items():
        if isinstance(key, str) and is_scalar(value):
            clean[key] = value
        else:
            invalid = True
    attributes["properties"] = clean
    return attributes, invalid


def _parse_row(
    row: RawRow, spec: AnalysisSpecification, tracker: QualityTracker
) -> Event | None:
    assert row.payload is not None
    payload = row.payload
    missing = [name for name in _REQUIRED if _text(payload, name) is None]
    if missing:
        tracker.increment("missing_required")
        tracker.sample(
            "missing_required",
            line_no=row.line_no,
            event_id=_text(payload, "event_id"),
            detail=",".join(missing),
        )
        return None
    zone = get_zone(spec.analysis.timezone)
    parsed_occurred = parse_timestamp(payload["occurred_at"], zone)
    parsed_received = parse_timestamp(payload["received_at"], zone)
    bad_fields = [
        name
        for name, parsed in (
            ("occurred_at", parsed_occurred),
            ("received_at", parsed_received),
        )
        if parsed.value is None
    ]
    if bad_fields:
        tracker.increment("invalid_timestamp")
        tracker.sample(
            "invalid_timestamp",
            line_no=row.line_no,
            event_id=_text(payload, "event_id"),
            detail=",".join(bad_fields),
        )
        return None
    assert parsed_occurred.value is not None and parsed_received.value is not None
    attributes, invalid_properties = _attributes(payload)
    flags: list[str] = []
    if parsed_occurred.ambiguous or parsed_received.ambiguous:
        flags.append("ambiguous_local_time")
    if invalid_properties:
        flags.append("invalid_properties")
    return Event(
        event_id=_text(payload, "event_id") or "",
        user_id=_text(payload, "user_id") or "",
        event_name=_text(payload, "event_name") or "",
        occurred_at=parsed_occurred.value,
        received_at=parsed_received.value,
        attributes=attributes,
        line_no=row.line_no,
        flags=tuple(flags),
    )


def _included_hash(events: list[Event], order: str) -> str:
    ordered = sorted(
        events,
        key=lambda item: (
            item.user_id,
            canonical_key(item, order),
        ),
    )
    return sha256_json([item.canonical_payload() for item in ordered])


def _rules(spec: AnalysisSpecification) -> dict[str, Any]:
    return {
        "as_of": iso_utc(spec.analysis.as_of),
        "timezone": spec.analysis.timezone,
        "future_grace_seconds": spec.analysis.future_grace_seconds,
        "late_after_seconds": spec.analysis.late_after_seconds,
        "late_policy": spec.analysis.late_policy,
        "dedup_key": spec.events.dedup_key,
        "dedup_keep": spec.events.dedup_keep,
        "same_timestamp_order": spec.events.same_timestamp_order,
        "future_rule": "occurred_at > as_of + future_grace 时排除",
        "late_rule": "received_at - occurred_at > late_after 时按策略处理",
    }


def normalize_batch(raw: RawInput, spec: AnalysisSpecification) -> NormalizedBatch:
    tracker = QualityTracker()
    tracker.increment("records_seen", len(raw.rows))
    tracker.increment("blank_lines", raw.blank_lines)
    candidates: list[Event] = []
    for row in raw.rows:
        if not row.ok:
            tracker.increment("malformed_json")
            tracker.sample("malformed_json", line_no=row.line_no)
            continue
        tracker.increment("records_parsed")
        event = _parse_row(row, spec, tracker)
        if event is not None:
            candidates.append(event)

    deduped = deduplicate(
        candidates,
        key=spec.events.dedup_key,
        keep=spec.events.dedup_keep,
    )
    tracker.increment("duplicate_rows", deduped.duplicate_rows)
    tracker.increment("duplicate_groups", deduped.duplicate_groups)
    for event in deduped.events:
        if "invalid_properties" in event.flags:
            tracker.increment("invalid_properties")
            tracker.sample(
                "invalid_properties",
                line_no=event.line_no,
                event_id=event.event_id,
            )
        if "ambiguous_local_time" in event.flags:
            tracker.increment("ambiguous_local_time")
            tracker.sample(
                "ambiguous_local_time",
                line_no=event.line_no,
                event_id=event.event_id,
                detail="按较早偏移(fold=0)解释",
            )

    as_of = spec.analysis.as_of
    future_threshold = as_of + timedelta(seconds=spec.analysis.future_grace_seconds)
    late_after = timedelta(seconds=spec.analysis.late_after_seconds)
    included: list[Event] = []
    included_users: set[str] = set()
    for event in deduped.events:
        flags = list(event.flags)
        exclude_reason: str | None = None
        if event.occurred_at > future_threshold:
            tracker.increment("future_rows")
            exclude_reason = "future_event"
        elif event.occurred_at > as_of:
            tracker.increment("future_within_grace")
            flags.append("future_within_grace")

        lag = event.received_at - event.occurred_at
        if lag < timedelta(0):
            tracker.increment("negative_latency")
        if lag > late_after:
            tracker.increment("late_rows")
            if spec.analysis.late_policy == "exclude":
                tracker.increment("late_excluded")
                if exclude_reason is None:
                    exclude_reason = "late_event"
            else:
                tracker.increment("late_included")
                flags.append("late_included")

        if exclude_reason is not None:
            tracker.sample(
                exclude_reason,
                line_no=event.line_no,
                event_id=event.event_id,
            )
            continue
        included.append(replace(event, flags=tuple(flags)))
        included_users.add(event.user_id)

    included.sort(
        key=lambda item: (item.user_id, canonical_key(item, spec.events.same_timestamp_order))
    )
    last_by_user: dict[str, Any] = {}
    for event in sorted(included, key=lambda item: item.line_no):
        previous = last_by_user.get(event.user_id)
        if previous is not None and event.occurred_at < previous:
            tracker.increment("out_of_order")
            tracker.sample(
                "out_of_order",
                line_no=event.line_no,
                event_id=event.event_id,
            )
        elif previous is None or event.occurred_at > previous:
            last_by_user[event.user_id] = event.occurred_at

    tracker.increment("included_events", len(included))
    tracker.increment("included_users", len(included_users))
    tracker.increment("excluded_events", len(raw.rows) - len(included))
    return NormalizedBatch(
        events=tuple(included),
        quality=tracker.finalize(_rules(spec)),
        normalized_sha256=_included_hash(included, spec.events.same_timestamp_order),
        raw_sha256=raw.sha256,
    )
