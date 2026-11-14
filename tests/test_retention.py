from __future__ import annotations

from pathlib import Path

from tests.support import make_event, run_once

RETENTION_SPEC = {
    "retention": {
        "cohort_event": "signup",
        "return_event": "view",
        "unit": "day",
        "periods": [1, 3, 7],
        "mode": "exact",
    }
}


def _periods(report: dict) -> dict[str, dict]:
    return {row["label"]: row for row in report["retention"]["periods"]}


def test_exact_retention_periods(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2026-03-01T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-03-02T00:00:00Z"),
        make_event("r2", "u1", "view", "2026-03-04T00:00:00Z"),
        make_event("r3", "u1", "view", "2026-03-08T00:00:00Z"),
        make_event("s2", "u2", "signup", "2026-03-01T00:00:00Z"),
        make_event("r4", "u2", "view", "2026-03-04T00:00:00Z"),
        make_event("s3", "u3", "signup", "2026-03-01T00:00:00Z"),
        make_event("r5", "u3", "view", "2026-03-02T00:00:00Z"),
    ]
    report = run_once(tmp_path, events, **RETENTION_SPEC).report
    retention = report["retention"]
    periods = _periods(report)
    assert retention["cohort_size"] == 3
    assert periods["D1"]["retained_users"] == 2
    assert periods["D3"]["retained_users"] == 2
    assert periods["D7"]["retained_users"] == 1
    assert periods["D1"]["eligible_users"] == 3
    assert periods["D1"]["retention_rate"] == 0.666667
    assert [row["date"] for row in retention["cohorts"]] == ["2026-03-01"]


def test_cumulative_retention(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2026-03-01T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-03-02T00:00:00Z"),
        make_event("s2", "u2", "signup", "2026-03-01T00:00:00Z"),
        make_event("r2", "u2", "view", "2026-03-04T00:00:00Z"),
        make_event("s3", "u3", "signup", "2026-03-01T00:00:00Z"),
        make_event("r3", "u3", "view", "2026-03-02T00:00:00Z"),
    ]
    retention = {
        "retention": {**RETENTION_SPEC["retention"], "mode": "cumulative"}
    }
    report = run_once(tmp_path, events, **retention).report
    periods = _periods(report)
    assert periods["D1"]["retained_users"] == 2
    assert periods["D3"]["retained_users"] == 3
    assert periods["D7"]["retained_users"] == 3


def test_eligibility_and_null_rate(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2026-03-01T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-03-02T00:00:00Z"),
        make_event("s2", "u2", "signup", "2026-03-01T00:00:00Z"),
        make_event("r2", "u2", "view", "2026-03-02T00:00:00Z"),
    ]
    report = run_once(
        tmp_path,
        events,
        analysis={"as_of": "2026-03-03T00:00:00Z"},
        retention={
            "cohort_event": "signup",
            "return_event": "view",
            "unit": "day",
            "periods": [1, 7],
            "mode": "exact",
        },
    ).report
    periods = _periods(report)
    assert periods["D1"]["eligible_users"] == 2
    assert periods["D1"]["retained_users"] == 2
    assert periods["D7"]["eligible_users"] == 0
    assert periods["D7"]["retained_users"] == 0
    assert periods["D7"]["retention_rate"] is None


def test_weekly_retention_and_return_week_index(tmp_path: Path) -> None:
    events = [
        make_event("s", "u1", "signup", "2026-03-01T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-03-08T00:00:00Z"),
        make_event("r2", "u1", "view", "2026-03-10T00:00:00Z"),
        make_event("r3", "u1", "view", "2026-03-15T00:00:00Z"),
    ]
    report = run_once(
        tmp_path,
        events,
        analysis={"as_of": "2026-04-01T00:00:00Z"},
        retention={
            "cohort_event": "signup",
            "return_event": "view",
            "unit": "week",
            "periods": [1, 2],
            "mode": "exact",
        },
    ).report
    periods = _periods(report)
    assert periods["W1"]["retained_users"] == 1
    assert periods["W2"]["retained_users"] == 1
    assert periods["W1"]["unit"] == "week"


def test_retention_segments_and_missing_value(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2026-03-01T00:00:00Z", channel="alpha"),
        make_event("r1", "u1", "view", "2026-03-02T00:00:00Z"),
        make_event("s2", "u2", "signup", "2026-03-01T00:00:00Z", channel="beta"),
        make_event("r2", "u2", "view", "2026-03-02T00:00:00Z"),
        make_event("r3", "u2", "view", "2026-03-08T00:00:00Z"),
        make_event("s3", "u3", "signup", "2026-03-01T00:00:00Z"),
        make_event("r4", "u3", "view", "2026-03-02T00:00:00Z"),
    ]
    report = run_once(
        tmp_path,
        events,
        segments=[{"field": "channel"}],
        **RETENTION_SPEC,
    ).report
    rows = report["retention"]["by_segment"]["channel"]
    values = {row["value"]: row for row in rows}
    assert set(values) == {"(missing)", "alpha", "beta"}
    assert values["alpha"]["cohort_size"] == 1
    beta_periods = {row["label"]: row for row in values["beta"]["periods"]}
    assert beta_periods["D1"]["retained_users"] == 1
    assert beta_periods["D7"]["retained_users"] == 1


def test_first_cohort_event_and_same_local_day_are_used(tmp_path: Path) -> None:
    events = [
        make_event("s1", "u1", "signup", "2026-03-01T20:00:00Z"),
        make_event("s2", "u1", "signup", "2026-03-05T20:00:00Z"),
        make_event("r1", "u1", "view", "2026-03-02T15:00:00Z"),
    ]
    report = run_once(
        tmp_path,
        events,
        analysis={
            "timezone": "Asia/Shanghai",
            "as_of": "2026-03-20T00:00:00Z",
        },
        retention={
            "cohort_event": "signup",
            "return_event": "view",
            "unit": "day",
            "periods": [1],
            "mode": "exact",
        },
    ).report
    retention = report["retention"]
    assert retention["cohorts"][0]["date"] == "2026-03-02"
    assert retention["periods"][0]["retained_users"] == 0


def test_no_cohort_produces_empty_retention(tmp_path: Path) -> None:
    report = run_once(
        tmp_path,
        [make_event("v", "u1", "view", "2026-03-01T00:00:00Z")],
        **RETENTION_SPEC,
    ).report
    retention = report["retention"]
    assert retention["cohort_size"] == 0
    assert retention["periods"][0]["eligible_users"] == 0
    assert retention["periods"][0]["retention_rate"] is None
    assert retention["cohorts"] == []
