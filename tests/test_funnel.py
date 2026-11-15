from __future__ import annotations

import json
from pathlib import Path

from funnelcheck.config import canonical_json
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


def _transition(report: dict, from_step: int) -> dict:
    return next(
        item
        for item in report["funnel"]["duration"]["transitions"]
        if item["from_step"] == from_step
    )


def _point(transition: dict, fraction: float) -> dict:
    return next(
        item for item in transition["survival"]["points"] if item["fraction"] == fraction
    )


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


# --- 逐步转化耗时与右删失 -------------------------------------------------


def test_steps_record_hit_times_and_roundtrip_elapsed(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T10:00:00Z"),
        make_event("a2", "u1", "signup", "2026-03-01T10:02:30Z"),
        make_event("a3", "u1", "buy", "2026-03-01T10:07:30Z"),
        make_event("a4", "u1", "buy", "2026-03-01T10:09:00Z"),
    ]
    outcome = run_once(tmp_path, events)
    funnel = outcome.report["funnel"]
    steps = funnel["steps"]
    assert [step["users"] for step in steps] == [1, 1, 1]
    # 第 2 步记录的是“上一步 → 本步”，elapsed 以 occurred_at 差计算。
    assert steps[1]["time_to_convert"]["from_step"] == 1
    assert steps[1]["time_to_convert"]["observed_median_seconds"] == 150
    assert steps[2]["time_to_convert"]["observed_median_seconds"] == 300
    duration = funnel["duration"]["transitions"]
    assert [item["converted_users"] for item in duration] == [1, 1]
    assert duration[0]["entered_users"] == 1 and duration[1]["entered_users"] == 1


def test_right_censoring_is_not_zero_and_keeps_user_in_risk_set(tmp_path: Path) -> None:
    # u-done 在第 1 天转化；u-open 到窗口结束仍未转化，应作为右删失观测。
    events = [
        make_event("a1", "u-done", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u-done", "buy", "2026-03-02T00:00:00Z"),
        make_event("b1", "u-open", "view", "2026-03-01T00:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    transition = _transition(outcome.report, 1)
    assert transition["entered_users"] == 2
    assert transition["converted_users"] == 1
    assert transition["censored_users"] == 1
    assert transition["censored_window_users"] == 1
    # 完成者观测中位数是 1 天；但它不能冒充总体口径。
    assert transition["observed_median_seconds"] == 86400
    # KM：t=1d 时风险集 2，转化 1 人 → 仍未转化比例 0.5；窗口末端仍为 0.5。
    assert _point(transition, 0.5)["not_converted_ratio"] == 0.5
    assert _point(transition, 1.0)["not_converted_ratio"] == 0.5
    assert _point(transition, 1.0)["censored_by"] == 1
    # S(t) 从未跌破 0.5 之外（恰好 0.5）→ 中位数取首个 <= 0.5 的时点。
    assert transition["survival"]["median_seconds"] == 86400
    assert transition["survival"]["method"] == "kaplan_meier"


def test_zero_completions_gives_null_median_but_keeps_censored_users(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u2", "view", "2026-03-02T00:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    transition = _transition(outcome.report, 1)
    assert transition["entered_users"] == 2
    assert transition["converted_users"] == 0
    assert transition["censored_users"] == 2
    assert transition["observed_median_seconds"] is None
    assert transition["observed_median_label"] == "—"
    assert transition["survival"]["median_seconds"] is None
    assert transition["survival"]["median_label"] == "—"
    assert _point(transition, 1.0)["not_converted_ratio"] == 1.0
    assert _point(transition, 1.0)["censored_by"] == 2
    # JSON 必须保留 null 而不是 0 或空对象。
    assert transition["survival"]["median_seconds"] is None
    assert '"median_seconds": null' in json.dumps(
        transition["survival"], sort_keys=True
    )


def test_window_endpoint_converts_and_beyond_is_censored(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u-edge", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u-edge", "buy", "2026-03-08T00:00:00Z"),  # 恰在端点
        make_event("b1", "u-out", "view", "2026-03-01T00:00:00Z"),
        make_event("b2", "u-out", "buy", "2026-03-08T00:00:01Z"),  # 端点之后
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    transition = _transition(outcome.report, 1)
    assert transition["converted_users"] == 1
    assert transition["censored_users"] == 1
    assert transition["censored_window_users"] == 1
    assert transition["observed_median_seconds"] == 604800
    # 端点之后的 buy 不能被当作 604801 秒的转化时长。
    assert transition["observed_quantiles_seconds"]["max"] == 604800


def test_observation_cutoff_censors_incomplete_users(tmp_path: Path) -> None:
    # as_of = 2026-03-05；窗口 7d 还剩 3 天，但观察只能到 as_of。
    events = [
        make_event("a1", "u-view", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u-buy", "view", "2026-03-02T00:00:00Z"),
        make_event("b2", "u-buy", "buy", "2026-03-04T00:00:00Z"),
    ]
    outcome = run_once(
        tmp_path,
        events,
        analysis={"as_of": "2026-03-05T00:00:00Z"},
        **_two_step_spec(),
    )
    transition = _transition(outcome.report, 1)
    assert transition["entered_users"] == 2
    assert transition["converted_users"] == 1
    # u-view 的首步在 03-01，窗口末端 03-08 晚于 as_of → 观察截止删失。
    assert transition["censored_observation_users"] == 1
    assert transition["censored_window_users"] == 0
    # 观察时长封顶在 min(as_of - 首步, window) = 4 天。
    assert _point(transition, 1.0)["censored_by"] == 1
    methodology = outcome.report["funnel"]["duration"]["methodology"]
    assert methodology["observation_cutoff"] == "min(as_of, 首步 occurred_at + funnel.window)"
    assert methodology["window_boundary"].startswith("occurred_at 差 <= window 算转化")


def test_same_elapsed_ties_follow_kaplan_meier_order(tmp_path: Path) -> None:
    # u-fast 在 1 天转化；u-slow 在 1 天时正好到达观察截止 → 同刻删失。
    # 风险集必须包含 u-slow，因此 S(1d) = 1 - 1/2 = 0.5，而不是 0。
    events = [
        make_event("a1", "u-fast", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u-fast", "buy", "2026-03-02T00:00:00Z"),
        make_event("b1", "u-slow", "view", "2026-03-02T00:00:00Z"),
    ]
    outcome = run_once(
        tmp_path,
        events,
        analysis={"as_of": "2026-03-03T00:00:00Z"},
        **_two_step_spec(),
    )
    transition = _transition(outcome.report, 1)
    assert transition["converted_users"] == 1
    assert transition["censored_users"] == 1
    assert transition["survival"]["censoring_tie_order"] == "converted_before_censored"
    assert _point(transition, 1.0)["not_converted_ratio"] == 0.5
    assert transition["survival"]["median_seconds"] == 86400


def test_same_timestamp_ties_need_order_to_form_sequence(tmp_path: Path) -> None:
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
    received_funnel = received_order.report["funnel"]
    id_funnel = id_order.report["funnel"]
    assert received_funnel["entered_users"] == 1
    assert received_funnel["converted_users"] == 0
    assert id_funnel["converted_users"] == 1
    # 同刻 id 顺序能组成序列，elapsed 都是 0 秒；同刻仍消耗不同事件。
    assert id_funnel["steps"][1]["time_to_convert"]["observed_median_seconds"] == 0
    assert id_funnel["steps"][2]["time_to_convert"]["observed_median_seconds"] == 0
    assert id_funnel["duration"]["methodology"]["same_timestamp_order"] == "id_then_received"


def test_each_step_must_consume_a_distinct_event(tmp_path: Path) -> None:
    funnel_spec = {
        "name": "repeat_view",
        "window": "7d",
        "steps": [
            {"name": "首次浏览", "event": "view"},
            {"name": "再次浏览", "event": "view"},
            {"name": "购买", "event": "buy"},
        ],
    }
    events = [
        make_event("a1", "u1", "view", "2026-03-01T10:00:00Z"),
        make_event("a2", "u1", "view", "2026-03-01T10:05:00Z"),
        make_event("a3", "u1", "buy", "2026-03-01T10:06:00Z"),
        make_event("a4", "u1", "buy", "2026-03-01T10:07:00Z"),
        make_event("b1", "u2", "view", "2026-03-01T11:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, funnel=funnel_spec)
    funnel = outcome.report["funnel"]
    assert [step["users"] for step in funnel["steps"]] == [2, 1, 1]
    # 第一步的 buy 已被第 3 步消耗，第 4 个事件不得再次命中。
    first = _transition(outcome.report, 1)
    second = _transition(outcome.report, 2)
    assert first["converted_users"] == 1
    assert first["observed_median_seconds"] == 300
    assert second["converted_users"] == 1
    assert second["observed_median_seconds"] == 60


def test_multiple_same_name_candidates_and_conditions_on_same_event(tmp_path: Path) -> None:
    # 同用户有多个 signup；同一事件同时满足步骤 2 和步骤 3 的条件。
    # 排序键必须严格前进，因此步骤 2、3 必须各自消耗不同事件。
    funnel_spec = {
        "name": "distinct_events",
        "window": "7d",
        "steps": [
            {"name": "浏览", "event": "view"},
            {"name": "注册 free", "event": "signup", "where": {"properties.plan": "free"}},
            {"name": "注册 paid", "event": "signup", "where": {"properties.plan": "paid"}},
        ],
    }
    events = [
        make_event("a1", "u1", "view", "2026-03-01T10:00:00Z"),
        make_event("a2", "u1", "signup", "2026-03-01T10:01:00Z", properties={"plan": "free"}),
        make_event("a3", "u1", "signup", "2026-03-01T10:01:00Z", properties={"plan": "paid"}),
        make_event("b1", "u2", "view", "2026-03-01T11:00:00Z"),
        make_event("b2", "u2", "signup", "2026-03-01T11:01:00Z", properties={"plan": "free"}),
        make_event("b3", "u2", "signup", "2026-03-01T11:01:00Z", properties={"plan": "paid"}),
    ]
    outcome = run_once(
        tmp_path,
        events,
        events={"same_timestamp_order": "id_then_received"},
        funnel=funnel_spec,
    )
    funnel = outcome.report["funnel"]
    assert [step["users"] for step in funnel["steps"]] == [2, 2, 2]
    # u2 两个 signup 同刻：id 顺序保证 b2 < b3，可以组成序列且 elapsed 为 0。
    second = _transition(outcome.report, 2)
    assert second["converted_users"] == 2
    assert second["observed_median_seconds"] == 0
    assert second["converted_users"] == 2


def test_observed_and_survival_medians_diverge_with_censoring(tmp_path: Path) -> None:
    # 多数用户未完成；完成者中位数仍然只描述“完成者”这一偏快子集。
    # 构造上让完成者中位数早于生存中位数，避免二者偶然一致。
    events = [
        make_event("f1", "u-fast-1", "view", "2026-03-01T00:00:00Z"),
        make_event("f2", "u-fast-1", "buy", "2026-03-01T01:00:00Z"),
        make_event("f3", "u-fast-1", "view", "2026-03-20T00:00:00Z"),
        make_event("g1", "u-fast-2", "view", "2026-03-01T00:00:00Z"),
        make_event("g2", "u-fast-2", "buy", "2026-03-01T01:00:00Z"),
        make_event("s1", "u-slow-1", "view", "2026-03-01T02:00:00Z"),
        make_event("s2", "u-slow-2", "view", "2026-03-01T03:00:00Z"),
        make_event("s3", "u-slow-3", "view", "2026-03-01T04:00:00Z"),
        make_event("s4", "u-slow-4", "view", "2026-03-01T05:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    transition = _transition(outcome.report, 1)
    assert transition["entered_users"] == 6
    assert transition["converted_users"] == 2
    assert transition["censored_users"] == 4
    # 完成者中位数只反映两个快速完成者。
    assert transition["observed_median_seconds"] == 3600
    # KM：t=3600 时风险集 6，S=1-2/6≈0.6667；之后无转化，S 停在 0.6667 > 0.5。
    assert transition["survival"]["median_seconds"] is None
    assert _point(transition, 1.0)["not_converted_ratio"] == 0.666667
    observed_median = transition["observed_median_seconds"]
    survival_median = transition["survival"]["median_seconds"]
    assert observed_median != survival_median
    methodology = outcome.report["funnel"]["duration"]["methodology"]
    assert "不能" in methodology["observed_quantiles"]


def test_time_point_risk_set_and_cumulative_counts(tmp_path: Path) -> None:
    # A 1d 转化、B 2d、C 3d、D 到窗口结束删失（7d）。
    events = [
        make_event("a1", "u-a", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u-a", "buy", "2026-03-02T00:00:00Z"),
        make_event("b1", "u-b", "view", "2026-03-01T00:00:00Z"),
        make_event("b2", "u-b", "buy", "2026-03-03T00:00:00Z"),
        make_event("c1", "u-c", "view", "2026-03-01T00:00:00Z"),
        make_event("c2", "u-c", "buy", "2026-03-04T00:00:00Z"),
        make_event("d1", "u-d", "view", "2026-03-01T00:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    transition = _transition(outcome.report, 1)
    # KM：t=1d 风险 4 → 0.75；t=2d 风险 3 → 0.5；t=3d 风险 2 → 0.25；7d 删失不变。
    assert transition["survival"]["median_seconds"] == 172800
    first = _point(transition, 0.25)
    assert first["elapsed_seconds"] == 151200
    # 时点 1.75d 时，1d 已完成者已离开风险集，剩下 2d/3d/7d 三人。
    assert first["at_risk_users"] == 3
    assert first["converted_by"] == 1
    assert first["censored_by"] == 0
    assert first["not_converted_ratio"] == 0.75
    later = _point(transition, 0.75)
    assert later["at_risk_users"] == 1
    assert later["converted_by"] == 3
    assert later["not_converted_ratio"] == 0.25
    end = _point(transition, 1.0)
    assert end["censored_by"] == 1
    assert end["not_converted_ratio"] == 0.25


def test_transition_with_no_users_is_null_not_zero(tmp_path: Path) -> None:
    # 无人到达步骤 2 → 2 → 3 无风险集；必须为 null。
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u2", "view", "2026-03-01T00:00:00Z"),
    ]
    outcome = run_once(tmp_path, events)
    funnel = outcome.report["funnel"]
    assert [step["users"] for step in funnel["steps"]] == [2, 0, 0]
    second = _transition(outcome.report, 2)
    assert second["entered_users"] == 0
    assert second["converted_users"] == 0
    assert second["censored_users"] == 0
    assert second["observed_median_seconds"] is None
    assert second["survival"]["median_seconds"] is None
    assert all(point["not_converted_ratio"] is None for point in second["survival"]["points"])
    assert all(point["at_risk_users"] == 0 for point in second["survival"]["points"])


def test_time_points_are_clamped_to_observable_horizon(tmp_path: Path) -> None:
    # 两个用户都在 1 小时内转化 → 可观察范围只有 1 小时，时点不应落到 7 天。
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "buy", "2026-03-01T00:30:00Z"),
        make_event("b1", "u2", "view", "2026-03-01T00:00:00Z"),
        make_event("b2", "u2", "buy", "2026-03-01T01:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    transition = _transition(outcome.report, 1)
    points = transition["survival"]["points"]
    assert [point["elapsed_seconds"] for point in points] == [900, 1800, 2700, 3600]
    # window_fraction 仍标注它相对名义窗口的位置，便于审阅。
    assert points[-1]["window_fraction"] == 0.005952
    assert points[-1]["not_converted_ratio"] == 0.0
    assert transition["survival"]["median_seconds"] == 1800


def test_events_after_as_of_never_count_as_conversion(tmp_path: Path) -> None:
    # as_of 之后的事件即使仍在 future_grace 内，也不得在观察截止之后记为转化。
    events = [
        make_event("a1", "u-late", "view", "2026-03-04T00:00:00Z"),
        make_event("a2", "u-late", "buy", "2026-03-05T12:00:00Z"),  # as_of 之后
        make_event("b1", "u-none", "view", "2026-03-06T00:00:00Z"),  # as_of 之后入口
        make_event("b2", "u-none", "buy", "2026-03-06T01:00:00Z"),
    ]
    outcome = run_once(
        tmp_path,
        events,
        analysis={"as_of": "2026-03-05T00:00:00Z", "future_grace": "24h"},
        **_two_step_spec(),
    )
    funnel = outcome.report["funnel"]
    assert funnel["entered_users"] == 1
    assert funnel["converted_users"] == 0
    transition = _transition(outcome.report, 1)
    assert transition["censored_users"] == 1
    assert transition["censored_observation_users"] == 1


def test_steps_expose_compact_duration_summary(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "signup", "2026-03-01T00:02:00Z"),
        make_event("a3", "u1", "buy", "2026-03-01T00:05:00Z"),
    ]
    outcome = run_once(tmp_path, events)
    steps = outcome.report["funnel"]["steps"]
    assert "time_to_convert" not in steps[0]
    assert steps[1]["time_to_convert"] == {
        "from_step": 1,
        "to_step": 2,
        "observed_median_seconds": 120,
        "observed_median_label": "2.0m",
        "survival_median_seconds": 120,
        "survival_median_label": "2.0m",
        "converted_users": 1,
        "censored_users": 0,
    }
    # 完整曲线只在 duration.transitions 中保留一份。
    transition = _transition(outcome.report, 1)
    assert "survival" in transition and "points" in transition["survival"]


def test_last_checkpoint_always_has_risk_set(tmp_path: Path) -> None:
    # 最大观察时长落在最后一个时点，因此最后一个时点不应出现空风险集。
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "buy", "2026-03-02T00:00:00Z"),
        make_event("b1", "u2", "view", "2026-03-01T00:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    transition = _transition(outcome.report, 1)
    last = _point(transition, 1.0)
    assert last["elapsed_seconds"] == 604800
    assert last["at_risk_users"] == 1
    assert last["not_converted_ratio"] is not None


def test_duration_output_is_deterministic(tmp_path: Path) -> None:
    events = simple_funnel_events()
    first = run_once(tmp_path, events, name="first")
    second = run_once(tmp_path, list(reversed(events)), name="second")
    assert canonical_json(first.report["funnel"]) == canonical_json(
        second.report["funnel"]
    )


def test_duration_reaches_markdown_and_html(tmp_path: Path) -> None:
    events = [
        make_event("a1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("a2", "u1", "buy", "2026-03-02T00:00:00Z"),
        make_event("b1", "u2", "view", "2026-03-01T00:00:00Z"),
    ]
    outcome = run_once(tmp_path, events, **_two_step_spec())
    markdown = (outcome.artifact_dir / "report.md").read_text(encoding="utf-8")
    assert "### 逐步转化耗时（含右删失）" in markdown
    assert "Kaplan" in markdown
    assert "仍未转化" in markdown
    assert "`null`" in markdown  # 无法估计时明确为 null
    # 每个观察时点都标注它在名义窗口中的位置。
    assert "窗口的 25.0%" in markdown and "窗口的 100.0%" in markdown
    html = (outcome.artifact_dir / "report.html").read_text(encoding="utf-8")
    assert "逐步转化耗时（含右删失）" in html
    assert "Kaplan" in html
    assert "仍未转化" in html
    assert "<code>null</code>" in html  # 无法估计时明确显示 null
    assert "http://" not in html and "https://" not in html
