"""两次运行的指标、配置、输入差异与归因判定。

漏斗步骤的跨运行身份不依赖数组下标，而使用**完整有序谓词前缀**：

    prefix(step_i) = [
        (event_1, canonical_json(where_1)),
        ...
        (event_i, canonical_json(where_i)),
    ]

只有完整前缀相等的两个步骤才是同一语义步骤（status=matched），此时比较
用户数并生成 delta；插入、删除、重命名以外的定义改变、重排都会让受影响后缀
失去共同前缀，分别标记为 removed / added，保留双方原值但不计算差值。缺少
event/where 定义字段的历史报告无法重建身份，按 legacy/index-only 处理，同样
不猜测身份、不生成 delta。
"""

from __future__ import annotations

from typing import Any

from ..config import (
    COMPARE_SCHEMA_VERSION,
    SEMANTICS_VERSION,
    canonical_json,
    make_comparison_id,
)
from ..errors import InputError

_MISSING = object()

# 步骤配对状态：
# matched —— 完整有序谓词前缀相同，可直接比较用户数
# removed —— 仅基线存在的旧定义步骤
# added   —— 仅候选存在的新定义步骤
# legacy  —— 旧报告缺少 event/where 定义，无法重建前缀身份
STEP_MATCHED = "matched"
STEP_REMOVED = "removed"
STEP_ADDED = "added"
STEP_LEGACY = "legacy"

STEP_STATUS_LABELS = {
    STEP_MATCHED: "可比",
    STEP_REMOVED: "旧定义移除",
    STEP_ADDED: "新定义新增",
    STEP_LEGACY: "历史报告（仅位置）",
}


def _flatten(value: Any, prefix: str = "spec") -> dict[str, Any]:
    output: dict[str, Any] = {}
    if isinstance(value, dict) and value:
        for key in sorted(value, key=str):
            child = f"{prefix}.{key}"
            output.update(_flatten(value[key], child))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            output.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        output[prefix] = value
    return output


def _config_changes(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    left = _flatten(baseline)
    right = _flatten(candidate)
    changes: list[dict[str, Any]] = []
    for path in sorted(set(left) | set(right)):
        before = left.get(path, _MISSING)
        after = right.get(path, _MISSING)
        if before is _MISSING and after is _MISSING:
            continue
        if (
            before is not _MISSING
            and after is not _MISSING
            and canonical_json(before) == canonical_json(after)
        ):
            continue
        if before is _MISSING:
            changes.append({"path": path, "change": "added", "candidate": after})
        elif after is _MISSING:
            changes.append({"path": path, "change": "removed", "baseline": before})
        else:
            changes.append(
                {"path": path, "change": "changed", "baseline": before, "candidate": after}
            )
    return changes


def _diff(baseline: Any, candidate: Any) -> tuple[Any, float | None, bool]:
    if baseline is None and candidate is None:
        return None, None, False
    if baseline is None or candidate is None:
        return None, None, True
    delta = round(float(candidate) - float(baseline), 6)
    if isinstance(baseline, int) and isinstance(candidate, int):
        delta = int(delta)
    pct = round(delta / float(baseline) * 100, 6) if baseline != 0 else None
    return delta, pct, delta != 0


def _metric(
    items: list[dict[str, Any]],
    key: str,
    label: str,
    baseline: Any,
    candidate: Any,
) -> None:
    delta, pct, changed = _diff(baseline, candidate)
    items.append(
        {
            "key": key,
            "label": label,
            "baseline": baseline,
            "candidate": candidate,
            "delta": delta,
            "delta_pct": pct,
            "changed": changed,
        }
    )


def _has_identity(step: dict[str, Any]) -> bool:
    """历史报告的步骤可能只有 name/users，缺少 event/where 定义。"""
    return isinstance(step, dict) and "event" in step and "where" in step


def _step_signature(step: dict[str, Any]) -> tuple[str, str]:
    """单步签名：事件名 + where 的规范 JSON 文本。"""
    return str(step.get("event")), canonical_json(step.get("where") or {})


def _prefixes(steps: list[dict[str, Any]]) -> list[tuple[tuple[str, str], ...]]:
    """截至当前步的完整有序谓词前缀序列。

    同一次运行内前缀天然唯一（每个前缀至少在末尾位置上不同），重复单步
    签名由其在序列中的出现位置自然消歧。
    """
    prefixes: list[tuple[tuple[str, str], ...]] = []
    current: list[tuple[str, str]] = []
    for step in steps:
        current = [*current, _step_signature(step)]
        prefixes.append(tuple(current))
    return prefixes


def _prefix_json(prefix: tuple[tuple[str, str], ...]) -> list[list[str]]:
    return [[event, where_json] for event, where_json in prefix]


def _step_key(status: str, index: int) -> str:
    """稳定且互不冲突的指标键。

    可比步骤沿用历史位置键 funnel.steps.{i}.users；不可比项带状态命名空间，
    即使两侧下标相同也不会互相覆盖。
    """
    if status == STEP_MATCHED:
        return f"funnel.steps.{index}.users"
    return f"funnel.steps.{status}.{index}.users"


def _step_label(status: str, index: int, step: dict[str, Any] | None) -> str:
    name = (step or {}).get("name") or "?"
    position = index + 1
    if status == STEP_MATCHED:
        return f"漏斗步骤 {position} · {name} · 用户"
    if status == STEP_REMOVED:
        return f"漏斗步骤 {position} · {name} · 用户（旧定义移除，不可比）"
    if status == STEP_ADDED:
        return f"漏斗步骤 {position} · {name} · 用户（新定义新增，不可比）"
    return f"漏斗步骤 {position} · {name} · 用户（历史报告，仅位置）"


def _unmatched_entry(
    status: str,
    *,
    step: dict[str, Any] | None,
    index: int,
    prefix: tuple[tuple[str, str], ...] | None,
    side: str,
) -> dict[str, Any]:
    """不可比条目：保留原数与定义，但不产生 delta/delta_pct，changed 恒为 False。"""
    users = None if step is None else step.get("users")
    entry: dict[str, Any] = {
        "key": _step_key(status, index),
        "label": _step_label(status, index, step),
        "status": status,
        "status_label": STEP_STATUS_LABELS[status],
        "baseline_index": index if side == "baseline" else None,
        "candidate_index": index if side == "candidate" else None,
        "baseline": users if side == "baseline" else None,
        "candidate": users if side == "candidate" else None,
        "delta": None,
        "delta_pct": None,
        "changed": False,
        "baseline_step": step if side == "baseline" else None,
        "candidate_step": step if side == "candidate" else None,
        "prefix": _prefix_json(prefix) if prefix is not None else None,
    }
    return entry


def _legacy_entries(
    b_steps: list[dict[str, Any]], c_steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """缺少定义的历史报告：按下标并排展示，标 legacy/index-only，不猜测身份。"""
    entries: list[dict[str, Any]] = []
    for index in range(max(len(b_steps), len(c_steps))):
        left = b_steps[index] if index < len(b_steps) else None
        right = c_steps[index] if index < len(c_steps) else None
        name = (right or left or {}).get("name") or "?"
        entries.append(
            {
                "key": _step_key(STEP_LEGACY, index),
                "label": f"漏斗步骤 {index + 1} · {name} · 用户（历史报告，仅位置）",
                "status": STEP_LEGACY,
                "status_label": STEP_STATUS_LABELS[STEP_LEGACY],
                "baseline_index": index if left is not None else None,
                "candidate_index": index if right is not None else None,
                "baseline": None if left is None else left.get("users"),
                "candidate": None if right is None else right.get("users"),
                "delta": None,
                "delta_pct": None,
                "changed": False,
                "baseline_step": left,
                "candidate_step": right,
                "prefix": None,
            }
        )
    return entries


def _pair_funnel_steps(
    b_steps: list[dict[str, Any]], c_steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """基于完整有序谓词前缀的确定性一对一保序配对。

    前缀是逐步累积的，两个序列的共同前缀一旦在某位置分叉就不会再次汇合，
    因此共同前缀部分即为全部 matched；分叉点之后的基线步骤为 removed、
    候选步骤为 added，重排场景按各自原顺序输出且不会交叉配对。
    """
    if not all(_has_identity(step) for step in b_steps) or not all(
        _has_identity(step) for step in c_steps
    ):
        return _legacy_entries(b_steps, c_steps)

    b_prefixes = _prefixes(b_steps)
    c_prefixes = _prefixes(c_steps)
    matched_count = 0
    for index in range(min(len(b_prefixes), len(c_prefixes))):
        if b_prefixes[index] != c_prefixes[index]:
            break
        matched_count = index + 1

    entries: list[dict[str, Any]] = []
    for index in range(matched_count):
        left, right = b_steps[index], c_steps[index]
        delta, pct, changed = _diff(left.get("users"), right.get("users"))
        entries.append(
            {
                "key": _step_key(STEP_MATCHED, index),
                "label": _step_label(STEP_MATCHED, index, right),
                "status": STEP_MATCHED,
                "status_label": STEP_STATUS_LABELS[STEP_MATCHED],
                "baseline_index": index,
                "candidate_index": index,
                "baseline": left.get("users"),
                "candidate": right.get("users"),
                "delta": delta,
                "delta_pct": pct,
                "changed": changed,
                "baseline_step": left,
                "candidate_step": right,
                "prefix": _prefix_json(b_prefixes[index]),
            }
        )
    for index in range(matched_count, len(b_steps)):
        entries.append(
            _unmatched_entry(
                STEP_REMOVED,
                step=b_steps[index],
                index=index,
                prefix=b_prefixes[index],
                side="baseline",
            )
        )
    for index in range(matched_count, len(c_steps)):
        entries.append(
            _unmatched_entry(
                STEP_ADDED,
                step=c_steps[index],
                index=index,
                prefix=c_prefixes[index],
                side="candidate",
            )
        )
    return entries


def _metrics(
    base: dict[str, Any],
    cand: dict[str, Any],
    step_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    bf, cf = base.get("funnel", {}), cand.get("funnel", {})
    _metric(items, "funnel.entered_users", "漏斗入口用户", bf.get("entered_users"), cf.get("entered_users"))
    _metric(items, "funnel.converted_users", "漏斗完成用户", bf.get("converted_users"), cf.get("converted_users"))
    _metric(items, "funnel.overall_conversion", "漏斗整体转化率", bf.get("overall_conversion"), cf.get("overall_conversion"))
    items.extend(step_entries)
    br, cr = base.get("retention", {}), cand.get("retention", {})
    _metric(items, "retention.cohort_size", "留存队列规模", br.get("cohort_size"), cr.get("cohort_size"))
    b_periods = {row.get("label"): row for row in (br.get("periods") or [])}
    c_periods = {row.get("label"): row for row in (cr.get("periods") or [])}
    labels = sorted(set(b_periods) | set(c_periods), key=lambda item: _label_sort(item))
    for label in labels:
        left = b_periods.get(label, {})
        right = c_periods.get(label, {})
        _metric(items, f"retention.periods.{label}.eligible_users", f"{label} 可观察用户", left.get("eligible_users"), right.get("eligible_users"))
        _metric(items, f"retention.periods.{label}.retained_users", f"{label} 留存用户", left.get("retained_users"), right.get("retained_users"))
        _metric(items, f"retention.periods.{label}.retention_rate", f"{label} 留存率", left.get("retention_rate"), right.get("retention_rate"))
    return items


def _label_sort(label: str) -> tuple[str, int]:
    prefix = label[:1]
    number = "".join(char for char in label[1:] if char.isdigit())
    return prefix, int(number or 0)


def _quality_changes(base: dict[str, Any], cand: dict[str, Any]) -> list[dict[str, Any]]:
    left = base.get("quality", {}).get("counts", {})
    right = cand.get("quality", {}).get("counts", {})
    changes = []
    for key in sorted(set(left) | set(right)):
        before, after = left.get(key, 0), right.get(key, 0)
        if before != after:
            changes.append({"key": key, "baseline": before, "candidate": after, "delta": after - before})
    return changes


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    funnel = report.get("funnel", {})
    retention = report.get("retention", {})
    return {
        "run_id": report.get("run_id"),
        "title": report.get("spec", {}).get("title"),
        "input_sha256": report.get("input", {}).get("sha256"),
        "normalized_sha256": report.get("input", {}).get("normalized_sha256"),
        "spec_sha256": report.get("spec", {}).get("sha256"),
        "funnel": {
            "name": funnel.get("name"),
            "entered_users": funnel.get("entered_users"),
            "converted_users": funnel.get("converted_users"),
            "overall_conversion": funnel.get("overall_conversion"),
            "steps": [
                {"name": step.get("name"), "users": step.get("users")}
                for step in (funnel.get("steps") or [])
            ],
        },
        "retention": {
            "cohort_size": retention.get("cohort_size"),
            "periods": {
                row.get("label"): {
                    "retained_users": row.get("retained_users"),
                    "retention_rate": row.get("retention_rate"),
                }
                for row in (retention.get("periods") or [])
            },
        },
    }


def _verdict(
    *, metrics_changed: bool, config_changed: bool, input_changed: bool, engine_changed: bool
) -> tuple[str, str]:
    if not metrics_changed:
        if not (config_changed or input_changed or engine_changed):
            return "identical", "两次运行的引擎、输入与规格一致，核心指标无差异。"
        return "no_metric_change", "输入或规格有变化，但本次核心漏斗/留存指标没有变化。"
    if engine_changed:
        return "engine_changed", "引擎或语义版本变化，核心指标随之变化。"
    if config_changed and input_changed:
        return "config_and_input_changed", "分析规格与输入数据同时变化，无法仅凭本次对比隔离单一原因。"
    if config_changed:
        return "config_changed", "输入数据一致，指标变化可归因于声明式分析规格变化。"
    if input_changed:
        return "input_changed", "分析规格一致，指标变化可归因于输入事件数据变化。"
    return "unexplained_metric_change", "输入与规格均未变化但指标不同，请检查引擎版本或产物完整性。"


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise InputError("运行报告必须是 JSON 对象")
    baseline_id = baseline.get("run_id")
    candidate_id = candidate.get("run_id")
    if not baseline_id or not candidate_id:
        raise InputError("运行报告缺少 run_id")
    config_changes = _config_changes(
        baseline.get("spec", {}).get("document", {}),
        candidate.get("spec", {}).get("document", {}),
    )
    b_steps = baseline.get("funnel", {}).get("steps", []) or []
    c_steps = candidate.get("funnel", {}).get("steps", []) or []
    step_entries = _pair_funnel_steps(b_steps, c_steps)
    metrics = _metrics(baseline, candidate, step_entries)
    # 只有 matched 前缀的步骤能贡献数值变化；不可比项 changed 恒为 False，
    # 不会污染 metrics_changed 与 changed_metric_count。
    changed_metrics = [item for item in metrics if item["changed"]]
    base_input, cand_input = baseline.get("input", {}), candidate.get("input", {})
    input_changed = base_input.get("sha256") != cand_input.get("sha256")
    base_engine = baseline.get("engine", {})
    cand_engine = candidate.get("engine", {})
    engine_changed = (
        base_engine.get("version") != cand_engine.get("version")
        or base_engine.get("semantics_version") != cand_engine.get("semantics_version")
    )
    verdict, explanation = _verdict(
        metrics_changed=bool(changed_metrics),
        config_changed=bool(config_changes),
        input_changed=input_changed,
        engine_changed=engine_changed,
    )
    comparison_id = make_comparison_id(str(baseline_id), str(candidate_id))
    return {
        "schema_version": COMPARE_SCHEMA_VERSION,
        "comparison_id": comparison_id,
        "engine": {"version": base_engine.get("version"), "semantics_version": SEMANTICS_VERSION},
        "baseline": _summary(baseline),
        "candidate": _summary(candidate),
        "verdict": verdict,
        "explanation": explanation,
        "flags": {
            "config_changed": bool(config_changes),
            "input_changed": input_changed,
            "metrics_changed": bool(changed_metrics),
            "engine_changed": engine_changed,
        },
        "input": {
            "baseline_sha256": base_input.get("sha256"),
            "candidate_sha256": cand_input.get("sha256"),
            "baseline_normalized_sha256": base_input.get("normalized_sha256"),
            "candidate_normalized_sha256": cand_input.get("normalized_sha256"),
            "baseline_bytes": base_input.get("byte_count"),
            "candidate_bytes": cand_input.get("byte_count"),
        },
        "metrics": metrics,
        "changed_metric_count": len(changed_metrics),
        "funnel_steps": step_entries,
        "config_changes": config_changes,
        "quality_changes": _quality_changes(baseline, candidate),
    }
