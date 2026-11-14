"""时间解析、时区边界与 DST 规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class ParsedTime:
    value: datetime | None
    reason: str | None = None
    ambiguous: bool = False


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"未知时区：{name!r}") from exc


def _resolve_naive(naive: datetime, zone: ZoneInfo) -> tuple[datetime, bool, bool]:
    """返回 (UTC 时间, 是否歧义, 是否不存在)。默认歧义取较早的 fold=0。"""
    early = naive.replace(tzinfo=zone, fold=0)
    late = naive.replace(tzinfo=zone, fold=1)
    if early.utcoffset() == late.utcoffset():
        return early.astimezone(UTC), False, False
    early_round = early.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    late_round = late.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    nonexistent = early_round != naive and late_round != naive
    if nonexistent:
        return early.astimezone(UTC), False, True
    return early.astimezone(UTC), True, False


def parse_timestamp(value: object, zone: ZoneInfo) -> ParsedTime:
    """将 ISO-8601 时间转为 UTC；无偏移时间按分析时区解释。"""
    if not isinstance(value, str) or not value.strip():
        return ParsedTime(None, "timestamp_missing")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ParsedTime(None, "timestamp_format")
    if parsed.tzinfo is None:
        resolved, ambiguous, nonexistent = _resolve_naive(parsed, zone)
        if nonexistent:
            return ParsedTime(None, "timestamp_nonexistent_local")
        return ParsedTime(resolved, None, ambiguous)
    return ParsedTime(parsed.astimezone(UTC))


def as_utc(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        resolved, _ambiguous, nonexistent = _resolve_naive(value, zone)
        if nonexistent:
            raise ValueError("as_of 落在不存在的本地时间")
        return resolved
    return value.astimezone(UTC)


def to_local_date(value: datetime, zone: ZoneInfo) -> date:
    return value.astimezone(zone).date()


def start_of_local_day(value: date, zone: ZoneInfo) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=zone)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return iso_utc(datetime.now(UTC))


def period_span_days(period: int, unit: str) -> int:
    if unit == "week":
        return period * 7
    return period


def period_label(period: int, unit: str) -> str:
    return f"{'W' if unit == 'week' else 'D'}{period}"


def eligible_on(as_of_date: date, cohort_date: date, period: int, unit: str) -> bool:
    """周期结束后的次日才视为完整可观察。"""
    span = period_span_days(period, unit)
    return as_of_date >= cohort_date + timedelta(days=span + 1)


def period_index(return_date: date, cohort_date: date, unit: str) -> int | None:
    days = (return_date - cohort_date).days
    if days <= 0:
        return None
    index = days // 7 if unit == "week" else days
    return index if index >= 1 else None
