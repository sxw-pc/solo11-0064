from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from funnelcheck.engine.compare import compare_reports
from tests.support import make_event, run_once

TWO_STEP = {
    "funnel": {
        "name": "view_to_buy",
        "window": "7d",
        "steps": [
            {"name": "浏览", "event": "view"},
            {"name": "购买", "event": "buy"},
        ],
    }
}


def _reports(tmp_path: Path):
    events = [
        make_event("v1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u1", "buy", "2026-03-09T00:00:00Z"),
        make_event("v2", "u2", "view", "2026-03-01T01:00:00Z"),
    ]
    baseline = run_once(tmp_path, events, name="baseline", **TWO_STEP)
    candidate = run_once(
        tmp_path,
        events,
        name="candidate",
        funnel={**TWO_STEP["funnel"], "window": "14d"},
    )
    return baseline.report, candidate.report


def test_identical_reports(tmp_path: Path) -> None:
    baseline, _candidate = _reports(tmp_path)
    result = compare_reports(deepcopy(baseline), deepcopy(baseline))
    assert result["verdict"] == "identical"
    assert result["changed_metric_count"] == 0
    assert result["config_changes"] == []
    assert result["flags"] == {
        "config_changed": False,
        "input_changed": False,
        "metrics_changed": False,
        "engine_changed": False,
    }


def test_config_change_is_isolated(tmp_path: Path) -> None:
    baseline, candidate = _reports(tmp_path)
    result = compare_reports(baseline, candidate)
    assert result["verdict"] == "config_changed"
    assert result["flags"]["config_changed"] is True
    assert result["flags"]["input_changed"] is False
    assert result["flags"]["metrics_changed"] is True
    assert result["input"]["baseline_sha256"] == result["input"]["candidate_sha256"]
    paths = [item["path"] for item in result["config_changes"]]
    assert "spec.funnel.window" in paths
    assert result["changed_metric_count"] > 0
    step_metric = next(
        item for item in result["metrics"] if item["key"] == "funnel.steps.1.users"
    )
    assert step_metric["baseline"] == 0
    assert step_metric["candidate"] == 1


def test_input_change_is_isolated(tmp_path: Path) -> None:
    events = [
        make_event("v1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u1", "buy", "2026-03-02T00:00:00Z"),
    ]
    baseline = run_once(tmp_path, events, name="baseline", **TWO_STEP).report
    candidate = run_once(tmp_path, events[:1], name="candidate", **TWO_STEP).report
    result = compare_reports(baseline, candidate)
    assert result["verdict"] == "input_changed"
    assert result["flags"]["input_changed"] is True
    assert result["flags"]["config_changed"] is False
    assert result["config_changes"] == []
    assert result["changed_metric_count"] > 0


def test_config_and_input_change(tmp_path: Path) -> None:
    events = [
        make_event("v1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u1", "buy", "2026-03-07T00:00:00Z"),
    ]
    baseline = run_once(tmp_path, events, name="baseline", **TWO_STEP).report
    candidate = run_once(
        tmp_path,
        events[:1],
        name="candidate",
        funnel={**TWO_STEP["funnel"], "window": "14d"},
    ).report
    result = compare_reports(baseline, candidate)
    assert result["verdict"] == "config_and_input_changed"
    assert result["flags"]["config_changed"] is True
    assert result["flags"]["input_changed"] is True


def test_no_metric_change_when_only_title_changes(tmp_path: Path) -> None:
    events = [make_event("v1", "u1", "view", "2026-03-01T00:00:00Z")]
    baseline = run_once(tmp_path, events, name="baseline", title="A", **TWO_STEP).report
    candidate = run_once(tmp_path, events, name="candidate", title="B", **TWO_STEP).report
    result = compare_reports(baseline, candidate)
    assert result["flags"]["config_changed"] is True
    assert result["flags"]["metrics_changed"] is False
    assert result["verdict"] == "no_metric_change"
    assert result["config_changes"][0]["path"] == "spec.title"


def test_engine_and_unexplained_verdicts(tmp_path: Path) -> None:
    baseline, candidate = _reports(tmp_path)
    engine_changed = deepcopy(candidate)
    engine_changed["engine"]["semantics_version"] = "future-semantics"
    result = compare_reports(baseline, engine_changed)
    assert result["verdict"] == "engine_changed"
    assert result["flags"]["engine_changed"] is True

    unexplained = deepcopy(baseline)
    unexplained["funnel"]["converted_users"] += 1
    result = compare_reports(baseline, unexplained)
    assert result["verdict"] == "unexplained_metric_change"
    assert result["flags"] == {
        "config_changed": False,
        "input_changed": False,
        "metrics_changed": True,
        "engine_changed": False,
    }


def test_comparison_id_is_deterministic_and_summary_present(tmp_path: Path) -> None:
    baseline, candidate = _reports(tmp_path)
    first = compare_reports(baseline, candidate)
    second = compare_reports(baseline, candidate)
    assert first["comparison_id"] == second["comparison_id"]
    assert first["comparison_id"].startswith("cmp-")
    assert first["baseline"]["run_id"] == baseline["run_id"]
    assert first["candidate"]["run_id"] == candidate["run_id"]
    assert first["baseline"]["funnel"]["entered_users"] == 2
    assert "D1" in first["baseline"]["retention"]["periods"] or first["baseline"]["retention"]["periods"] == {}
    assert isinstance(first["quality_changes"], list)


MONTH_RETENTION = {
    "retention": {
        "cohort_event": "signup",
        "return_event": "view",
        "unit": "month",
        "periods": [1, 2],
        "mode": "exact",
    }
}


def _month_events() -> list[dict]:
    return [
        make_event("s1", "u1", "signup", "2026-01-10T00:00:00Z"),
        make_event("r1", "u1", "view", "2026-02-10T00:00:00Z"),
        make_event("s2", "u2", "signup", "2026-01-20T00:00:00Z"),
        make_event("r2", "u2", "view", "2026-03-05T00:00:00Z"),
    ]


def test_month_labels_sort_and_compare(tmp_path: Path) -> None:
    events = _month_events()
    baseline = run_once(
        tmp_path,
        events,
        name="baseline",
        analysis={"as_of": "2026-06-01T00:00:00Z"},
        **MONTH_RETENTION,
    ).report
    candidate = run_once(
        tmp_path,
        events,
        name="candidate",
        analysis={"as_of": "2026-06-01T00:00:00Z"},
        retention={**MONTH_RETENTION["retention"], "mode": "cumulative"},
    ).report
    result = compare_reports(baseline, candidate)
    assert result["flags"]["config_changed"] is True
    keys = [item["key"] for item in result["metrics"] if item["key"].startswith("retention.periods.")]
    # 指标顺序为 M1 后 M2，不能是 M1 之后跳到字符串排序意外的位置。
    assert keys[:3] == [
        "retention.periods.M1.eligible_users",
        "retention.periods.M1.retained_users",
        "retention.periods.M1.retention_rate",
    ]
    assert "retention.periods.M2.retained_users" in keys
    m2 = next(item for item in result["metrics"] if item["key"] == "retention.periods.M2.retained_users")
    assert m2["baseline"] == 1
    assert m2["candidate"] == 2
    assert "M1" in result["baseline"]["retention"]["periods"]
    assert "M2" in result["candidate"]["retention"]["periods"]


def test_incompatible_day_and_month_reports_do_not_mismatch_labels(tmp_path: Path) -> None:
    events = _month_events()
    day_report = run_once(
        tmp_path,
        events,
        name="day",
        analysis={"as_of": "2026-06-01T00:00:00Z"},
        retention={
            "cohort_event": "signup",
            "return_event": "view",
            "unit": "day",
            "periods": [1, 2],
            "mode": "exact",
        },
    ).report
    month_report = run_once(
        tmp_path,
        events,
        name="month",
        analysis={"as_of": "2026-06-01T00:00:00Z"},
        **MONTH_RETENTION,
    ).report
    result = compare_reports(day_report, month_report)
    labels = [item["key"].split(".")[2] for item in result["metrics"] if item["key"].startswith("retention.periods.")]
    assert "D1" in labels and "D2" in labels
    assert "M1" in labels and "M2" in labels
    # D1/M1 是不同指标：M1 的基线端为空，不能与 D1 的基线值按位置对齐。
    m1_eligible = next(item for item in result["metrics"] if item["key"] == "retention.periods.M1.eligible_users")
    d1_eligible = next(item for item in result["metrics"] if item["key"] == "retention.periods.D1.eligible_users")
    assert m1_eligible["baseline"] is None
    assert d1_eligible["candidate"] is None
    assert result["flags"]["config_changed"] is True
    assert result["verdict"] == "config_changed"


def test_week_and_month_reports_keep_distinct_labels(tmp_path: Path) -> None:
    events = _month_events()
    week_report = run_once(
        tmp_path,
        events,
        name="week",
        analysis={"as_of": "2026-06-01T00:00:00Z"},
        retention={**MONTH_RETENTION["retention"], "unit": "week"},
    ).report
    month_report = run_once(
        tmp_path,
        events,
        name="month2",
        analysis={"as_of": "2026-06-01T00:00:00Z"},
        **MONTH_RETENTION,
    ).report
    result = compare_reports(week_report, month_report)
    keys = [item["key"] for item in result["metrics"]]
    assert "retention.periods.W1.retained_users" in keys
    assert "retention.periods.M1.retained_users" in keys
    w1 = next(item for item in result["metrics"] if item["key"] == "retention.periods.W1.retained_users")
    m1 = next(item for item in result["metrics"] if item["key"] == "retention.periods.M1.retained_users")
    assert w1["candidate"] is None
    assert m1["baseline"] is None
