"""漏斗步骤跨运行身份配对（完整有序谓词前缀）的测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from funnelcheck.engine.compare import compare_reports


def _step(name: str, event: str, users: int, where: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "index": 0,
        "name": name,
        "event": event,
        "where": where or {},
        "users": users,
        "conversion_from_first": None,
        "conversion_from_previous": None,
        "dropoff_from_previous": 0,
    }


def _report(
    run_id: str,
    steps: list[dict[str, Any]],
    *,
    input_sha: str = "input",
    **funnel_overrides: Any,
) -> dict[str, Any]:
    funnel = {
        "name": "f",
        "window": "7d",
        "entered_users": steps[0]["users"] if steps else 0,
        "converted_users": steps[-1]["users"] if steps else 0,
        "overall_conversion": None,
        "steps": steps,
    }
    funnel.update(funnel_overrides)
    document = {
        "spec_version": 1,
        "funnel": {
            "name": "f",
            "window": "7d",
            "steps": [
                {
                    "name": step["name"],
                    "event": step["event"],
                    "where": step.get("where", {}),
                }
                for step in steps
                if "event" in step
            ],
        },
    }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "engine": {"version": "1.0.0", "semantics_version": "2026.11.1"},
        "spec": {"title": "t", "sha256": "spec", "document": document},
        "input": {"sha256": input_sha, "normalized_sha256": "norm", "byte_count": 1},
        "funnel": funnel,
        "retention": {"cohort_size": 0, "periods": []},
        "quality": {"counts": {}},
    }


def _indexed(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = deepcopy(steps)
    for index, row in enumerate(rows):
        row["index"] = index + 1
    return rows


def _by_status(comparison: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in comparison["funnel_steps"]:
        grouped.setdefault(entry["status"], []).append(entry)
    return grouped


def _step_metric(comparison: dict[str, Any], key: str) -> dict[str, Any]:
    return next(item for item in comparison["metrics"] if item["key"] == key)


def test_middle_insertion_marks_suffix_uncomparable_without_fake_delta() -> None:
    """路线三反例：checkout 前插入 qualify，后续步骤不得产生伪 delta。"""
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("结算", "checkout", 8),
                _step("购买", "buy", 3),
            ]
        ),
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("资格确认", "qualify", 90),
                _step("结算", "checkout", 8),
                _step("购买", "buy", 3),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)

    assert [entry["candidate_index"] for entry in grouped["matched"]] == [0]
    matched = grouped["matched"][0]
    assert matched["baseline"] == 100 and matched["candidate"] == 100
    assert matched["delta"] == 0 and matched["changed"] is False

    added = {entry["candidate_step"]["name"]: entry for entry in grouped["added"]}
    removed = {entry["baseline_step"]["name"]: entry for entry in grouped["removed"]}
    assert set(added) == {"资格确认", "结算", "购买"}
    assert set(removed) == {"结算", "购买"}
    # 旧 checkout=8 与新 checkout=8 不得被当作同一指标相减
    assert removed["结算"]["baseline"] == 8 and removed["结算"]["candidate"] is None
    assert added["结算"]["candidate"] == 8 and added["结算"]["baseline"] is None

    for entry in grouped["added"] + grouped["removed"]:
        assert entry["delta"] is None
        assert entry["delta_pct"] is None
        assert entry["changed"] is False

    step_metric_keys = [
        item["key"] for item in result["metrics"] if item["key"].startswith("funnel.steps.")
    ]
    # 稳定且互不冲突：matched 用位置键，added/removed 带状态命名空间
    assert len(step_metric_keys) == len(set(step_metric_keys))
    assert "funnel.steps.0.users" in step_metric_keys
    assert all(
        ".added." in key or ".removed." in key or key == "funnel.steps.0.users"
        for key in step_metric_keys
    )
    # 不可比项不污染 metrics_changed；overall 指标两边相同
    assert result["flags"]["metrics_changed"] is False
    assert result["changed_metric_count"] == 0
    step_changed = [
        item
        for item in result["metrics"]
        if item["key"].startswith("funnel.steps.") and item["changed"]
    ]
    assert step_changed == []


def test_middle_deletion_marks_suffix_removed_and_added() -> None:
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("资格确认", "qualify", 90),
                _step("结算", "checkout", 8),
                _step("购买", "buy", 3),
            ]
        ),
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("结算", "checkout", 8),
                _step("购买", "buy", 3),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert len(grouped["matched"]) == 1
    assert {entry["baseline_step"]["name"] for entry in grouped["removed"]} == {
        "资格确认",
        "结算",
        "购买",
    }
    assert {entry["candidate_step"]["name"] for entry in grouped["added"]} == {"结算", "购买"}
    for entry in grouped["removed"] + grouped["added"]:
        assert entry["delta"] is None and entry["changed"] is False
    assert result["changed_metric_count"] == 0


def test_pure_rename_still_matches_by_prefix() -> None:
    """纯改名不改变 (event, where) 前缀，仍然是同一语义步骤。"""
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览页", "view", 100),
                _step("下单", "buy", 5),
            ]
        ),
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("落地浏览", "view", 100),
                _step("完成购买", "buy", 5),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert len(grouped["matched"]) == 2
    assert "added" not in grouped and "removed" not in grouped and "legacy" not in grouped
    assert [entry["key"] for entry in grouped["matched"]] == [
        "funnel.steps.0.users",
        "funnel.steps.1.users",
    ]
    assert all(entry["changed"] is False for entry in grouped["matched"])
    assert result["flags"]["metrics_changed"] is False


def test_same_name_event_change_is_removed_plus_added() -> None:
    baseline = _report(
        "run-b", _indexed([_step("浏览", "view", 100), _step("转化", "signup", 5)])
    )
    candidate = _report(
        "run-c", _indexed([_step("浏览", "view", 100), _step("转化", "register", 5)])
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert len(grouped["matched"]) == 1
    removed = grouped["removed"][0]
    added = grouped["added"][0]
    assert removed["baseline_step"]["name"] == "转化"
    assert added["candidate_step"]["name"] == "转化"
    assert removed["delta"] is None and added["delta"] is None
    assert removed["changed"] is False and added["changed"] is False


def test_same_name_where_change_breaks_prefix_for_suffix() -> None:
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览", "view", 100, {"channel": "web"}),
                _step("购买", "buy", 5),
            ]
        ),
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100, {"channel": "app"}),
                _step("购买", "buy", 5),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert "matched" not in grouped
    assert {entry["baseline_index"] for entry in grouped["removed"]} == {0, 1}
    assert {entry["candidate_index"] for entry in grouped["added"]} == {0, 1}
    for entry in grouped["removed"] + grouped["added"]:
        assert entry["delta"] is None and entry["changed"] is False
    # 规格差异仍然逐路径记录
    paths = {item["path"] for item in result["config_changes"]}
    assert any("funnel.steps[0].where" in path for path in paths)


def test_step_reorder_does_not_cross_pair() -> None:
    """重排：共同前缀之后全部按原顺序列为 removed/added，不交叉配对。"""
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("注册", "signup", 40),
                _step("加购", "cart", 20),
                _step("购买", "buy", 5),
            ]
        ),
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("加购", "cart", 20),
                _step("注册", "signup", 40),
                _step("购买", "buy", 5),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert [entry["candidate_step"]["event"] for entry in grouped["matched"]] == ["view"]
    assert [entry["baseline_step"]["event"] for entry in grouped["removed"]] == [
        "signup",
        "cart",
        "buy",
    ]
    assert [entry["candidate_step"]["event"] for entry in grouped["added"]] == [
        "cart",
        "signup",
        "buy",
    ]
    # 即便 signup/cart 在两侧都出现，也不允许交叉匹配出 delta
    for entry in grouped["removed"] + grouped["added"]:
        assert entry["delta"] is None and entry["changed"] is False


def test_duplicate_signatures_are_disambiguated_by_position() -> None:
    """重复单步签名由前缀中的出现位置自然区分，键不冲突且配对确定。"""
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览一", "view", 100),
                _step("浏览二", "view", 60),
                _step("购买", "buy", 5),
            ]
        ),
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览一", "view", 100),
                _step("浏览二", "view", 70),
                _step("购买", "buy", 6),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert len(grouped["matched"]) == 3
    second = grouped["matched"][1]
    assert second["baseline"] == 60 and second["candidate"] == 70
    assert second["delta"] == 10 and second["changed"] is True
    third = grouped["matched"][2]
    assert third["delta"] == 1 and third["changed"] is True
    keys = [entry["key"] for entry in result["funnel_steps"]]
    assert keys == [
        "funnel.steps.0.users",
        "funnel.steps.1.users",
        "funnel.steps.2.users",
    ]
    assert result["changed_metric_count"] >= 2
    assert result["flags"]["metrics_changed"] is True


def test_real_user_change_on_matched_prefix_produces_delta() -> None:
    """前缀完全相同、仅事件数据变化时，人数差才是真实数值变化。"""
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览", "view", 100, {"channel": "web"}),
                _step("购买", "buy", 8),
            ]
        ),
        input_sha="input-b",
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 120, {"channel": "web"}),
                _step("购买", "buy", 4),
            ]
        ),
        input_sha="input-c",
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert len(grouped["matched"]) == 2
    first, second = grouped["matched"]
    assert first["delta"] == 20 and first["delta_pct"] == 20.0 and first["changed"] is True
    assert second["delta"] == -4 and second["changed"] is True
    metric = _step_metric(result, "funnel.steps.1.users")
    assert metric["baseline"] == 8 and metric["candidate"] == 4
    assert result["flags"]["metrics_changed"] is True
    # 输入不同、规格相同 → 归因于输入
    assert result["verdict"] == "input_changed"


def test_legacy_report_without_definitions_is_index_only() -> None:
    """缺少 event/where 的历史报告：legacy/index-only，不猜测身份、不产生 delta。"""
    baseline = _report(
        "run-b",
        [
            {"index": 1, "name": "浏览", "users": 100},
            {"index": 2, "name": "购买", "users": 5},
        ],
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("购买", "buy", 6),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert "matched" not in grouped and "added" not in grouped and "removed" not in grouped
    legacy = grouped["legacy"]
    assert [entry["baseline_index"] for entry in legacy] == [0, 1]
    assert [entry["candidate_index"] for entry in legacy] == [0, 1]
    for entry in legacy:
        assert entry["prefix"] is None
        assert entry["delta"] is None
        assert entry["delta_pct"] is None
        assert entry["changed"] is False
    # 唯一真实变化来自候选完成用户数（overall 指标），步骤项不计入
    step_changed = [
        item
        for item in result["metrics"]
        if item["key"].startswith("funnel.steps.") and item["changed"]
    ]
    assert step_changed == []


def test_legacy_report_with_different_lengths_pads_by_index() -> None:
    baseline = _report(
        "run-b",
        [{"index": 1, "name": "浏览", "users": 100}],
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("购买", "buy", 6),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    legacy = _by_status(result)["legacy"]
    assert len(legacy) == 2
    assert legacy[1]["baseline"] is None and legacy[1]["candidate"] == 6
    assert all(entry["changed"] is False for entry in legacy)


def test_prefix_payload_is_complete_and_ordered() -> None:
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览", "view", 100, {"channel": "web"}),
                _step("购买", "buy", 5),
            ]
        ),
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100, {"channel": "web"}),
                _step("购买", "buy", 5),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    second = result["funnel_steps"][1]
    assert second["prefix"] == [
        ["view", '{"channel":"web"}'],
        ["buy", "{}"],
    ]
    # metrics 与 funnel_steps 共享同一份配对结果
    metric = _step_metric(result, "funnel.steps.1.users")
    assert metric["status"] == "matched"
    assert metric["prefix"] == second["prefix"]


def test_overall_funnel_metrics_still_compared_when_steps_uncomparable() -> None:
    """步骤不可比时，overall 漏斗指标仍按原报告值正常比较。"""
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("购买", "buy", 10),
            ]
        ),
        overall_conversion=0.1,
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("资格", "qualify", 80),
                _step("购买", "buy", 20),
            ]
        ),
        overall_conversion=0.2,
    )
    result = compare_reports(baseline, candidate)
    overall = _step_metric(result, "funnel.overall_conversion")
    assert overall["baseline"] == 0.1 and overall["candidate"] == 0.2
    assert overall["changed"] is True
    converted = _step_metric(result, "funnel.converted_users")
    assert converted["delta"] == 10 and converted["changed"] is True
    # 步骤条目自身没有任何 changed
    assert all(
        not entry["changed"]
        for entry in result["funnel_steps"]
        if entry["status"] != "matched"
    )


def test_append_step_keeps_shared_prefix_matched() -> None:
    baseline = _report(
        "run-b", _indexed([_step("浏览", "view", 100), _step("购买", "buy", 5)])
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("购买", "buy", 5),
                _step("复购", "rebuy", 2),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    grouped = _by_status(result)
    assert len(grouped["matched"]) == 2
    added = grouped["added"]
    assert len(added) == 1 and added[0]["candidate_step"]["event"] == "rebuy"
    assert added[0]["key"] == "funnel.steps.added.2.users"
    assert added[0]["delta"] is None and added[0]["changed"] is False


def test_metric_keys_are_unique_under_colliding_indexes() -> None:
    baseline = _report(
        "run-b",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("结算", "checkout", 8),
            ]
        ),
    )
    candidate = _report(
        "run-c",
        _indexed(
            [
                _step("浏览", "view", 100),
                _step("资格", "qualify", 90),
                _step("结算", "checkout", 8),
            ]
        ),
    )
    result = compare_reports(baseline, candidate)
    keys = [item["key"] for item in result["metrics"]]
    assert len(keys) == len(set(keys))
    # 下标 1 同时存在 removed 与 added，但键不冲突
    assert "funnel.steps.removed.1.users" in keys
    assert "funnel.steps.added.1.users" in keys
