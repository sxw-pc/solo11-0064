"""增量计算缓存的正确性与失效安全测试。

覆盖：追加/删除/用户迁移、event_id 跨用户冲突、content 去重、
规格/语义变化拒绝复用、中断重试与证据链断裂时的全量回退。
所有增量结果都与关闭缓存的全量重跑逐字段比对。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from funnelcheck import config, incremental
from funnelcheck.ledger import get_run
from funnelcheck.pipeline import execute_run, ledger_path
from funnelcheck.spec import load_spec
from tests.support import make_event, write_events, write_spec


# ---------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------


def _base_events() -> list[dict]:
    return [
        make_event("a1", "u-a", "view", "2026-03-01T10:00:00Z"),
        make_event("a2", "u-a", "signup", "2026-03-01T10:01:00Z"),
        make_event("a3", "u-a", "buy", "2026-03-01T10:02:00Z"),
        make_event("b1", "u-b", "view", "2026-03-01T11:00:00Z"),
        make_event("b2", "u-b", "signup", "2026-03-01T11:01:00Z"),
        make_event("b3", "u-b", "buy", "2026-03-01T11:02:00Z"),
        make_event("c1", "u-c", "view", "2026-03-01T12:00:00Z"),
        make_event("c2", "u-c", "signup", "2026-03-01T12:01:00Z"),
    ]


def _full_rerun(events_path: Path, spec_path: Path, full_root: Path) -> dict:
    outcome = execute_run(
        events_path, spec_path, full_root, use_incremental_cache=False
    )
    assert outcome.compute_mode == "full"
    return outcome.report


@pytest.fixture()
def case(tmp_path: Path):
    spec_path = write_spec(tmp_path)
    root = tmp_path / "runs"
    full_root = tmp_path / "runs-full"
    events_path = write_events(tmp_path, _base_events(), name="events-1.jsonl")
    first = execute_run(events_path, spec_path, root)
    assert first.compute_mode == "full"
    return {
        "tmp": tmp_path,
        "spec": spec_path,
        "root": root,
        "full_root": full_root,
        "first": first,
        "write": lambda rows, name="events-next.jsonl": write_events(
            tmp_path, rows, name=name
        ),
    }


def _cache_slot(case: dict) -> Path:
    spec_bundle = load_spec(case["spec"])
    return incremental.cache_slot_path(case["root"], spec_bundle)


def _assert_matches_full(case: dict, events_path: Path, outcome) -> None:
    full_report = _full_rerun(events_path, case["spec"], case["full_root"])
    assert outcome.report == full_report


# ---------------------------------------------------------------------------
# 基础：全量建缓存、追加只重算受影响用户
# ---------------------------------------------------------------------------


def test_first_run_is_full_and_writes_cache(case: dict) -> None:
    slot = _cache_slot(case)
    assert slot.is_file()
    state = json.loads(slot.read_text(encoding="utf-8"))
    assert state["semantics_version"] == config.SEMANTICS_VERSION
    assert state["spec_sha256"] == load_spec(case["spec"]).sha256
    assert state["dedup"] == {
        "dedup_key": "event_id",
        "dedup_keep": "first_received",
        "same_timestamp_order": "received_then_id",
    }
    assert set(state["users"]) == {"u-a", "u-b", "u-c"}
    # 每个用户同时带漏斗与留存贡献（无命中时为 null）。
    for contribution in state["users"].values():
        assert set(contribution) == {"funnel", "retention"}
    # 全局 event_id → 来源用户/事件映射存在且跨用户。
    assert state["groups"]["a1"][0]["user_id"] == "u-a"
    assert all(
        {"identity", "line_no", "user_id"} == set(entry)
        for entries in state["groups"].values()
        for entry in entries
    )


def test_append_only_recomputes_new_user(case: dict) -> None:
    events = _base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z"),
        make_event("d2", "u-d", "signup", "2026-03-02T09:01:00Z"),
    ]
    events_path = case["write"](events)
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "incremental"
    assert outcome.incremental["reused_user_count"] == 3
    assert outcome.incremental["recomputed_user_count"] == 1
    assert outcome.incremental["affected_group_count"] == 2
    _assert_matches_full(case, events_path, outcome)

    row = get_run(ledger_path(case["root"]), outcome.run_id)
    assert row is not None
    assert row["compute_mode"] == "incremental"
    assert row["cache_reused_users"] == 3
    assert row["cache_recomputed_users"] == 1
    assert row["contribution_sha256"] == outcome.incremental["contribution_sha256"]


def test_append_event_to_existing_user_recomputes_only_that_user(case: dict) -> None:
    events = _base_events() + [
        make_event("c3", "u-c", "buy", "2026-03-01T12:05:00Z"),
    ]
    events_path = case["write"](events)
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "incremental"
    assert outcome.incremental["reused_user_count"] == 2
    assert outcome.incremental["recomputed_user_count"] == 1
    _assert_matches_full(case, events_path, outcome)


def test_delete_events_recomputes_affected_users_and_global_counts(case: dict) -> None:
    events = _base_events()[:3]  # 仅保留 u-a
    events_path = case["write"](events, name="events-deleted.jsonl")
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "incremental"
    assert outcome.incremental["reused_user_count"] == 1
    assert outcome.incremental["recomputed_user_count"] == 0
    _assert_matches_full(case, events_path, outcome)
    # 缓存状态里被删除用户的贡献与组映射一并消失。
    state = json.loads(_cache_slot(case).read_text(encoding="utf-8"))
    assert set(state["users"]) == {"u-a"}
    assert all(
        entry["user_id"] == "u-a"
        for entries in state["groups"].values()
        for entry in entries
    )


def test_same_input_rerun_reuses_and_keeps_artifacts_deterministic(case: dict) -> None:
    events_path = case["write"](_base_events(), name="events-1-copy.jsonl")
    # 同字节内容 → 同 run_id；直接复用已完成产物。
    first = case["first"]
    rerun = execute_run(events_path, case["spec"], case["root"])
    assert rerun.run_id == first.run_id
    assert rerun.report == first.report
    for name in ("report.json", "report.md", "report.html", "manifest.json"):
        assert (rerun.artifact_dir / name).read_bytes() == (
            first.artifact_dir / name
        ).read_bytes()


# ---------------------------------------------------------------------------
# event_id 跨用户迁移与冲突
# ---------------------------------------------------------------------------


def test_event_id_migration_between_users_recomputes_both_owners(case: dict) -> None:
    events = [dict(item) for item in _base_events()]
    # 重复 ID a1 从 u-a 移动到 u-b。
    events[0] = make_event("a1", "u-b", "view", "2026-03-01T10:00:00Z")
    events_path = case["write"](events, name="events-migrated.jsonl")
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "incremental"
    assert outcome.incremental["recomputed_user_count"] == 2
    assert outcome.incremental["reused_user_count"] == 1
    _assert_matches_full(case, events_path, outcome)
    state = json.loads(_cache_slot(case).read_text(encoding="utf-8"))
    assert state["groups"]["a1"][0]["user_id"] == "u-b"
    assert "a1" not in {
        entry_key
        for entry_key, entries in state["groups"].items()
        for entry in entries
        if entry["user_id"] == "u-a"
    }


def test_cross_user_event_id_conflict_recomputes_all_owners(case: dict) -> None:
    events = _base_events() + [
        make_event("a1", "u-d", "view", "2026-03-01T09:00:00Z"),
    ]
    events_path = case["write"](events, name="events-conflict.jsonl")
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "incremental"
    # a1 同时出现在 u-a 与 u-d：两个归属用户都必须重算，u-b/u-c 复用。
    assert outcome.incremental["recomputed_user_count"] == 2
    assert outcome.incremental["reused_user_count"] == 2
    _assert_matches_full(case, events_path, outcome)


def test_line_number_shuffle_invalidates_affected_groups(case: dict) -> None:
    events = list(reversed(_base_events()))
    events_path = case["write"](events, name="events-shuffled.jsonl")
    outcome = execute_run(events_path, case["spec"], case["root"])
    # 行号是组身份的一部分：重排后无法证明任何组未变，保守全量重算用户。
    assert outcome.compute_mode == "incremental"
    assert outcome.incremental["reused_user_count"] == 0
    _assert_matches_full(case, events_path, outcome)


# ---------------------------------------------------------------------------
# content 去重：规范化完整事件键
# ---------------------------------------------------------------------------


def test_content_dedup_uses_canonical_event_keys(tmp_path: Path) -> None:
    spec_path = write_spec(tmp_path, events={"dedup_key": "content"})
    root = tmp_path / "runs"
    full_root = tmp_path / "runs-full"
    events_path = write_events(tmp_path, _base_events(), name="c-1.jsonl")
    first = execute_run(events_path, spec_path, root)
    assert first.compute_mode == "full"

    # 改 event_id 但事件内容不变：content 键不变，应当零受影响组。
    events = [dict(item, event_id=f"renamed-{item['event_id']}") for item in _base_events()]
    renamed_path = write_events(tmp_path, events, name="c-2.jsonl")
    outcome = execute_run(renamed_path, spec_path, root)
    assert outcome.compute_mode == "incremental"
    assert outcome.incremental["affected_group_count"] == 0
    full_report = execute_run(
        renamed_path, spec_path, full_root, use_incremental_cache=False
    ).report
    assert outcome.report == full_report

    # 修改一个事件的内容：仅该规范化键受影响。
    events[-1] = make_event("c2", "u-c", "signup", "2026-03-01T12:09:00Z")
    changed_path = write_events(tmp_path, events, name="c-3.jsonl")
    changed = execute_run(changed_path, spec_path, root)
    assert changed.compute_mode == "incremental"
    assert changed.incremental["reused_user_count"] == 2
    assert changed.incremental["recomputed_user_count"] == 1
    full_changed = execute_run(
        changed_path, spec_path, full_root, use_incremental_cache=False
    ).report
    assert changed.report == full_changed


# ---------------------------------------------------------------------------
# 规格与语义变化：拒绝增量复用
# ---------------------------------------------------------------------------


def test_spec_change_rejects_reuse_and_uses_separate_slot(case: dict) -> None:
    spec_two = write_spec(case["tmp"], name="spec-two.yaml", funnel={"window": "3d"})
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])
    outcome = execute_run(events_path, spec_two, case["root"])
    assert outcome.compute_mode == "full"
    slots = list((case["root"] / "cache").glob("slot-*.json"))
    assert len(slots) == 2
    # 新槽位绑定新 spec_sha256。
    state = json.loads(
        incremental.cache_slot_path(case["root"], load_spec(spec_two)).read_text(
            encoding="utf-8"
        )
    )
    assert state["spec_sha256"] == load_spec(spec_two).sha256


def test_dedup_rule_change_rejects_reuse(tmp_path: Path) -> None:
    spec_one = write_spec(tmp_path, name="s1.yaml")
    spec_two = write_spec(
        tmp_path, name="s2.yaml", events={"dedup_keep": "last_received"}
    )
    root = tmp_path / "runs"
    events_path = write_events(tmp_path, _base_events(), name="d.jsonl")
    execute_run(events_path, spec_one, root)
    outcome = execute_run(events_path, spec_two, root)
    assert outcome.compute_mode == "full"


def test_semantics_version_bump_rejects_reuse(case: dict, monkeypatch) -> None:
    monkeypatch.setattr(config, "SEMANTICS_VERSION", "2099.01.1")
    monkeypatch.setattr(incremental, "SEMANTICS_VERSION", "2099.01.1")
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "full"
    # 语义升级后建立的新缓存再次变化时可正常增量。
    again_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z"),
        make_event("e1", "u-e", "view", "2026-03-02T10:00:00Z"),
    ], name="events-again.jsonl")
    again = execute_run(again_path, case["spec"], case["root"])
    assert again.compute_mode == "incremental"
    _assert_matches_full(case, again_path, again)


# ---------------------------------------------------------------------------
# 中断重试与证据链
# ---------------------------------------------------------------------------


def test_interrupted_incremental_keeps_old_cache_and_retries(case: dict, monkeypatch) -> None:
    slot = _cache_slot(case)
    original_bytes = slot.read_bytes()
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])

    def crash(**_kwargs):
        raise RuntimeError("simulated interruption")

    monkeypatch.setitem(incremental.TEST_HOOKS, "before_cache_commit", crash)
    with pytest.raises(RuntimeError):
        execute_run(events_path, case["spec"], case["root"])
    monkeypatch.delitem(incremental.TEST_HOOKS, "before_cache_commit")

    # 旧缓存原样可用，且留有暂存文件。
    assert slot.read_bytes() == original_bytes
    assert list((case["root"] / "cache").glob("*.tmp"))

    # 重试：先清暂存，再基于旧缓存增量续建。
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "incremental"
    assert outcome.incremental["reused_user_count"] == 3
    assert not list((case["root"] / "cache").glob("*.tmp"))
    _assert_matches_full(case, events_path, outcome)

    # 新缓存自洽：输入摘要与 manifest 链可校验通过。
    state = json.loads(slot.read_text(encoding="utf-8"))
    assert incremental.verify_chain(
        case["root"], state, ledger_file=ledger_path(case["root"])
    )


def test_interrupted_first_run_falls_back_to_full(case: dict, monkeypatch) -> None:
    # 全量路径中断：缓存槽尚不存在，重跑安全走全量。
    slot = _cache_slot(case)
    slot.unlink()

    def crash(**_kwargs):
        raise RuntimeError("simulated interruption")

    monkeypatch.setitem(incremental.TEST_HOOKS, "before_cache_commit", crash)
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])
    with pytest.raises(RuntimeError):
        execute_run(events_path, case["spec"], case["root"])
    monkeypatch.delitem(incremental.TEST_HOOKS, "before_cache_commit")

    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "full"
    _assert_matches_full(case, events_path, outcome)


def test_corrupt_cache_state_falls_back_to_full(case: dict) -> None:
    slot = _cache_slot(case)
    slot.write_text("{broken-json", encoding="utf-8")
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "full"
    _assert_matches_full(case, events_path, outcome)
    # 全量后缓存被重建为合法状态。
    state = json.loads(slot.read_text(encoding="utf-8"))
    assert state["run_id"] == outcome.run_id


def test_tampered_group_mapping_falls_back_to_full(case: dict) -> None:
    slot = _cache_slot(case)
    state = json.loads(slot.read_text(encoding="utf-8"))
    # 删除一个仍然存在于新输入中的用户贡献：无法证明该用户可复用，
    # 禁止靠“文件大致相似”猜测，必须全量重算。
    del state["users"]["u-b"]
    slot.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "full"
    _assert_matches_full(case, events_path, outcome)


def test_unverifiable_group_change_is_not_guessed(case: dict) -> None:
    # 幽灵组（缓存里没有任何记录能证明其身份）同样拒绝增量猜测。
    slot = _cache_slot(case)
    state = json.loads(slot.read_text(encoding="utf-8"))
    state["groups"]["ghost-id"] = [
        {"identity": "deadbeef", "line_no": 999, "user_id": "u-ghost"}
    ]
    slot.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])
    outcome = execute_run(events_path, case["spec"], case["root"])
    # 幽灵组只影响不存在的用户，实际用户贡献仍可证明 → 可增量；
    # 但最终结果必须与全量逐字段一致。
    _assert_matches_full(case, events_path, outcome)


def test_broken_manifest_chain_falls_back_to_full(case: dict) -> None:
    slot = _cache_slot(case)
    state = json.loads(slot.read_text(encoding="utf-8"))
    manifest_path = case["root"] / state["run_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "partial"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "full"
    _assert_matches_full(case, events_path, outcome)


def test_ledger_run_id_mismatch_falls_back_to_full(case: dict) -> None:
    state = json.loads(_cache_slot(case).read_text(encoding="utf-8"))
    connection = sqlite3.connect(ledger_path(case["root"]))
    connection.execute("DELETE FROM runs WHERE run_id = ?", (state["run_id"],))
    connection.commit()
    connection.close()
    events_path = case["write"](_base_events() + [
        make_event("d1", "u-d", "view", "2026-03-02T09:00:00Z")
    ])
    outcome = execute_run(events_path, case["spec"], case["root"])
    assert outcome.compute_mode == "full"
    _assert_matches_full(case, events_path, outcome)


def test_manifest_carries_contribution_binding(case: dict) -> None:
    first = case["first"]
    manifest = json.loads(
        (first.artifact_dir / "manifest.json").read_text(encoding="utf-8")
    )
    binding = manifest["incremental"]
    assert binding["mode"] == "full"
    assert binding["spec_sha256"] == load_spec(case["spec"]).sha256
    assert binding["input_sha256"] == first.report["input"]["sha256"]
    assert len(binding["contribution_sha256"]) == 64
    state = json.loads(_cache_slot(case).read_text(encoding="utf-8"))
    assert state["manifest_sha256"] == config.sha256_bytes(
        (first.artifact_dir / "manifest.json").read_bytes()
    )
    assert state["contribution_sha256"] == binding["contribution_sha256"]


def test_cache_disabled_never_writes_slot(tmp_path: Path) -> None:
    spec_path = write_spec(tmp_path)
    events_path = write_events(tmp_path, _base_events())
    outcome = execute_run(
        events_path, spec_path, tmp_path / "runs", use_incremental_cache=False
    )
    assert outcome.compute_mode == "full"
    assert not (tmp_path / "runs" / "cache").exists()
    manifest = json.loads(
        (outcome.artifact_dir / "manifest.json").read_text(encoding="utf-8")
    )
    # 即使关闭缓存，manifest 仍记录贡献绑定，保证产物可审计。
    assert manifest["incremental"]["contribution_sha256"]


def test_quality_counts_remain_identical_under_incremental(case: dict) -> None:
    # 含重复行与质量问题的输入，验证全局质量计数逐字段一致。
    events = _base_events() + [
        make_event("a1", "u-a", "view", "2026-03-01T10:00:00Z",
                   "2026-03-05T10:00:00Z"),  # 同 id 重复
        make_event("dup-content", "u-c", "view", "2026-03-01T12:00:00Z"),
    ]
    events_path = case["write"](events, name="events-quality.jsonl")
    outcome = execute_run(events_path, case["spec"], case["root"])
    _assert_matches_full(case, events_path, outcome)
    full_report = _full_rerun(events_path, case["spec"], case["full_root"])
    assert outcome.report["quality"] == full_report["quality"]
    assert outcome.report["input"]["normalized_sha256"] == full_report["input"][
        "normalized_sha256"
    ]


def test_seeded_differential_fuzz_both_dedup_modes(tmp_path: Path) -> None:
    """固定随机种子下，随机变更序列的增量结果必须始终等于全量重跑。"""
    import random

    rng = random.Random(20261115)
    names = ["view", "signup", "buy"]
    users = [f"u-{i}" for i in range(6)]

    def rand_event(index: int) -> dict:
        return make_event(
            f"e-{index:04d}",
            rng.choice(users),
            rng.choice(names),
            f"2026-03-{rng.randint(1, 10):02d}T{rng.randint(0, 23):02d}:00:00Z",
        )

    for mode, override in (
        ("event_id", {}),
        ("content", {"events": {"dedup_key": "content"}}),
    ):
        workdir = tmp_path / mode
        spec_path = write_spec(workdir, **override)
        root, full_root = workdir / "runs", workdir / "runs-full"
        events = [rand_event(i) for i in range(40)]
        events += [dict(events[rng.randrange(len(events))]) for _ in range(4)]
        path = write_events(workdir, events, name="v0.jsonl")
        execute_run(path, spec_path, root)

        for version in range(1, 12):
            action = rng.choice(
                ["append", "delete", "modify", "migrate", "shuffle"]
            )
            next_events = [dict(item) for item in events]
            if action == "append":
                next_events += [rand_event(1000 + version)]
            elif action == "delete" and next_events:
                next_events.pop(rng.randrange(len(next_events)))
            elif action == "modify" and next_events:
                k = rng.randrange(len(next_events))
                next_events[k]["occurred_at"] = (
                    f"2026-03-{rng.randint(1, 10):02d}T08:30:00Z"
                )
            elif action == "migrate" and next_events:
                k = rng.randrange(len(next_events))
                next_events[k]["user_id"] = rng.choice(users)
            else:
                rng.shuffle(next_events)
            path = write_events(workdir, next_events, name=f"v{version}.jsonl")
            incremental_outcome = execute_run(path, spec_path, root)
            full_outcome = execute_run(
                path, spec_path, full_root, use_incremental_cache=False
            )
            # file_name 来自输入路径本身，与计算正确性无关，比较前归一。
            incremental_report = dict(incremental_outcome.report)
            full_report = dict(full_outcome.report)
            incremental_report["input"] = {
                **incremental_report["input"],
                "file_name": "events.jsonl",
            }
            full_report["input"] = {
                **full_report["input"],
                "file_name": "events.jsonl",
            }
            assert incremental_report == full_report, (mode, action, version)
            events = next_events
