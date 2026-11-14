from __future__ import annotations

import json
from pathlib import Path

import pytest

from funnelcheck.errors import ErrorCode, FunnelCheckError
from funnelcheck.loader import load_events
from funnelcheck.normalize import normalize_batch
from funnelcheck.spec import load_spec
from funnelcheck.timeutil import get_zone, parse_timestamp
from tests.support import make_event, normalize_case, write_events, write_spec


def test_event_size_and_encoding_limits(tmp_path: Path) -> None:
    path = write_events(tmp_path, [make_event("a", "u", "view", "2026-03-01T00:00:00Z")])
    with pytest.raises(FunnelCheckError) as exc:
        load_events(path, max_bytes=8)
    assert exc.value.code == ErrorCode.INPUT_TOO_LARGE
    assert "20" not in exc.value.message or "8" in exc.value.message

    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(FunnelCheckError) as exc:
        load_events(bad)
    assert exc.value.code == ErrorCode.INPUT_ENCODING


def test_load_events_counts_rows_and_malformed(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event_id":"a"}\n\nnot-json\n[]\n',
        encoding="utf-8",
    )
    raw = load_events(path)
    assert raw.line_count == 4
    assert raw.blank_lines == 1
    assert len(raw.rows) == 3
    assert raw.rows[0].ok
    assert not raw.rows[1].ok
    assert not raw.rows[2].ok
    assert len(raw.sha256) == 64


def test_quality_pipeline_policies_and_counts(tmp_path: Path) -> None:
    lines = [
        json.dumps(make_event("evt-1", "u1", "view", "2026-03-01T10:00:00Z")),
        json.dumps(
            make_event("evt-1", "u1", "view", "2026-03-01T10:00:00Z", "2026-03-02T10:00:00Z")
        ),
        json.dumps(
            {
                "event_id": "evt-2",
                "event_name": "view",
                "occurred_at": "2026-03-01T10:00:00Z",
                "received_at": "2026-03-01T10:00:00Z",
            }
        ),
        json.dumps(make_event("evt-3", "u1", "view", "not-a-time")),
        json.dumps(
            make_event("evt-4", "u2", "signup", "2026-03-01T10:00:00Z", "2026-03-05T10:00:00Z")
        ),
        json.dumps(make_event("evt-5", "u3", "view", "2026-03-11T10:00:00Z")),
        json.dumps(make_event("evt-6", "u3", "buy", "2026-03-10T00:30:00Z")),
        '{"event_id":"broken"',
        "",
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    spec_path = write_spec(
        tmp_path,
        analysis={"future_grace": "1h", "as_of": "2026-03-10T00:00:00Z"},
    )
    raw = load_events(path)
    batch = normalize_batch(raw, load_spec(spec_path).spec)
    counts = batch.quality["counts"]
    assert counts["records_seen"] == 8
    assert counts["records_parsed"] == 7
    assert counts["blank_lines"] == 1
    assert counts["malformed_json"] == 1
    assert counts["missing_required"] == 1
    assert counts["invalid_timestamp"] == 1
    assert counts["duplicate_rows"] == 1
    assert counts["duplicate_groups"] == 1
    assert counts["late_rows"] == 1
    assert counts["late_excluded"] == 1
    assert counts["future_rows"] == 1
    assert counts["future_within_grace"] == 1
    assert counts["included_events"] == 2
    assert counts["included_users"] == 2
    assert counts["excluded_events"] == 6
    assert len(batch.events) == 2
    reasons = {item["reason"] for item in batch.quality["samples"]}
    assert {"malformed_json", "missing_required", "invalid_timestamp"} <= reasons


def test_late_policy_include_and_negative_latency(tmp_path: Path) -> None:
    events = [
        make_event("a", "u1", "view", "2026-03-01T10:00:00Z", "2026-03-06T10:00:00Z"),
        make_event("b", "u2", "view", "2026-03-02T10:00:00Z", "2026-03-02T09:00:00Z"),
    ]
    batch = normalize_case(
        tmp_path,
        events,
        analysis={"late_policy": "include"},
    )
    counts = batch.quality["counts"]
    assert counts["late_rows"] == 1
    assert counts["late_included"] == 1
    assert counts["late_excluded"] == 0
    assert counts["negative_latency"] == 1
    assert counts["included_events"] == 2


def test_dedup_keep_last_received(tmp_path: Path) -> None:
    events = [
        make_event("same", "u1", "view", "2026-03-01T10:00:00Z", properties={"x": 1}),
        make_event("same", "u1", "view", "2026-03-01T10:00:00Z", "2026-03-01T11:00:00Z", properties={"x": 2}),
    ]
    batch = normalize_case(
        tmp_path,
        events,
        name="last",
        events={"dedup_keep": "last_received"},
    )
    assert len(batch.events) == 1
    assert batch.events[0].value_for("properties.x") == 2
    assert batch.quality["counts"]["duplicate_rows"] == 1


def test_content_dedup_ignores_event_id(tmp_path: Path) -> None:
    events = [
        make_event("id-a", "u1", "view", "2026-03-01T10:00:00Z", channel="x"),
        make_event("id-b", "u1", "view", "2026-03-01T10:00:00Z", channel="x"),
    ]
    batch = normalize_case(tmp_path, events, events={"dedup_key": "content"})
    assert len(batch.events) == 1
    assert batch.quality["counts"]["duplicate_groups"] == 1


def test_normalized_hash_is_independent_of_input_order(tmp_path: Path) -> None:
    events = [
        make_event("a", "u1", "view", "2026-03-01T10:00:00Z"),
        make_event("b", "u1", "signup", "2026-03-01T10:01:00Z"),
        make_event("c", "u2", "view", "2026-03-01T11:00:00Z"),
    ]
    first = normalize_case(tmp_path, events, name="first")
    second = normalize_case(tmp_path, list(reversed(events)), name="second")
    assert first.normalized_sha256 == second.normalized_sha256
    assert first.raw_sha256 != second.raw_sha256


def test_dst_nonexistent_and_ambiguous(tmp_path: Path) -> None:
    try:
        get_zone("America/New_York")
    except ValueError:
        pytest.skip("时区数据库不可用")
    ambiguous = parse_timestamp("2026-11-01T01:30:00", get_zone("America/New_York"))
    assert ambiguous.value is not None and ambiguous.ambiguous
    nonexistent = parse_timestamp("2026-03-08T02:30:00", get_zone("America/New_York"))
    assert nonexistent.value is None
    assert nonexistent.reason == "timestamp_nonexistent_local"

    events = [
        make_event("amb", "u1", "view", "2026-11-01T01:30:00"),
        make_event("bad", "u1", "signup", "2026-03-08T02:30:00"),
    ]
    batch = normalize_case(
        tmp_path,
        events,
        analysis={"timezone": "America/New_York", "as_of": "2026-12-01T00:00:00Z"},
    )
    assert batch.quality["counts"]["ambiguous_local_time"] == 1
    assert batch.quality["counts"]["invalid_timestamp"] == 1
    assert batch.quality["counts"]["included_events"] == 1


def test_out_of_order_and_exact_naive_interpretation(tmp_path: Path) -> None:
    events = [
        make_event("late-line", "u1", "signup", "2026-03-01T11:00:00Z"),
        make_event("early-line", "u1", "view", "2026-03-01T10:00:00Z"),
    ]
    batch = normalize_case(tmp_path, events)
    assert batch.quality["counts"]["out_of_order"] == 1
    assert batch.events[0].occurred_at.isoformat() == "2026-03-01T10:00:00+00:00"
