"""漏斗步骤配对在 Markdown / 自包含 HTML / Web 模板中的展示测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from funnelcheck.engine.compare import compare_reports
from funnelcheck.render_html import render_compare_html
from funnelcheck.render_markdown import render_compare_markdown
from funnelcheck.report import dump_json
from funnelcheck.web import create_app
from tests.test_funnel_step_matching import _indexed, _report, _step


def _qualify_comparison() -> dict:
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
                _step("资格确认", "qualify", 90, {"tier": "gold"}),
                _step("结算", "checkout", 8),
                _step("购买", "buy", 3),
            ]
        ),
    )
    return compare_reports(baseline, candidate)


def test_markdown_renders_step_pairing() -> None:
    comparison = _qualify_comparison()
    text = render_compare_markdown(comparison)
    assert "## 漏斗步骤配对" in text
    assert "旧定义移除" in text and "新定义新增" in text and "可比" in text
    assert "qualify" in text and "资格确认" in text
    # 不可比行的差值列必须是 —，不能出现伪差值
    assert "+82" not in text
    # 指标差异区块只展示 changed 项；本场景没有步骤级 changed
    assert "核心指标无差异。" in text
    # 不可比步骤条目不得出现在指标差异表（仅出现在配对表）
    metric_section = text.split("## 指标差异")[1].split("## 漏斗步骤配对")[0]
    assert "qualify" not in metric_section
    # 配对表保留双方原值
    pairing_section = text.split("## 漏斗步骤配对")[1]
    assert "| 8 | — | — |" in pairing_section
    assert "| — | 8 | — |" in pairing_section


def test_html_renders_step_pairing() -> None:
    html = render_compare_html(_qualify_comparison())
    assert "漏斗步骤配对" in html
    assert "旧定义移除" in html and "新定义新增" in html
    assert "qualify" in html and "tier" in html
    assert "<script" not in html
    assert "http://" not in html and "https://" not in html


def test_legacy_status_renders_in_markdown_and_html() -> None:
    baseline = _report(
        "run-b", [{"index": 1, "name": "浏览", "users": 100}]
    )
    candidate = _report("run-c", _indexed([_step("浏览", "view", 100)]))
    comparison = compare_reports(baseline, candidate)
    markdown = render_compare_markdown(comparison)
    html = render_compare_html(comparison)
    assert "历史报告（仅位置）" in markdown
    assert "历史报告（仅位置）" in html


def test_web_template_renders_step_pairing(tmp_path: Path) -> None:
    comparison = _qualify_comparison()
    root = tmp_path / "runs"
    comp_dir = root / "comparisons" / comparison["comparison_id"]
    comp_dir.mkdir(parents=True)
    (comp_dir / "compare.json").write_text(
        dump_json(comparison),
        encoding="utf-8",
    )
    client = TestClient(create_app(root))
    page = client.get(f"/comparisons/{comparison['comparison_id']}")
    assert page.status_code == 200
    assert "漏斗步骤配对" in page.text
    assert "旧定义移除" in page.text and "新定义新增" in page.text
    assert "qualify" in page.text
    # 页面仍保留原有指标差异区块
    assert "指标差异" in page.text
