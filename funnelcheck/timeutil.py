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


def _month_ordinal(year: int, month: int) -> int:
    """把年月转成可做整数加减的序号，避免直接对日号做月运算。"""
    return year * 12 + (month - 1)


def add_months(year: int, month: int, count: int) -> tuple[int, int]:
    """按自然月平移年月，返回 ``(year, month)``；不涉及日号，避免月末漂移。"""
    year_index, month_index = divmod(_month_ordinal(year, month) + count, 12)
    return year_index, month_index + 1


def period_span_days(period: int, unit: str) -> int:
    if unit == "week":
        return period * 7
    if unit == "month":
        raise ValueError("自然月周期没有固定天数，请按本地日历月边界计算")
    return period


def period_label(period: int, unit: str) -> str:
    prefix = {"week": "W", "month": "M"}.get(unit, "D")
    return f"{prefix}{period}"


def period_mature_on(cohort_date: date, period: int, unit: str) -> date:
    """周期完整可观察的首个本地日期。

    ``day``/``week`` 为周期结束后次日；``month`` 为目标自然月最后一天结束后的
    次日，即目标月下一个月的第一天。后者不假定固定天数，因而 28/29/30/31 天
    月份与闰年二月都会得到各自的成熟日期。
    """
    if unit == "month":
        year, month = add_months(cohort_date.year, cohort_date.month, period + 1)
        return date(year, month, 1)
    return cohort_date + timedelta(days=period_span_days(period, unit) + 1)


def eligible_on(as_of_date: date, cohort_date: date, period: int, unit: str) -> bool:
    """周期结束后的次日才视为完整可观察。"""
    return as_of_date >= period_mature_on(cohort_date, period, unit)


def period_index(return_date: date, cohort_date: date, unit: str) -> int | None:
    if unit == "month":
        # 先由调用方把时间戳转成本地日期，这里只比较年月序号。
        # 同月回访为 M0（忽略），跨月按月份差归入 M1/M2…。
        index = _month_ordinal(return_date.year, return_date.month) - _month_ordinal(
            cohort_date.year, cohort_date.month
        )
        return index if index >= 1 else None
    days = (return_date - cohort_date).days
    if days <= 0:
        return None
    index = days // 7 if unit == "week" else days
    return index if index >= 1 else None
