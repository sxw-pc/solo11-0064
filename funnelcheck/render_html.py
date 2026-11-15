"""生成无外部依赖、可单独打开的自包含 HTML 报告。"""

from __future__ import annotations

from html import escape
from typing import Any

from .render_markdown import QUALITY_LABELS, REASON_LABELS

_CSS = """
:root{--ink:#15231f;--muted:#64716c;--line:#d9e2dd;--paper:#fbfdfb;--card:#fff;
--accent:#0d7a5f;--accent-soft:#dff3ec;--warn:#9a5b00;--danger:#9d2f38}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.55 "Segoe UI","Noto Sans SC",system-ui,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 22px 60px}
header{border-bottom:3px solid var(--ink);padding-bottom:18px;margin-bottom:24px}
h1{font-size:30px;line-height:1.2;margin:0 0 8px}h2{font-size:20px;margin:32px 0 12px}
h3{font-size:16px;margin:22px 0 8px}.sub{color:var(--muted);word-break:break-all}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .label{color:var(--muted);font-size:12px}.card .value{font-size:24px;font-weight:700}
table{width:100%;border-collapse:collapse;margin:10px 0 20px;background:var(--card)}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase}
.bar{height:7px;background:#eef3f0;border-radius:99px;overflow:hidden;margin-top:5px}
.bar span{display:block;height:100%;background:var(--accent)}
.chip{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:2px 9px;
margin:0 6px 6px 0;font-size:12px}.chip.on{border-color:var(--accent);background:var(--accent-soft)}
.verdict{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--accent);
padding:18px;border-radius:8px}.verdict strong{font-size:19px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.panel{background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:14px}.muted{color:var(--muted)}
code{background:#eef3f0;padding:1px 5px;border-radius:4px;word-break:break-all}
pre{white-space:pre-wrap;background:#eef3f0;padding:10px;border-radius:8px;overflow:auto}
footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
.warn{color:var(--warn)}.neg{color:var(--danger)}
@media(max-width:760px){.cards,.grid{grid-template-columns:1fr 1fr}}
@media print{body{background:#fff}.card,.panel,table{break-inside:avoid}}
"""


def _e(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        value = f"{value:g}"
    return escape(str(value), quote=True)


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.2f}%"


def _bar(value: int, maximum: int) -> str:
    width = 0 if maximum <= 0 else max(0, min(100, round(value / maximum * 100)))
    return f'<div class="bar"><span style="width:{width}%"></span></div>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _shell(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; img-src data:\">"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _funnel_section(funnel: dict[str, Any]) -> str:
    maximum = max([step.get("users", 0) for step in funnel.get("steps", [])] or [0])
    rows = []
    for step in funnel.get("steps", []):
        rows.append(
            [
                _e(step.get("index")),
                f"<strong>{_e(step.get('name'))}</strong><br><span class=\"muted\"><code>{_e(step.get('event'))}</code></span>",
                f"{_e(step.get('users'))}{_bar(step.get('users', 0), maximum)}",
                _e(_pct(step.get("conversion_from_first"))),
                _e(_pct(step.get("conversion_from_previous"))),
                _e(step.get("dropoff_from_previous")),
            ]
        )
    table = _table(["#", "步骤", "用户", "较首步", "较上步", "上步流失"], rows)
    segments = ""
    for field, values in funnel.get("by_segment", {}).items():
        seg_rows = [
            [_e(item.get("value")), _e(item.get("entered_users")), _e(item.get("converted_users")), _e(_pct(item.get("overall_conversion")))]
            for item in values
        ]
        segments += f"<h3>漏斗分群 · <code>{_e(field)}</code></h3>" + _table(["分群值", "入口", "完成", "转化率"], seg_rows)
    return (
        "<h2>漏斗</h2>"
        f"<p class=\"sub\">{_e(funnel.get('name'))} · 窗口 {_e(funnel.get('window'))} · "
        "从首步起算（含端点）</p>" + table + segments
    )


def _retention_section(retention: dict[str, Any]) -> str:
    rows = [
        [
            _e(row.get("label")),
            _e(row.get("eligible_users")),
            _e(row.get("retained_users")),
            _e(_pct(row.get("retention_rate"))),
        ]
        for row in retention.get("periods", [])
    ]
    table = _table(["周期", "可观察用户", "留存用户", "留存率"], rows)
    cohort_rows = []
    labels = [row.get("label", "") for row in retention.get("periods", [])]
    for cohort in retention.get("cohorts", []):
        row = [_e(cohort.get("date")), _e(cohort.get("cohort_size"))]
        row.extend(_e(_pct(cohort.get("periods", {}).get(label, {}).get("retention_rate"))) for label in labels)
        cohort_rows.append(row)
    cohort_table = _table(["Cohort 日期", "规模", *labels], cohort_rows) if cohort_rows else ""
    segments = ""
    for field, values in retention.get("by_segment", {}).items():
        seg_rows = []
        for item in values:
            by_label = {row.get("label"): row for row in item.get("periods", [])}
            row = [_e(item.get("value")), _e(item.get("cohort_size"))]
            row.extend(_e(_pct(by_label.get(label, {}).get("retention_rate"))) for label in labels)
            seg_rows.append(row)
        segments += f"<h3>留存分群 · <code>{_e(field)}</code></h3>" + _table(["分群值", "规模", *labels], seg_rows)
    return (
        "<h2>留存</h2>"
        f"<p class=\"sub\">Cohort <code>{_e(retention.get('cohort_event'))}</code> → "
        f"回访 <code>{_e(retention.get('return_event'))}</code> · {_e(retention.get('unit'))} · "
        f"{_e(retention.get('mode'))} · 观察截止 {_e(retention.get('methodology', {}).get('observation_cutoff'))}</p>"
        + table + segments + (f"<h3>按 Cohort 日期</h3>{cohort_table}" if cohort_table else "")
    )


def _quality_section(quality: dict[str, Any]) -> str:
    counts = quality.get("counts", {})
    cards = "".join(
        f'<div class="card"><div class="label">{_e(QUALITY_LABELS.get(key, key))}</div>'
        f'<div class="value">{_e(value)}</div></div>'
        for key, value in counts.items()
        if key in ("records_seen", "included_events", "included_users", "excluded_events", "duplicate_rows", "late_rows", "future_rows", "invalid_timestamp")
    )
    samples = quality.get("samples", [])
    sample_rows = [
        [_e(item.get("line")), _e(REASON_LABELS.get(item.get("reason"), item.get("reason"))), _e(item.get("event_id")), _e(item.get("detail"))]
        for item in samples
    ]
    sample_html = "<h3>质量样例</h3>" + _table(["输入行", "原因", "事件 ID", "说明"], sample_rows) if sample_rows else ""
    rules = quality.get("rules", {})
    rule_rows = [
        ["同刻顺序", f"<code>{_e(rules.get('same_timestamp_order'))}</code>"],
        ["去重", f"<code>{_e(rules.get('dedup_key'))}</code> / <code>{_e(rules.get('dedup_keep'))}</code>"],
        ["迟到阈值", f"{_e(rules.get('late_after_seconds'))} 秒 / <code>{_e(rules.get('late_policy'))}</code>"],
        ["未来宽限", f"{_e(rules.get('future_grace_seconds'))} 秒"],
    ]
    return (
        "<h2>数据质量</h2>" f'<div class="cards">{cards}</div>' + sample_html
        + "<h3>计算规则</h3>" + _table(["规则", "取值"], rule_rows)
    )


def render_run_html(report: dict[str, Any]) -> str:
    spec = report.get("spec", {})
    funnel = report.get("funnel", {})
    retention = report.get("retention", {})
    quality = report.get("quality", {})
    title = spec.get("title", "漏斗与留存验算")
    cards = (
        '<div class="cards">'
        f'<div class="card"><div class="label">漏斗入口</div><div class="value">{_e(funnel.get("entered_users"))}</div></div>'
        f'<div class="card"><div class="label">漏斗完成</div><div class="value">{_e(funnel.get("converted_users"))}</div></div>'
        f'<div class="card"><div class="label">整体转化</div><div class="value">{_e(_pct(funnel.get("overall_conversion")))}</div></div>'
        f'<div class="card"><div class="label">留存队列</div><div class="value">{_e(retention.get("cohort_size"))}</div></div>'
        "</div>"
    )
    meta = (
        f"<header><h1>{_e(title)}</h1><div class=\"sub\">运行 <code>{_e(report.get('run_id'))}</code> · "
        f"引擎 {_e(report.get('engine', {}).get('version'))} · 语义 {_e(report.get('engine', {}).get('semantics_version'))}"
        f"<br>输入 SHA-256 <code>{_e(report.get('input', {}).get('sha256'))}</code>"
        f"<br>规格 SHA-256 <code>{_e(spec.get('sha256'))}</code></div></header>"
    )
    body = meta + cards + _funnel_section(funnel) + _retention_section(retention) + _quality_section(quality)
    body += (
        "<footer>漏斗验算台 · 本地确定性报告 · 无外部字体、脚本、图表库或网络请求。</footer>"
    )
    return _shell(title, body)


STEP_STATUS_LABELS = {
    "matched": "可比",
    "removed": "旧定义移除",
    "added": "新定义新增",
    "legacy": "历史报告（仅位置）",
}


def _where_html(step: dict[str, Any] | None) -> str:
    if not step:
        return '<span class="muted">—</span>'
    where = step.get("where") or {}
    if not where:
        return '<span class="muted">—</span>'
    text = "; ".join(f"{path}={_e(value)}" for path, value in sorted(where.items()))
    return f"<code>{text}</code>"


def _funnel_step_section(comparison: dict[str, Any]) -> str:
    entries = comparison.get("funnel_steps", [])
    if not entries:
        return "<h2>漏斗步骤配对</h2><p class=\"muted\">两次运行均没有漏斗步骤。</p>"
    rows = []
    for entry in entries:
        status = entry.get("status")
        left = entry.get("baseline_step") or {}
        right = entry.get("candidate_step") or {}
        if status == "matched":
            event = right.get("event") or left.get("event")
            where = _where_html(right)
            delta = "—" if entry.get("delta") is None else _e(entry.get("delta"))
        elif status == "removed":
            event, where, delta = _e(left.get("event")), _where_html(left), "—"
        elif status == "added":
            event, where, delta = _e(right.get("event")), _where_html(right), "—"
        else:
            event = right.get("event") or left.get("event") or "—"
            where = _where_html(right) if right else _where_html(left)
            delta = "—"
        base_index = entry.get("baseline_index")
        cand_index = entry.get("candidate_index")
        rows.append(
            [
                _e(STEP_STATUS_LABELS.get(status, status)),
                _e(base_index if base_index is not None else "—"),
                _e(cand_index if cand_index is not None else "—"),
                _e(left.get("name")) if left else '<span class="muted">—</span>',
                _e(right.get("name")) if right else '<span class="muted">—</span>',
                f"<code>{_e(event)}</code>",
                where,
                _e(entry.get("baseline")) if entry.get("baseline") is not None else "—",
                _e(entry.get("candidate")) if entry.get("candidate") is not None else "—",
                delta,
            ]
        )
    table = _table(
        ["状态", "基线下标", "候选下标", "基线步骤", "候选步骤", "事件", "谓词", "基线用户", "候选用户", "差值"],
        rows,
    )
    return (
        "<h2>漏斗步骤配对</h2>"
        '<p class="sub">步骤身份按截至当前步的完整有序谓词前缀匹配；仅“可比”项参与数值差值与指标变化统计，'
        "不可比项保留双方原值且不产生差值。</p>"
        + table
    )


def _changed_metric_rows(comparison: dict[str, Any]) -> str:
    rows = []
    for item in comparison.get("metrics", []):
        if not item.get("changed"):
            continue
        pct = item.get("delta_pct")
        rows.append(
            [
                _e(item.get("label")),
                _e(item.get("baseline")),
                _e(item.get("candidate")),
                _e(item.get("delta")),
                "—" if pct is None else f"{float(pct):.2f}%",
            ]
        )
    if not rows:
        return "<p class=\"muted\">核心指标无差异。</p>"
    return _table(["指标", "基线", "候选", "差值", "相对变化"], rows)


def render_compare_html(comparison: dict[str, Any]) -> str:
    flags = comparison.get("flags", {})
    baseline = comparison.get("baseline", {})
    candidate = comparison.get("candidate", {})
    chips = "".join(
        f'<span class="chip {"on" if value else ""}">{_e(label)}</span>'
        for label, value in (
            ("规格变化", flags.get("config_changed")),
            ("输入变化", flags.get("input_changed")),
            ("指标变化", flags.get("metrics_changed")),
            ("引擎变化", flags.get("engine_changed")),
        )
    )
    config_rows = [
        [
            f"<code>{_e(item.get('path'))}</code>",
            _e(item.get("change")),
            _e(item.get("baseline")),
            _e(item.get("candidate")),
        ]
        for item in comparison.get("config_changes", [])
    ]
    config_html = (
        _table(["路径", "变化", "基线", "候选"], config_rows)
        if config_rows
        else "<p class=\"muted\">分析规格一致。</p>"
    )
    input_rows = [
        ["基线原始 SHA-256", f"<code>{_e(comparison.get('input', {}).get('baseline_sha256'))}</code>"],
        ["候选原始 SHA-256", f"<code>{_e(comparison.get('input', {}).get('candidate_sha256'))}</code>"],
        ["基线规范化 SHA-256", f"<code>{_e(comparison.get('input', {}).get('baseline_normalized_sha256'))}</code>"],
        ["候选规范化 SHA-256", f"<code>{_e(comparison.get('input', {}).get('candidate_normalized_sha256'))}</code>"],
    ]
    body = (
        f"<header><h1>运行对比</h1><div class=\"sub\">对比 <code>{_e(comparison.get('comparison_id'))}</code></div></header>"
        f'<div class="verdict"><strong>{_e(comparison.get("verdict"))}</strong><p>{_e(comparison.get("explanation"))}</p><div>{chips}</div></div>'
        f'<div class="grid" style="margin-top:18px">'
        f'<div class="panel"><div class="label muted">基线</div><code>{_e(baseline.get("run_id"))}</code><br>{_e(baseline.get("title"))}</div>'
        f'<div class="panel"><div class="label muted">候选</div><code>{_e(candidate.get("run_id"))}</code><br>{_e(candidate.get("title"))}</div>'
        "</div><h2>指标差异</h2>" + _changed_metric_rows(comparison)
        + _funnel_step_section(comparison)
        + "<h2>规格差异</h2>" + config_html
        + "<h2>输入指纹</h2>" + _table(["项目", "值"], input_rows)
        + "<footer>差异分类基于原始输入哈希、规格文档与核心指标；不进行因果推断。</footer>"
    )
    return _shell("运行对比", body)
