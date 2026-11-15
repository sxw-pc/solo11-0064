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


def content_key(event: Event) -> str:
    """content 去重模式的规范化完整事件键（不含 event_id）。"""
    return _content_key(event)


def group_key_for(event: Event, key: str) -> str:
    """去重组标识：event_id 模式用事件 ID，content 模式用规范化完整事件键。"""
    if key == "event_id":
        return event.event_id
    return _content_key(event)


def deduplicate(events: list[Event], *, key: str, keep: str) -> DedupResult:
    groups: dict[str, list[Event]] = {}
    for event in events:
        groups.setdefault(group_key_for(event, key), []).append(event)

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
