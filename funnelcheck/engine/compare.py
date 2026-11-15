"""两次运行的指标、配置、输入差异与归因判定。"""

from __future__ import annotations

from typing import Any

from ..config import COMPARE_SCHEMA_VERSION, SEMANTICS_VERSION, canonical_json, make_comparison_id
from ..errors import InputError

_MISSING = object()


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
        if before is not _MISSING and after is not _MISSING:
            if canonical_json(before) == canonical_json(after):
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


def _metrics(base: dict[str, Any], cand: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    bf, cf = base.get("funnel", {}), cand.get("funnel", {})
    _metric(items, "funnel.entered_users", "漏斗入口用户", bf.get("entered_users"), cf.get("entered_users"))
    _metric(items, "funnel.converted_users", "漏斗完成用户", bf.get("converted_users"), cf.get("converted_users"))
    _metric(items, "funnel.overall_conversion", "漏斗整体转化率", bf.get("overall_conversion"), cf.get("overall_conversion"))
    b_steps, c_steps = bf.get("steps", []), cf.get("steps", [])
    for index in range(max(len(b_steps), len(c_steps))):
        left = b_steps[index] if index < len(b_steps) else {}
        right = c_steps[index] if index < len(c_steps) else {}
        name = right.get("name") or left.get("name") or "?"
        _metric(
            items,
            f"funnel.steps.{index}.users",
            f"漏斗步骤 {index + 1} · {name} · 用户",
            left.get("users"),
            right.get("users"),
        )
    br, cr = base.get("retention", {}), cand.get("retention", {})
    _metric(items, "retention.cohort_size", "留存队列规模", br.get("cohort_size"), cr.get("cohort_size"))
    b_periods = {row.get("label"): row for row in br.get("periods", [])}
    c_periods = {row.get("label"): row for row in cr.get("periods", [])}
    labels = sorted(set(b_periods) | set(c_periods), key=lambda item: _label_sort(item))
    for label in labels:
        left = b_periods.get(label, {})
        right = c_periods.get(label, {})
        _metric(items, f"retention.periods.{label}.eligible_users", f"{label} 可观察用户", left.get("eligible_users"), right.get("eligible_users"))
        _metric(items, f"retention.periods.{label}.retained_users", f"{label} 留存用户", left.get("retained_users"), right.get("retained_users"))
        _metric(items, f"retention.periods.{label}.retention_rate", f"{label} 留存率", left.get("retention_rate"), right.get("retention_rate"))
    return items


# 周期标签按单位分组排序；比较跨单位报告时 M1 不会与 D1/W1 因位置相邻而被混用。
_LABEL_UNIT_ORDER = {"D": 0, "W": 1, "M": 2}


def _label_sort(label: str) -> tuple[int, str, int]:
    text = str(label or "").strip()
    prefix = text[:1].upper()
    number = "".join(char for char in text[1:] if char.isdigit())
    return (_LABEL_UNIT_ORDER.get(prefix, len(_LABEL_UNIT_ORDER)), prefix, int(number or 0))


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
                for step in funnel.get("steps", [])
            ],
        },
        "retention": {
            "cohort_size": retention.get("cohort_size"),
            "periods": {
                row.get("label"): {
                    "retained_users": row.get("retained_users"),
                    "retention_rate": row.get("retention_rate"),
                }
                for row in retention.get("periods", [])
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
    metrics = _metrics(baseline, candidate)
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
        "config_changes": config_changes,
        "quality_changes": _quality_changes(baseline, candidate),
    }
