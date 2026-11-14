"""唯一的事件排序规则，供漏斗与留存共用。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..models import Event


def canonical_key(event: Event, same_timestamp_order: str) -> tuple:
    if same_timestamp_order == "id_then_received":
        tie = (event.event_id, event.received_at)
    else:
        tie = (event.received_at, event.event_id)
    return (event.occurred_at, *tie, event.line_no)


def events_for_user(
    events: Iterable[Event], user_id: str, same_timestamp_order: str
) -> list[Event]:
    selected = [item for item in events if item.user_id == user_id]
    return sorted(selected, key=lambda item: canonical_key(item, same_timestamp_order))


def group_by_user(events: Iterable[Event]) -> dict[str, list[Event]]:
    grouped: dict[str, list[Event]] = {}
    for event in events:
        grouped.setdefault(event.user_id, []).append(event)
    return grouped


def max_timestamp(values: Iterable[datetime]) -> datetime | None:
    return max(values, default=None)
