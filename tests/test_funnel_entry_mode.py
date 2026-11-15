"""复入归属口径（funnel.entry_mode）的引擎、分母、分群与对比测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from funnelcheck.engine.compare import compare_reports
from funnelcheck.errors import SpecError
from funnelcheck.spec import load_spec
from tests.support import make_event, read_json, run_once, write_spec

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

EVERY_TWO_STEP = {"funnel": {**TWO_STEP["funnel"], "entry_mode": "every_entry"}}


def _reentry_events() -> list[dict]:
    """同一用户首次进入超时失败，隔天重新进入并完成购买。"""
    return [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "view", "2026-03-10T00:00:00Z"),
        make_event("a3", "u1", "buy", "2026-03-10T01:00:00Z"),
    ]


def test_entry_mode_defaults_to_first_entry(tmp_path: Path) -> None:
    spec_path = write_spec(tmp_path)
    bundle = load_spec(spec_path)
    assert bundle.spec.funnel.entry_mode == "first_entry"
    assert bundle.document["funnel"]["entry_mode"] == "first_entry"


def test_entry_mode_rejects_unknown_value(tmp_path: Path) -> None:
    spec_path = write_spec(tmp_path, funnel={"entry_mode": "sometimes"})
    with pytest.raises(SpecError, match="entry_mode"):
        load_spec(spec_path)


def test_every_entry_counts_reentry_after_window_expiry(tmp_path: Path) -> None:
    events = _reentry_events()
    first = run_once(tmp_path, events, name="first", **TWO_STEP)
    every = run_once(tmp_path, events, name="every", **EVERY_TWO_STEP)

    first_funnel = first.report["funnel"]
    assert first_funnel["entry_mode"] == "first_entry"
    assert first_funnel["attempts"] == 1
    assert first_funnel["entered_users"] == 1
    assert first_funnel["converted_attempts"] == 0
    assert first_funnel["converted_users"] == 0

    funnel = every.report["funnel"]
    assert funnel["entry_mode"] == "every_entry"
    assert funnel["attempts"] == 2
    assert funnel["entered_users"] == 1
    assert funnel["converted_attempts"] == 1
    assert funnel["converted_users"] == 1
    assert funnel["attempt_conversion"] == 0.5
    assert funnel["overall_conversion"] == 1.0
    assert [step["attempts"] for step in funnel["steps"]] == [2, 1]
    assert [step["users"] for step in funnel["steps"]] == [1, 1]


def test_window_endpoint_is_inclusive_per_attempt(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "buy", "2026-03-08T00:00:00Z"),
        make_event("b1", "u1", "view", "2026-03-10T00:00:00Z"),
        make_event("b2", "u1", "buy", "2026-03-17T00:00:01Z"),
    ]
    outcome = run_once(tmp_path, events, **EVERY_TWO_STEP)
    funnel = outcome.report["funnel"]
    assert funnel["attempts"] == 2
    # 恰好在窗口端点的购买计入；超出 1 秒的购买不补入旧尝试。
    assert funnel["converted_attempts"] == 1
    assert [step["attempts"] for step in funnel["steps"]] == [2, 1]


def test_expired_path_is_not_backfilled_and_new_entry_converts(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "buy", "2026-03-10T00:00:00Z"),
        make_event("b1", "u1", "view", "2026-03-11T00:00:00Z"),
        make_event("b2", "u1", "buy", "2026-03-11T01:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **EVERY_TWO_STEP)
    funnel = outcome.report["funnel"]
    assert funnel["attempts"] == 2
    # 03-10 的购买超出首条尝试的 7d 窗口，不得补入旧路径；只有新入口的购买计入。
    assert funnel["converted_attempts"] == 1
    assert funnel["steps"][1]["attempts"] == 1
    assert funnel["steps"][1]["users"] == 1


def test_same_timestamp_first_steps_open_separate_attempts(tmp_path: Path) -> None:
    events = [
        make_event("a", "u1", "view", "2026-03-01T12:00:00Z", "2026-03-01T12:00:30Z"),
        make_event("b", "u1", "view", "2026-03-01T12:00:00Z", "2026-03-01T12:00:10Z"),
        make_event("c", "u1", "buy", "2026-03-01T12:05:00Z"),
    ]
    for name, order in (
        ("received", "received_then_id"),
        ("id-order", "id_then_received"),
    ):
        outcome = run_once(
            tmp_path,
            events,
            name=name,
            events={"same_timestamp_order": order},
            **EVERY_TWO_STEP,
        )
        funnel = outcome.report["funnel"]
        # 同刻两次首步各自成为独立尝试；唯一的购买只被消耗一次。
        assert funnel["attempts"] == 2
        assert funnel["converted_attempts"] == 1
        assert funnel["entered_users"] == 1
        assert funnel["converted_users"] == 1
        assert funnel["attempt_conversion"] == 0.5


def test_single_purchase_is_not_shared_between_two_entries(tmp_path: Path) -> None:
    events = [
        make_event("v1", "u1", "view", "2026-03-01T10:00:00Z"),
        make_event("v2", "u1", "view", "2026-03-01T12:00:00Z"),
        make_event("b1", "u1", "buy", "2026-03-01T13:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **EVERY_TWO_STEP)
    funnel = outcome.report["funnel"]
    # 同一购买落在两条入口的窗口内，只计入先开启的尝试。
    assert funnel["attempts"] == 2
    assert funnel["converted_attempts"] == 1
    assert funnel["steps"][1]["attempts"] == 1
    assert funnel["attempt_conversion"] == 0.5
    assert funnel["overall_conversion"] == 1.0


def test_repeated_step_events_are_each_consumed_once(tmp_path: Path) -> None:
    spec = {
        "funnel": {
            "name": "double_view",
            "window": "7d",
            "entry_mode": "every_entry",
            "steps": [
                {"name": "浏览", "event": "view"},
                {"name": "再浏览", "event": "view"},
            ],
        }
    }
    events = [
        make_event("v1", "u1", "view", "2026-03-01T10:00:00Z"),
        make_event("v2", "u1", "view", "2026-03-01T11:00:00Z"),
        make_event("v3", "u1", "view", "2026-03-01T12:00:00Z"),
    ]
    every = run_once(tmp_path, events, name="every", **spec)
    funnel = every.report["funnel"]
    # 每个命中首步的事件都开启自己的尝试并被其消耗，不会同时推进旧尝试。
    assert funnel["attempts"] == 3
    assert funnel["converted_attempts"] == 0
    assert [step["attempts"] for step in funnel["steps"]] == [3, 0]

    first = run_once(tmp_path, events, name="first", funnel={**spec["funnel"], "entry_mode": "first_entry"})
    first_funnel = first.report["funnel"]
    # 单路径口径下同一批事件沿步骤推进并转化，两种口径结果真实不同。
    assert first_funnel["attempts"] == 1
    assert first_funnel["converted_attempts"] == 1
    assert [step["users"] for step in first_funnel["steps"]] == [1, 1]


def test_step_denominators_report_attempts_and_deduped_users(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "view", "2026-03-02T00:00:00Z"),
        make_event("a3", "u1", "buy", "2026-03-02T01:00:00Z"),
        make_event("b1", "u2", "view", "2026-03-01T00:00:00Z"),
        make_event("b2", "u2", "buy", "2026-03-02T00:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **EVERY_TWO_STEP)
    funnel = outcome.report["funnel"]
    assert funnel["attempts"] == 3
    assert funnel["entered_users"] == 2
    assert funnel["converted_attempts"] == 2
    assert funnel["converted_users"] == 2
    assert funnel["attempt_conversion"] == 0.666667
    assert funnel["overall_conversion"] == 1.0
    first_step, second_step = funnel["steps"]
    assert (first_step["attempts"], first_step["users"]) == (3, 2)
    assert (second_step["attempts"], second_step["users"]) == (2, 2)
    assert second_step["conversion_from_first"] == 0.666667
    assert second_step["conversion_from_previous"] == 0.666667
    assert second_step["user_conversion_from_first"] == 1.0
    assert second_step["user_conversion_from_previous"] == 1.0
    assert second_step["dropoff_from_previous"] == 1


def test_segments_follow_the_counted_entry_event(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z", channel="alpha"),
        make_event("a2", "u1", "view", "2026-03-10T00:00:00Z", channel="beta"),
        make_event("a3", "u1", "buy", "2026-03-10T01:00:00Z", channel="beta"),
    ]
    segments = [{"field": "channel"}]
    every = run_once(tmp_path, events, name="every", segments=segments, **EVERY_TWO_STEP)
    rows = {row["value"]: row for row in every.report["funnel"]["by_segment"]["channel"]}
    # 同一用户的两条尝试按各自入口事件归属到不同分群。
    assert rows["alpha"]["attempts"] == 1
    assert rows["alpha"]["entered_users"] == 1
    assert rows["alpha"]["converted_attempts"] == 0
    assert rows["beta"]["attempts"] == 1
    assert rows["beta"]["converted_attempts"] == 1
    assert rows["beta"]["converted_users"] == 1
    assert rows["beta"]["attempt_conversion"] == 1.0

    first = run_once(tmp_path, events, name="first", segments=segments, **TWO_STEP)
    first_rows = {row["value"]: row for row in first.report["funnel"]["by_segment"]["channel"]}
    # 单路径口径只认首次入口，beta 分群不存在，分群归属随口径真实改变。
    assert set(first_rows) == {"alpha"}
    assert first_rows["alpha"]["converted_users"] == 0


def test_report_marks_entry_mode_in_methodology_and_markdown(tmp_path: Path) -> None:
    outcome = run_once(tmp_path, _reentry_events(), **EVERY_TWO_STEP)
    funnel = outcome.report["funnel"]
    methodology = funnel["methodology"]
    assert methodology["entry_mode"] == "every_entry"
    assert methodology["entry_mode_label"] == "每次入口单独计数"
    assert "独立尝试" in methodology["entry_rule"]
    assert "分母" in methodology["rate_denominators"]
    # 规格摘要（报告内嵌的规格文档）也标明该口径。
    assert outcome.report["spec"]["document"]["funnel"]["entry_mode"] == "every_entry"
    markdown = (outcome.artifact_dir / "report.md").read_text(encoding="utf-8")
    assert "复入口径" in markdown
    assert "每次入口单独计数" in markdown
    assert "入口尝试" in markdown
    html = (outcome.artifact_dir / "report.html").read_text(encoding="utf-8")
    assert "每次入口单独计数" in html


def test_compare_attributes_entry_mode_change_to_spec(tmp_path: Path) -> None:
    events = _reentry_events()
    baseline = run_once(tmp_path, events, name="baseline", **TWO_STEP).report
    candidate = run_once(tmp_path, events, name="candidate", **EVERY_TWO_STEP).report
    result = compare_reports(baseline, candidate)
    assert result["verdict"] == "config_changed"
    assert result["flags"]["config_changed"] is True
    assert result["flags"]["input_changed"] is False
    assert result["input"]["baseline_sha256"] == result["input"]["candidate_sha256"]
    mode_change = next(
        item for item in result["config_changes"] if item["path"] == "spec.funnel.entry_mode"
    )
    assert mode_change["baseline"] == "first_entry"
    assert mode_change["candidate"] == "every_entry"
    metrics = {item["key"]: item for item in result["metrics"]}
    assert metrics["funnel.attempts"]["baseline"] == 1
    assert metrics["funnel.attempts"]["candidate"] == 2
    assert metrics["funnel.attempts"]["changed"] is True
    assert metrics["funnel.converted_attempts"]["candidate"] == 1
    assert metrics["funnel.attempt_conversion"]["changed"] is True
    assert metrics["funnel.steps.0.attempts"]["candidate"] == 2
    # 对比摘要同样标明两侧口径。
    assert result["baseline"]["funnel"]["entry_mode"] == "first_entry"
    assert result["candidate"]["funnel"]["entry_mode"] == "every_entry"


def test_compare_no_metric_change_when_mode_change_is_inert(tmp_path: Path) -> None:
    events = [
        make_event("v1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u1", "buy", "2026-03-02T00:00:00Z"),
    ]
    baseline = run_once(tmp_path, events, name="baseline", **TWO_STEP).report
    candidate = run_once(tmp_path, events, name="candidate", **EVERY_TWO_STEP).report
    result = compare_reports(baseline, candidate)
    assert result["flags"]["config_changed"] is True
    assert result["flags"]["metrics_changed"] is False
    assert result["verdict"] == "no_metric_change"
    assert any(
        item["path"] == "spec.funnel.entry_mode" for item in result["config_changes"]
    )


def test_report_json_is_deterministic_for_every_entry(tmp_path: Path) -> None:
    events = _reentry_events()
    first = run_once(tmp_path, events, name="one", **EVERY_TWO_STEP)
    second = run_once(tmp_path, events, name="two", **EVERY_TWO_STEP)
    assert first.run_id == second.run_id
    assert read_json(first.artifact_dir / "report.json") == read_json(
        second.artifact_dir / "report.json"
    )
