"""自然月留存：月长、闰年、跨年、时区月界与 M0 语义。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from funnelcheck.timeutil import (
    eligible_on,
    get_zone,
    period_index,
    period_label,
    period_mature_on,
    period_span_days,
    to_local_date,
)
from tests.support import make_event, run_once

UTC = timezone.utc


def month_spec(**overrides):
    retention = {
        "cohort_event": "signup",
        "return_event": "view",
        "unit": "month",
        "periods": [1, 2, 3],
        "mode": "exact",
    }
    retention.update(overrides)
    return {"retention": retention}


def _periods(report: dict) -> dict[str, dict]:
    return {row["label"]: row for row in report["retention"]["periods"]}


def _local(value: str, zone_name: str) -> date:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return to_local_date(moment, get_zone(zone_name))


# --- 纯时间工具：月长与闰年 ---


def test_month_labels_and_no_fixed_span() -> None:
    assert period_label(1, "day") == "D1"
    assert period_label(1, "week") == "W1"
    assert period_label(1, "month") == "M1"
    assert period_label(12, "month") == "M12"
    assert period_span_days(3, "day") == 3
    assert period_span_days(2, "week") == 14
    with pytest.raises(ValueError):
        period_span_days(1, "month")


@pytest.mark.parametrize(
    ("cohort", "expected"),
    [
        # 28/29/30/31 天的目标自然月，成熟日都是次月 1 日，而不是固定 30 天后。
        (date(2026, 1, 31), date(2026, 3, 1)),   # M1 目标 2 月（28 天）
        (date(2024, 1, 31), date(2024, 3, 1)),   # M1 目标 2 月（29 天，闰年）
        (date(2026, 3, 15), date(2026, 5, 1)),   # M1 目标 4 月（30 天）
        (date(2026, 5, 15), date(2026, 7, 1)),   # M1 目标 6 月（30 天）
        (date(2026, 7, 15), date(2026, 9, 1)),   # M1 目标 8 月（31 天）
        (date(2026, 12, 20), date(2027, 2, 1)),  # 跨年 M1 目标 1 月
    ],
)
def test_month_maturity_uses_calendar_month_end(cohort: date, expected: date) -> None:
    assert period_mature_on(cohort, 1, "month") == expected
    assert eligible_on(expected - date.resolution, cohort, 1, "month") is False
    assert eligible_on(expected, cohort, 1, "month") is True


def test_month_lengths_differ_by_cohort_month_and_leap_year() -> None:
    january = date(2026, 1, 31)
    february = date(2026, 2, 10)
    # 1 月 cohort 的 M1 覆盖 2 月，2 月 cohort 的 M1 覆盖 3 月，长度不同。
    assert period_mature_on(january, 1, "month") == date(2026, 3, 1)
    assert period_mature_on(february, 1, "month") == date(2026, 4, 1)
    # 闰日属于 1 月 cohort 的 M1，且只有闰年存在。
    assert period_index(date(2024, 2, 29), date(2024, 1, 31), "month") == 1
    assert period_index(date(2025, 2, 28), date(2025, 1, 31), "month") == 1
    assert period_index(date(2026, 3, 31), date(2026, 1, 31), "month") == 2


def test_month_index_ignores_same_month_and_orders_by_year() -> None:
    cohort = date(2025, 12, 20)
    assert period_index(date(2025, 12, 31), cohort, "month") is None  # M0
    assert period_index(date(2025, 12, 20), cohort, "month") is None
    assert period_index(date(2026, 1, 1), cohort, "month") == 1
    assert period_index(date(2026, 2, 28), cohort, "month") == 2
    assert period_index(date(2027, 1, 1), cohort, "month") == 13


# --- 引擎集成：M0 忽略、exact/cumulative、跨年 ---


def test_same_month_return_is_m0_not_m1(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2026-01-10T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-01-31T23:00:00Z"),
    ]
    report = run_once(
        tmp_path,
        events,
        analysis={"as_of": "2026-04-01T00:00:00Z"},
        **month_spec(periods=[1]),
    ).report
    periods = _periods(report)
    assert report["retention"]["unit"] == "month"
    assert periods["M1"]["eligible_users"] == 1
    assert periods["M1"]["retained_users"] == 0

    cumulative = run_once(
        tmp_path,
        events,
        name="cumulative",
        analysis={"as_of": "2026-04-01T00:00:00Z"},
        **month_spec(periods=[1], mode="cumulative"),
    ).report
    assert _periods(cumulative)["M1"]["retained_users"] == 0


def test_exact_and_cumulative_month_periods(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2026-01-10T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-01-20T00:00:00Z"),  # M0 忽略
        make_event("r2", "u1", "view", "2026-02-10T00:00:00Z"),  # M1
        make_event("r3", "u1", "view", "2026-04-05T00:00:00Z"),  # M3
        make_event("s2", "u2", "signup", "2026-01-15T00:00:00Z"),
        make_event("r4", "u2", "view", "2026-03-01T00:00:00Z"),  # M2
        make_event("s3", "u3", "signup", "2026-02-20T00:00:00Z"),
        make_event("r5", "u3", "view", "2026-03-01T00:00:00Z"),  # 2 月 cohort 的 M1
    ]
    exact = run_once(
        tmp_path,
        events,
        analysis={"as_of": "2026-06-01T00:00:00Z"},
        **month_spec(periods=[1, 2, 3]),
    ).report
    exact_periods = _periods(exact)
    assert exact["retention"]["cohort_size"] == 3
    assert [row["label"] for row in exact["retention"]["periods"]] == ["M1", "M2", "M3"]
    assert exact_periods["M1"]["eligible_users"] == 3
    assert exact_periods["M1"]["retained_users"] == 2  # u1、u3
    assert exact_periods["M2"]["eligible_users"] == 3
    assert exact_periods["M2"]["retained_users"] == 1  # u2
    assert exact_periods["M3"]["eligible_users"] == 3
    assert exact_periods["M3"]["retained_users"] == 1  # u1

    cumulative = run_once(
        tmp_path,
        events,
        name="cumulative",
        analysis={"as_of": "2026-06-01T00:00:00Z"},
        **month_spec(periods=[1, 2, 3], mode="cumulative"),
    ).report
    cumulative_periods = _periods(cumulative)
    assert cumulative_periods["M1"]["retained_users"] == 2
    assert cumulative_periods["M2"]["retained_users"] == 3
    assert cumulative_periods["M3"]["retained_users"] == 3


def test_cross_year_month_periods_and_maturity(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2025-12-20T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-01-05T00:00:00Z"),  # M1
        make_event("r2", "u1", "view", "2026-02-28T00:00:00Z"),  # M2
    ]
    report = run_once(
        tmp_path,
        events,
        analysis={"as_of": "2026-02-15T00:00:00Z"},
        **month_spec(periods=[1, 2]),
    ).report
    periods = _periods(report)
    assert periods["M1"]["eligible_users"] == 1
    assert periods["M1"]["retained_users"] == 1
    # M2 目标 2026-02，要等 2026-03-01 才成熟。
    assert periods["M2"]["eligible_users"] == 0
    assert periods["M2"]["retention_rate"] is None

    matured = run_once(
        tmp_path,
        events,
        name="matured",
        analysis={"as_of": "2026-03-01T00:00:00Z"},
        **month_spec(periods=[1, 2]),
    ).report
    matured_periods = _periods(matured)
    assert matured_periods["M2"]["eligible_users"] == 1
    assert matured_periods["M2"]["retained_users"] == 1


# --- 时区月界：先转本地日期再按年月归期 ---


def test_timezone_month_boundary_uses_local_date_not_utc() -> None:
    # UTC 已是 3 月 1 日，但纽约本地仍是 2 月 28 日 -> 应归 M1。
    assert _local("2026-03-01T03:00:00Z", "America/New_York") == date(2026, 2, 28)
    assert period_index(date(2026, 2, 28), date(2026, 1, 15), "month") == 1
    # 相反方向：UTC 仍是 2 月 28 日，上海本地已到 3 月 1 日 -> 应归 M2。
    assert _local("2026-02-28T17:00:00Z", "Asia/Shanghai") == date(2026, 3, 1)
    assert period_index(date(2026, 3, 1), date(2026, 1, 15), "month") == 2


def test_timezone_boundary_retention_report(tmp_path: Path) -> None:
    events = [
        # 纽约本地 1 月 15 日 cohort。
        make_event("s1", "u1", "signup", "2026-01-15T17:00:00Z"),
        # UTC 3 月 1 日，纽约本地 2 月 28 日 -> M1。
        make_event("r1", "u1", "view", "2026-03-01T03:00:00Z"),
    ]
    report = run_once(
        tmp_path,
        events,
        analysis={"timezone": "America/New_York", "as_of": "2026-03-05T12:00:00Z"},
        **month_spec(periods=[1, 2]),
    ).report
    periods = _periods(report)
    assert report["retention"]["cohorts"][0]["date"] == "2026-01-15"
    assert periods["M1"]["retained_users"] == 1
    assert periods["M2"]["eligible_users"] == 0


def test_timezone_same_month_return_ignored(tmp_path: Path) -> None:
    events = [
        # 纽约本地 1 月 31 日 22:30 cohort。
        make_event("s1", "u1", "signup", "2026-02-01T03:30:00Z"),
        # 纽约本地 1 月 31 日 23:30，同月 M0；UTC 已是 2 月 1 日。
        make_event("r1", "u1", "view", "2026-02-01T04:30:00Z"),
    ]
    report = run_once(
        tmp_path,
        events,
        analysis={"timezone": "America/New_York", "as_of": "2026-04-05T12:00:00Z"},
        **month_spec(periods=[1]),
    ).report
    periods = _periods(report)
    assert report["retention"]["cohorts"][0]["date"] == "2026-01-31"
    assert periods["M1"]["retained_users"] == 0


def test_legacy_day_week_run_ids_stay_stable(tmp_path: Path) -> None:
    """新增 month 单位不改变旧 day/week 的标签、语义与 run_id。"""
    events = [
        make_event("s1", "u1", "signup", "2026-03-01T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-03-02T00:00:00Z"),
        make_event("s2", "u2", "signup", "2026-03-01T00:00:00Z"),
    ]
    expected = {
        "day": ("run-a5b1836aed12b5e1ecf4", ["D1", "D7"]),
        "week": ("run-01b99575c4ea5f7715c8", ["W1", "W2"]),
    }
    for unit, (run_id, labels) in expected.items():
        report = run_once(
            tmp_path,
            events,
            name=unit,
            retention={
                "cohort_event": "signup",
                "return_event": "view",
                "unit": unit,
                "periods": [1, 2] if unit == "week" else [1, 7],
                "mode": "exact",
            },
        ).report
        assert report["run_id"] == run_id
        assert [row["label"] for row in report["retention"]["periods"]] == labels


def test_dst_transition_does_not_shift_month(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2026-02-15T17:00:00Z"),  # 本地 2 月 15 日
        make_event("r1", "u1", "view", "2026-03-09T04:30:00Z"),   # 本地 3 月 9 日（EDT）
    ]
    report = run_once(
        tmp_path,
        events,
        analysis={"timezone": "America/New_York", "as_of": "2026-04-05T12:00:00Z"},
        **month_spec(periods=[1]),
    ).report
    assert _periods(report)["M1"]["retained_users"] == 1
