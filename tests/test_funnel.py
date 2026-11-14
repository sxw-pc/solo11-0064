from __future__ import annotations

from pathlib import Path

from tests.support import make_event, run_once, simple_funnel_events


def _two_step_spec(window: str = "7d") -> dict:
    return {
        "funnel": {
            "name": "view_to_buy",
            "window": window,
            "steps": [
                {"name": "浏览", "event": "view"},
                {"name": "购买", "event": "buy"},
            ],
        }
    }


def test_basic_funnel_counts_and_rates(tmp_path: Path) -> None:
    outcome = run_once(tmp_path, simple_funnel_events())
    funnel = outcome.report["funnel"]
    assert funnel["entered_users"] == 3
    assert funnel["converted_users"] == 1
    assert funnel["overall_conversion"] == 0.333333
    assert [step["users"] for step in funnel["steps"]] == [3, 2, 1]
    assert funnel["steps"][0]["conversion_from_first"] == 1.0
    assert funnel["steps"][1]["conversion_from_first"] == 0.666667
    assert funnel["steps"][2]["conversion_from_previous"] == 0.5
    assert funnel["steps"][2]["dropoff_from_previous"] == 1


def test_window_is_inclusive_and_starts_at_first_step(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u-edge", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u-edge", "buy", "2026-03-08T00:00:00Z"),
        make_event("b1", "u-out", "view", "2026-03-01T00:00:00Z"),
        make_event("b2", "u-out", "buy", "2026-03-08T00:00:01Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    funnel = outcome.report["funnel"]
    assert funnel["entered_users"] == 2
    assert funnel["converted_users"] == 1
    by_name = {step["name"]: step for step in funnel["steps"]}
    assert by_name["购买"]["users"] == 1


def test_same_timestamp_order_changes_match(tmp_path: Path) -> None:
    events = [
        make_event("a", "u1", "view", "2026-03-01T12:00:00Z", "2026-03-01T12:00:30Z"),
        make_event("b", "u1", "signup", "2026-03-01T12:00:00Z", "2026-03-01T12:00:10Z"),
        make_event("c", "u1", "buy", "2026-03-01T12:00:00Z", "2026-03-01T12:00:20Z"),
    ]
    received_order = run_once(tmp_path, events, name="received")
    id_order = run_once(
        tmp_path,
        events,
        name="id-order",
        events={"same_timestamp_order": "id_then_received"},
    )
    assert received_order.report["funnel"]["converted_users"] == 0
    assert id_order.report["funnel"]["converted_users"] == 1
    assert "id_then_received" in id_order.report["funnel"]["methodology"]["event_order"]


def test_where_filters_step_events(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T10:00:00Z"),
        make_event("a2", "u1", "signup", "2026-03-01T10:01:00Z", properties={"plan": "free"}),
        make_event("a3", "u1", "buy", "2026-03-01T10:02:00Z", properties={"amount": 42}),
        make_event("b1", "u2", "view", "2026-03-01T11:00:00Z"),
        make_event("b2", "u2", "signup", "2026-03-01T11:01:00Z", properties={"plan": "pro"}),
        make_event("b3", "u2", "buy", "2026-03-01T11:02:00Z", properties={"amount": 42}),
        make_event("c1", "u3", "view", "2026-03-01T12:00:00Z"),
        make_event("c2", "u3", "signup", "2026-03-01T12:01:00Z", properties={"plan": "free"}),
        make_event("c3", "u3", "buy", "2026-03-01T12:02:00Z", properties={"amount": 7}),
    ]
    funnel_spec = _two_step_spec()["funnel"]
    funnel_spec["steps"] = [
        {"name": "浏览", "event": "view"},
        {"name": "免费注册", "event": "signup", "where": {"properties.plan": "free"}},
        {"name": "指定购买", "event": "buy", "where": {"properties.amount": 42}},
    ]
    outcome = run_once(tmp_path, events, funnel=funnel_spec)
    funnel = outcome.report["funnel"]
    assert [step["users"] for step in funnel["steps"]] == [3, 2, 1]


def test_funnel_segments_use_first_step_event(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T10:00:00Z", channel="alpha"),
        make_event("a2", "u1", "signup", "2026-03-01T10:01:00Z", channel="other"),
        make_event("b1", "u2", "view", "2026-03-01T11:00:00Z", channel="alpha"),
        make_event("c1", "u3", "view", "2026-03-01T12:00:00Z"),
    ]
    outcome = run_once(
        tmp_path,
        events,
        segments=[{"field": "channel"}],
        **_two_step_spec(),
    )
    rows = outcome.report["funnel"]["by_segment"]["channel"]
    assert [row["value"] for row in rows] == ["(missing)", "alpha"]
    alpha = next(row for row in rows if row["value"] == "alpha")
    assert alpha["entered_users"] == 2
    assert alpha["converted_users"] == 0


def test_first_entry_is_not_retried_after_window_expiry(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "view", "2026-03-10T00:00:00Z"),
        make_event("a3", "u1", "buy", "2026-03-10T01:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    assert outcome.report["funnel"]["entered_users"] == 1
    assert outcome.report["funnel"]["converted_users"] == 0
