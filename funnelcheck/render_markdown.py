"""可离线阅读的 Markdown 运行报告与对比报告。"""

from __future__ import annotations

from typing import Any

QUALITY_LABELS = {
    "records_seen": "非空记录行",
    "records_parsed": "成功解析 JSON",
    "blank_lines": "空白行",
    "malformed_json": "JSON 解析失败",
    "missing_required": "缺少必填字段",
    "invalid_timestamp": "无效时间",
    "ambiguous_local_time": "歧义本地时间",
    "invalid_properties": "properties 无效",
    "duplicate_rows": "重复行（移除）",
    "duplicate_groups": "重复组",
    "future_rows": "未来事件（排除）",
    "future_within_grace": "宽限内未来事件",
    "late_rows": "迟到事件",
    "late_included": "迟到但纳入",
    "late_excluded": "迟到且排除",
    "negative_latency": "接收早于发生",
    "out_of_order": "乱序事件",
    "excluded_events": "排除记录",
    "included_events": "纳入事件",
    "included_users": "纳入用户",
}

REASON_LABELS = {
    "malformed_json": "JSON 解析失败",
    "missing_required": "缺少必填字段",
    "invalid_timestamp": "无效时间",
    "invalid_properties": "properties 无效",
    "ambiguous_local_time": "歧义本地时间",
    "future_event": "未来事件",
    "late_event": "迟到事件",
    "out_of_order": "乱序事件",
}


def pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.2f}%"


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if text else "—"


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_cell(item) for item in row) + " |")
    lines.append("")
    return lines


def _short(value: Any) -> str:
    text = str(value or "")
    return text[:12] if text else "—"


def render_run_markdown(report: dict[str, Any]) -> str:
    spec = report.get("spec", {})
    document = spec.get("document", {})
    funnel = report.get("funnel", {})
    retention = report.get("retention", {})
    quality = report.get("quality", {})
    counts = quality.get("counts", {})
    lines = [f"# {spec.get('title', '漏斗与留存验算')}", ""]
    lines += [
        f"- 运行 ID：`{report.get('run_id', '')}`",
        f"- 引擎：{report.get('engine', {}).get('version')} / 语义 {report.get('engine', {}).get('semantics_version')}",
        f"- 输入 SHA-256：`{_short(report.get('input', {}).get('sha256'))}`",
        f"- 规范化 SHA-256：`{_short(report.get('input', {}).get('normalized_sha256'))}`",
        f"- 规格 SHA-256：`{_short(spec.get('sha256'))}`",
        f"- 分析时区：{document.get('analysis', {}).get('timezone')}",
        f"- as_of：{document.get('analysis', {}).get('as_of')}",
        "",
        "> 本报告由本地确定性引擎生成；不含任何外部资源引用。",
        "",
        "## 漏斗",
        "",
        f"- 名称：{funnel.get('name')}",
        f"- 窗口：{funnel.get('window')}（从首步事件起算，含端点）",
        f"- 入口用户：{funnel.get('entered_users', 0)}；完成用户：{funnel.get('converted_users', 0)}；整体转化：{pct(funnel.get('overall_conversion'))}",
        "",
    ]
    rows = [
        [
            step.get("index"),
            step.get("name"),
            step.get("event"),
            step.get("users"),
            pct(step.get("conversion_from_first")),
            pct(step.get("conversion_from_previous")),
            step.get("dropoff_from_previous"),
        ]
        for step in funnel.get("steps", [])
    ]
    lines += _table(["#", "步骤", "事件", "用户", "较首步", "较上一步", "上步流失"], rows)
    for field, values in funnel.get("by_segment", {}).items():
        lines += [f"### 漏斗分群：`{field}`", ""]
        segment_rows = [
            [
                item.get("value"),
                item.get("entered_users"),
                item.get("converted_users"),
                pct(item.get("overall_conversion")),
            ]
            for item in values
        ]
        lines += _table(["分群值", "入口用户", "完成用户", "转化率"], segment_rows)

    lines += [
        "## 留存",
        "",
        f"- Cohort 事件：`{retention.get('cohort_event')}`；回访事件：`{retention.get('return_event')}`",
        f"- 周期单位：{retention.get('unit')}；模式：{retention.get('mode')}；队列规模：{retention.get('cohort_size', 0)}",
        f"- 观察截止：{retention.get('methodology', {}).get('observation_cutoff')}",
        "",
    ]
    retention_rows = [
        [
            row.get("label"),
            row.get("eligible_users"),
            row.get("retained_users"),
            pct(row.get("retention_rate")),
        ]
        for row in retention.get("periods", [])
    ]
    lines += _table(["周期", "可观察用户", "留存用户", "留存率"], retention_rows)
    for field, values in retention.get("by_segment", {}).items():
        lines += [f"### 留存分群：`{field}`", ""]
        segment_rows = []
        for item in values:
            by_label = {row.get("label"): row for row in item.get("periods", [])}
            row = [item.get("value"), item.get("cohort_size")]
            row.extend(pct(by_label.get(period.get("label"), {}).get("retention_rate")) for period in retention.get("periods", []))
            segment_rows.append(row)
        headers = ["分群值", "队列规模"] + [row.get("label", "") for row in retention.get("periods", [])]
        lines += _table(headers, segment_rows)
    if retention.get("cohorts"):
        lines += ["### 按 Cohort 日期", ""]
        cohort_rows = []
        for cohort in retention.get("cohorts", []):
            row = [cohort.get("date"), cohort.get("cohort_size")]
            row.extend(pct(cohort.get("periods", {}).get(label, {}).get("retention_rate")) for label in [item.get("label") for item in retention.get("periods", [])])
            cohort_rows.append(row)
        headers = ["日期", "队列规模"] + [row.get("label", "") for row in retention.get("periods", [])]
        lines += _table(headers, cohort_rows)

    lines += ["## 数据质量", ""]
    count_rows = [[QUALITY_LABELS.get(key, key), value] for key, value in counts.items()]
    lines += _table(["检查项", "数量"], count_rows)
    sample_rows = [
        [item.get("line"), REASON_LABELS.get(item.get("reason"), item.get("reason")), item.get("event_id"), item.get("detail")]
        for item in quality.get("samples", [])
    ]
    if sample_rows:
        lines += ["### 样例", ""]
        lines += _table(["输入行", "原因", "事件 ID", "说明"], sample_rows)
    lines += [
        "### 计算规则",
        "",
        f"- 同刻事件顺序：`{quality.get('rules', {}).get('same_timestamp_order')}`",
        f"- 去重键/保留：`{quality.get('rules', {}).get('dedup_key')}` / `{quality.get('rules', {}).get('dedup_keep')}`",
        f"- 迟到阈值：{quality.get('rules', {}).get('late_after_seconds')} 秒；策略：`{quality.get('rules', {}).get('late_policy')}`",
        f"- 未来宽限：{quality.get('rules', {}).get('future_grace_seconds')} 秒",
        "",
        "## 复现",
        "",
        "```sh",
        "python -m funnelcheck run --events <events.jsonl> --spec <spec.yaml> --runs-dir runs",
        f"python -m funnelcheck compare --baseline {report.get('run_id')} --candidate <run_id>",
        "```",
        "",
    ]
    return "\n".join(lines)


STEP_STATUS_LABELS = {
    "matched": "可比",
    "removed": "旧定义移除",
    "added": "新定义新增",
    "legacy": "历史报告（仅位置）",
}


def _where_text(step: dict[str, Any] | None) -> str:
    if not step:
        return "—"
    where = step.get("where") or {}
    if not where:
        return "—"
    return "; ".join(f"{path}={_cell(value)}" for path, value in sorted(where.items()))


def _step_pair_rows(comparison: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for entry in comparison.get("funnel_steps", []):
        status = entry.get("status")
        left = entry.get("baseline_step") or {}
        right = entry.get("candidate_step") or {}
        if status == "matched":
            event = right.get("event") or left.get("event")
            where = _where_text(right)
            delta = "—" if entry.get("delta") is None else entry.get("delta")
        elif status == "removed":
            event, where, delta = left.get("event"), _where_text(left), "—"
        elif status == "added":
            event, where, delta = right.get("event"), _where_text(right), "—"
        else:
            event = right.get("event") or left.get("event") or "—"
            where = _where_text(right) if right else _where_text(left)
            delta = "—"
        rows.append(
            [
                STEP_STATUS_LABELS.get(status, status),
                entry.get("baseline_index") if entry.get("baseline_index") is not None else "—",
                entry.get("candidate_index") if entry.get("candidate_index") is not None else "—",
                left.get("name") if left else "—",
                right.get("name") if right else "—",
                event,
                where,
                entry.get("baseline") if entry.get("baseline") is not None else "—",
                entry.get("candidate") if entry.get("candidate") is not None else "—",
                delta,
            ]
        )
    return rows


def render_compare_markdown(comparison: dict[str, Any]) -> str:
    flags = comparison.get("flags", {})
    baseline = comparison.get("baseline", {})
    candidate = comparison.get("candidate", {})
    lines = [
        "# 运行对比报告",
        "",
        f"- 对比 ID：`{comparison.get('comparison_id', '')}`",
        f"- 基线：`{baseline.get('run_id')}` · {baseline.get('title')}",
        f"- 候选：`{candidate.get('run_id')}` · {candidate.get('title')}",
        f"- 判定：**{comparison.get('verdict')}**",
        f"- 说明：{comparison.get('explanation')}",
        "",
        "## 归因标记",
        "",
    ]
    flag_rows = [
        ["规格变化", flags.get("config_changed")],
        ["输入变化", flags.get("input_changed")],
        ["指标变化", flags.get("metrics_changed")],
        ["引擎变化", flags.get("engine_changed")],
    ]
    lines += _table(["标记", "结果"], flag_rows)
    lines += ["## 指标差异", ""]
    changed = [item for item in comparison.get("metrics", []) if item.get("changed")]
    if changed:
        rows = [
            [
                item.get("label"),
                item.get("baseline"),
                item.get("candidate"),
                item.get("delta"),
                f"{float(item['delta_pct']):+.2f}%"
                if item.get("delta_pct") is not None
                else "—",
            ]
            for item in changed
        ]
        lines += _table(["指标", "基线", "候选", "差值", "相对变化"], rows)
    else:
        lines += ["核心指标无差异。", ""]
    step_rows = _step_pair_rows(comparison)
    lines += ["## 漏斗步骤配对", ""]
    if step_rows:
        lines += [
            "> 步骤身份按截至当前步的完整有序谓词前缀匹配；仅“可比”项参与数值差值与指标变化统计，不可比项保留双方原值。",
            "",
        ]
        lines += _table(
            [
                "状态",
                "基线下标",
                "候选下标",
                "基线步骤",
                "候选步骤",
                "事件",
                "谓词",
                "基线用户",
                "候选用户",
                "差值",
            ],
            step_rows,
        )
    else:
        lines += ["两次运行均没有漏斗步骤。", ""]
    lines += ["## 规格差异", ""]
    config_rows = []
    for item in comparison.get("config_changes", []):
        config_rows.append(
            [item.get("path"), item.get("change"), item.get("baseline"), item.get("candidate")]
        )
    if config_rows:
        lines += _table(["路径", "变化", "基线", "候选"], config_rows)
    else:
        lines += ["分析规格一致。", ""]
    quality = comparison.get("quality_changes", [])
    lines += ["## 数据质量差异", ""]
    if quality:
        lines += _table(
            ["检查项", "基线", "候选", "差值"],
            [[item.get("key"), item.get("baseline"), item.get("candidate"), item.get("delta")] for item in quality],
        )
    else:
        lines += ["质量计数一致。", ""]
    lines += [
        "## 输入指纹",
        "",
        f"- 基线原始 SHA-256：`{comparison.get('input', {}).get('baseline_sha256')}`",
        f"- 候选原始 SHA-256：`{comparison.get('input', {}).get('candidate_sha256')}`",
        f"- 基线规范化 SHA-256：`{comparison.get('input', {}).get('baseline_normalized_sha256')}`",
        f"- 候选规范化 SHA-256：`{comparison.get('input', {}).get('candidate_normalized_sha256')}`",
        "",
    ]
    return "\n".join(lines)
