"""按显式键与保留策略去重。"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import canonical_json
from ..models import Event


@dataclass(frozen=True, slots=True)
class DedupResult:
    events: tuple[Event, ...]
    duplicate_rows: int
    duplicate_groups: int


def _content_key(event: Event) -> str:
    payload = event.canonical_payload()
    payload.pop("event_id", None)
    attributes = payload.get("attributes")
    if isinstance(attributes, dict):
        attributes.pop("event_id", None)
    return canonical_json(payload)


def _choose(events: list[Event], keep: str) -> Event:
    if keep == "first_received":
        return min(events, key=lambda item: (item.received_at, item.line_no))
    if keep == "last_received":
        return max(events, key=lambda item: (item.received_at, item.line_no))
    return min(events, key=lambda item: item.line_no)


def deduplicate(events: list[Event], *, key: str, keep: str) -> DedupResult:
    groups: dict[str, list[Event]] = {}
    for event in events:
        group_key = event.event_id if key == "event_id" else _content_key(event)
        groups.setdefault(group_key, []).append(event)

    kept: list[Event] = []
    duplicate_rows = 0
    duplicate_groups = 0
    for group in groups.values():
        kept.append(_choose(group, keep))
        if len(group) > 1:
            duplicate_groups += 1
            duplicate_rows += len(group) - 1
    kept.sort(key=lambda item: item.line_no)
    return DedupResult(
        events=tuple(kept),
        duplicate_rows=duplicate_rows,
        duplicate_groups=duplicate_groups,
    )
