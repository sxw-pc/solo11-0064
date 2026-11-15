from __future__ import annotations

from pathlib import Path

import pytest

from funnelcheck.errors import ErrorCode, FunnelCheckError, SpecError
from funnelcheck.spec import AnalysisSpecification, load_spec
from tests.support import spec_document, write_raw_spec, write_spec


def test_load_builtin_spec_is_stable(tmp_path: Path) -> None:
    spec_path = Path(__file__).parents[1] / "funnelcheck" / "fixtures" / "spec.yaml"
    first = load_spec(spec_path)
    second = load_spec(spec_path)
    assert isinstance(first.spec, AnalysisSpecification)
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64
    assert first.spec.analysis.future_grace_seconds == 3600
    assert first.spec.analysis.late_after_seconds == 172800
    assert [step.name for step in first.spec.funnel.steps][-1] == "完成购买"
    assert first.spec.retention.periods == [1, 3, 7]
    assert first.spec.segments[0].field == "channel"
    assert first.document["funnel"]["window"] == "7d"


def test_zero_future_grace_is_allowed_and_zero_window_is_not(tmp_path: Path) -> None:
    path = write_spec(tmp_path, analysis={"future_grace": "0h"})
    bundle = load_spec(path)
    assert bundle.spec.analysis.future_grace_seconds == 0

    bad = write_spec(tmp_path, name="bad.yaml", funnel={"window": "0d"})
    with pytest.raises(SpecError) as exc:
        load_spec(bad)
    assert "window" in exc.value.message


def test_invalid_timezone_is_rejected(tmp_path: Path) -> None:
    path = write_spec(tmp_path, analysis={"timezone": "Mars/Olympus"})
    with pytest.raises(SpecError, match="未知时区"):
        load_spec(path)


def test_duplicate_step_names_are_rejected(tmp_path: Path) -> None:
    spec = spec_document()
    spec["funnel"]["steps"][1]["name"] = spec["funnel"]["steps"][0]["name"]
    path = write_spec(tmp_path, funnel=spec["funnel"])
    with pytest.raises(SpecError, match="name 必须唯一"):
        load_spec(path)


def test_periods_reject_duplicates_and_booleans(tmp_path: Path) -> None:
    duplicate = write_spec(
        tmp_path, name="dupe.yaml", retention={"periods": [1, 1, 7]}
    )
    with pytest.raises(SpecError, match="不能重复"):
        load_spec(duplicate)
    boolean = write_spec(tmp_path, name="bool.yaml", retention={"periods": [True]})
    with pytest.raises(SpecError, match="1..365"):
        load_spec(boolean)


def test_month_unit_is_accepted_and_unknown_units_are_rejected(tmp_path: Path) -> None:
    month = write_spec(
        tmp_path,
        name="month.yaml",
        retention={"unit": "month", "periods": [1, 2]},
    )
    bundle = load_spec(month)
    assert bundle.spec.retention.unit == "month"
    assert bundle.document["retention"]["unit"] == "month"

    unknown = write_spec(
        tmp_path, name="quarter.yaml", retention={"unit": "quarter"}
    )
    with pytest.raises(SpecError, match="unit"):
        load_spec(unknown)


def test_unknown_field_and_invalid_where_are_rejected(tmp_path: Path) -> None:
    path = write_spec(tmp_path, mystery_field=True)
    with pytest.raises(SpecError, match="mystery_field"):
        load_spec(path)
    raw = (
        "spec_version: 1\n"
        "analysis:\n  timezone: UTC\n  as_of: '2026-03-01T00:00:00Z'\n"
        "funnel:\n  name: f\n  window: 7d\n  steps:\n"
        "    - name: a\n      event: view\n      where:\n        properties.x: [1, 2]\n"
        "    - name: b\n      event: buy\n"
        "retention:\n  cohort_event: signup\n  return_event: view\n  periods: [1]\n"
    )
    path = write_raw_spec(tmp_path, raw, name="where.yaml")
    with pytest.raises(SpecError, match="标量"):
        load_spec(path)


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    raw = (
        "spec_version: 1\n"
        "title: first\n"
        "title: second\n"
        "analysis:\n  timezone: UTC\n  as_of: '2026-03-01T00:00:00Z'\n"
        "funnel:\n  name: f\n  window: 7d\n  steps:\n"
        "    - name: a\n      event: view\n"
        "    - name: b\n      event: buy\n"
        "retention:\n  cohort_event: signup\n  return_event: view\n  periods: [1]\n"
    )
    path = write_raw_spec(tmp_path, raw)
    with pytest.raises(SpecError, match="YAML"):
        load_spec(path)


def test_spec_size_limit_and_naive_as_of(tmp_path: Path) -> None:
    path = write_spec(tmp_path, analysis={"timezone": "Asia/Shanghai", "as_of": "2026-03-01T08:00:00"})
    bundle = load_spec(path)
    assert bundle.spec.analysis.as_of.isoformat() == "2026-03-01T00:00:00+00:00"
    with pytest.raises(FunnelCheckError) as exc:
        load_spec(path, max_bytes=8)
    assert exc.value.code == ErrorCode.SPEC_TOO_LARGE


def test_invalid_yaml_root_and_missing_required(tmp_path: Path) -> None:
    root_list = write_raw_spec(tmp_path, "- a\n- b\n", name="list.yaml")
    with pytest.raises(SpecError, match="映射"):
        load_spec(root_list)
    no_retention = write_raw_spec(
        tmp_path,
        "spec_version: 1\nanalysis:\n  timezone: UTC\n  as_of: '2026-03-01T00:00:00Z'\n"
        "funnel:\n  name: f\n  window: 7d\n  steps:\n    - name: a\n      event: view\n"
        "    - name: b\n      event: buy\n",
        name="no-retention.yaml",
    )
    with pytest.raises(SpecError, match="retention"):
        load_spec(no_retention)
