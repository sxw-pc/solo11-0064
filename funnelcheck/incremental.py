"""可失效的增量计算缓存。

缓存只在能够*证明*兼容时复用用户级漏斗/留存贡献，任何无法证明的情形
（规格、去重规则、语义版本变化，缓存损坏，贡献链断裂，组身份不一致）
一律退回全量计算。设计上的正确性边界：

* 缓存索引绑定 ``semantics_version``、``engine_version``、完整 ``spec_sha256``
  与显式去重规则；任一不匹配即拒绝复用。
* 原始输入摘要（字节 sha256、字节数、行数）写入状态，重试时先校验。
* event_id 去重模式维护跨用户的全局 event_id → 组成员映射；重复 ID 在
  用户间移动或发生跨用户冲突时，相关用户全部进入受影响集合。
* content 去重模式用规范化完整事件键（含 user_id）判定受影响组。
* 组身份用 ``(规范化事件摘要, 原始行号)`` 多重集比较，行序变化也会保守失效，
  绝不靠“文件大致相似”猜测未变用户。
* 状态文件以临时文件 + 原子替换落盘，进程中断时旧缓存保持可用，
  新缓存不会出现半组贡献。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    ENGINE_VERSION,
    SEMANTICS_VERSION,
    canonical_json,
    sha256_bytes,
    sha256_json,
    sha256_text,
)
from .engine.dedup import group_key_for
from .engine.funnel import (
    FunnelContribution,
    aggregate_funnel,
    funnel_contributions,
    user_funnel_contribution,
)
from .engine.order import group_by_user
from .engine.retention import (
    RetentionContribution,
    aggregate_retention,
    retention_contributions,
    user_retention_contribution,
)
from .ledger import get_run
from .models import Event, NormalizedBatch, RawInput, SpecBundle
from .normalize import (
    _included_hash,
    _rules,
    apply_deduplication,
    apply_inclusion,
    apply_out_of_order,
    parse_raw_rows,
    sort_included,
)
from .spec import AnalysisSpecification

CACHE_FORMAT = "funnelcheck-incremental-cache"
CACHE_SCHEMA_VERSION = 1
CACHE_DIR_NAME = "cache"
SLOT_PREFIX = "slot-"
SLOT_SUFFIX = ".json"

# 测试钩子：键为回调名，值为可调用对象，抛出异常即模拟进程中断。
TEST_HOOKS: dict[str, Any] = {}


def _fire_hook(name: str, **payload: Any) -> None:
    hook = TEST_HOOKS.get(name)
    if callable(hook):
        hook(**payload)


# ---------------------------------------------------------------------------
# 缓存索引与身份
# ---------------------------------------------------------------------------


def dedup_binding(spec: AnalysisSpecification) -> dict[str, str]:
    """显式去重规则绑定，与 spec_sha256 一起构成复用前提。"""
    return {
        "dedup_key": spec.events.dedup_key,
        "dedup_keep": spec.events.dedup_keep,
        "same_timestamp_order": spec.events.same_timestamp_order,
    }


def slot_identity(spec_bundle: SpecBundle) -> dict[str, Any]:
    spec = spec_bundle.spec
    return {
        "format": CACHE_FORMAT,
        "schema_version": CACHE_SCHEMA_VERSION,
        "semantics_version": SEMANTICS_VERSION,
        "engine_version": ENGINE_VERSION,
        "spec_sha256": spec_bundle.sha256,
        "dedup": dedup_binding(spec),
    }


def slot_token(spec_bundle: SpecBundle) -> str:
    binding = slot_identity(spec_bundle)
    material = canonical_json(binding)
    return sha256_text(material)[:20]


def cache_slot_path(root: str | Path, spec_bundle: SpecBundle) -> Path:
    directory = Path(root) / CACHE_DIR_NAME
    return directory / f"{SLOT_PREFIX}{slot_token(spec_bundle)}{SLOT_SUFFIX}"


def row_identity(event: Event) -> str:
    """规范化完整事件摘要：同一事件的不同 JSON 文本写法得到相同身份。"""
    return sha256_json(event.canonical_payload())


def build_groups(
    candidates: list[Event] | tuple[Event, ...], spec: AnalysisSpecification
) -> dict[str, list[dict[str, int | str]]]:
    """全局去重组映射（event_id 模式天然跨用户），按行号稳定排序。

    组内身份与去重模式保持一致：event_id 模式用完整规范化事件摘要
    （同 ID 内容变化必须失效），content 模式用规范化完整事件键
    （仅改 event_id 不影响去重组）。
    """
    groups: dict[str, list[dict[str, int | str]]] = {}
    for event in candidates:
        key = group_key_for(event, spec.events.dedup_key)
        if spec.events.dedup_key == "content":
            identity = key
        else:
            identity = row_identity(event)
        groups.setdefault(key, []).append(
            {
                "identity": identity,
                "line_no": event.line_no,
                "user_id": event.user_id,
            }
        )
    for entries in groups.values():
        entries.sort(key=lambda item: int(item["line_no"]))
    return groups


def input_digest(raw: RawInput) -> dict[str, int | str]:
    return {
        "sha256": raw.sha256,
        "byte_count": raw.byte_count,
        "line_count": raw.line_count,
    }


# ---------------------------------------------------------------------------
# 状态读取与严格校验
# ---------------------------------------------------------------------------


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _valid_state_shape(state: Any) -> bool:
    if not isinstance(state, dict):
        return False
    if not _is_str(state.get("run_id")) or not _is_str(state.get("manifest_sha256")):
        return False
    if not isinstance(state.get("input"), dict) or not _is_str(
        state["input"].get("sha256")
    ):
        return False
    groups = state.get("groups")
    users = state.get("users")
    if not isinstance(groups, dict) or not isinstance(users, dict):
        return False
    for entries in groups.values():
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            if not _is_str(entry.get("identity")) or not isinstance(
                entry.get("line_no"), int
            ) or not _is_str(entry.get("user_id")):
                return False
    for contribution in users.values():
        if not isinstance(contribution, dict) or "funnel" not in contribution or (
            "retention" not in contribution
        ):
            return False
    return True


def load_reusable_state(
    path: str | Path, spec_bundle: SpecBundle
) -> dict[str, Any] | None:
    """读取缓存状态并严格校验索引绑定；任何不符都返回 None（触发全量）。"""
    source = Path(path)
    if not source.is_file():
        return None
    try:
        state = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not _valid_state_shape(state):
        return None
    expected = slot_identity(spec_bundle)
    for key, value in expected.items():
        if state.get(key) != value:
            return None
    if state.get("input") is None:
        return None
    return state


def verify_chain(
    root: str | Path,
    state: dict[str, Any],
    *,
    ledger_file: str | Path | None,
) -> bool:
    """校验贡献 manifest 与 ledger run_id 链；链断裂则拒绝增量复用。"""
    root_path = Path(root)
    run_id = state["run_id"]
    manifest_path = root_path / run_id / "manifest.json"
    try:
        raw_manifest = manifest_path.read_bytes()
    except OSError:
        return False
    if sha256_bytes(raw_manifest) != state.get("manifest_sha256"):
        return False
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        return False
    if manifest.get("status") != "complete":
        return False
    if manifest.get("input_sha256") != state["input"].get("sha256"):
        return False
    if manifest.get("spec_sha256") != state.get("spec_sha256"):
        return False
    incremental = manifest.get("incremental")
    if not isinstance(incremental, dict) or not _is_str(
        incremental.get("contribution_sha256")
    ):
        return False
    if incremental.get("contribution_sha256") != state.get("contribution_sha256"):
        return False
    if ledger_file is not None:
        row = get_run(ledger_file, run_id)
        if row is None:
            return False
        if row.get("input_sha256") != state["input"].get("sha256"):
            return False
        if row.get("spec_sha256") != state.get("spec_sha256"):
            return False
    return True


# ---------------------------------------------------------------------------
# 增量计划：证明哪些用户可复用、哪些必须重算
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncrementalPlan:
    affected_users: frozenset[str]
    new_groups: dict[str, list[dict[str, int | str]]]
    changed_group_count: int

    @property
    def is_noop(self) -> bool:
        return self.changed_group_count == 0


def plan_incremental(
    state: dict[str, Any],
    groups: dict[str, list[dict[str, int | str]]],
    spec: AnalysisSpecification,
) -> IncrementalPlan | None:
    """比较新旧去重组，返回受影响用户；无法证明兼容时返回 None。"""
    old_groups = state.get("groups")
    if not isinstance(old_groups, dict):
        return None
    old_keys = set(old_groups)
    new_keys = set(groups)
    changed: set[str] = set()
    for key in old_keys | new_keys:
        if old_groups.get(key) != groups.get(key):
            changed.add(key)

    affected: set[str] = set()
    for key in changed:
        for entry in old_groups.get(key, ()):
            affected.add(str(entry["user_id"]))
        for entry in groups.get(key, ()):
            affected.add(str(entry["user_id"]))

    # 未受影响用户必须在旧状态里有结构合法的贡献，否则无法证明可复用。
    cached_users = state.get("users", {})
    all_new_users = {
        str(entry["user_id"])
        for entries in groups.values()
        for entry in entries
    }
    for user_id in all_new_users - affected:
        contribution = cached_users.get(user_id)
        if not isinstance(contribution, dict):
            return None
        if not _valid_contribution_shape(contribution):
            return None

    return IncrementalPlan(
        affected_users=frozenset(affected),
        new_groups=groups,
        changed_group_count=len(changed),
    )


def _valid_contribution_shape(contribution: Any) -> bool:
    if not isinstance(contribution, dict):
        return False
    funnel = contribution.get("funnel")
    retention = contribution.get("retention")
    if funnel is not None and not (
        _is_str(funnel.get("user_id"))
        and isinstance(funnel.get("reached"), int)
        and isinstance(funnel.get("first_event_payload"), dict)
    ):
        return False
    if retention is not None and not (
        _is_str(retention.get("user_id"))
        and _is_str(retention.get("cohort_date"))
        and isinstance(retention.get("return_periods"), list)
        and isinstance(retention.get("segments"), dict)
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# 规范化（全量阶段函数编排，保证质量计数与全量路径同源）
# ---------------------------------------------------------------------------


def normalize_with_groups(
    raw: RawInput, spec: AnalysisSpecification
) -> tuple[Any, dict[str, list[dict[str, int | str]]]]:
    """运行与 normalize_batch 相同的阶段序列，同时返回去重组供差异比较。"""
    stage = parse_raw_rows(raw, spec)
    tracker = stage.tracker
    deduped = apply_deduplication(stage.candidates, spec, tracker)
    included, included_users = apply_inclusion(deduped.events, spec, tracker)
    apply_out_of_order(included, tracker)
    ordered = sort_included(included, spec)
    tracker.increment("included_events", len(ordered))
    tracker.increment("included_users", len(included_users))
    tracker.increment("excluded_events", len(raw.rows) - len(ordered))

    batch = NormalizedBatch(
        events=tuple(ordered),
        quality=tracker.finalize(_rules(spec)),
        normalized_sha256=_included_hash(ordered, spec.events.same_timestamp_order),
        raw_sha256=raw.sha256,
    )
    groups = build_groups(stage.candidates, spec)
    return batch, groups


# ---------------------------------------------------------------------------
# 贡献计算、序列化与汇总
# ---------------------------------------------------------------------------


def _funnel_to_json(contribution: FunnelContribution | None) -> dict[str, Any] | None:
    if contribution is None:
        return None
    return {
        "user_id": contribution.user_id,
        "reached": contribution.reached,
        "first_event_payload": contribution.first_event_payload,
    }


def _retention_to_json(
    contribution: RetentionContribution | None,
) -> dict[str, Any] | None:
    if contribution is None:
        return None
    return {
        "user_id": contribution.user_id,
        "cohort_date": contribution.cohort_date,
        "return_periods": list(contribution.return_periods),
        "segments": dict(contribution.segments),
    }


def _funnel_from_json(payload: dict[str, Any]) -> FunnelContribution:
    return FunnelContribution(
        user_id=payload["user_id"],
        reached=int(payload["reached"]),
        first_event_payload=dict(payload["first_event_payload"]),
    )


def _retention_from_json(payload: dict[str, Any]) -> RetentionContribution:
    return RetentionContribution(
        user_id=payload["user_id"],
        cohort_date=payload["cohort_date"],
        return_periods=tuple(int(item) for item in payload["return_periods"]),
        segments=dict(payload["segments"]),
    )


def contribution_set_sha256(
    funnel: list[FunnelContribution],
    retention: list[RetentionContribution],
) -> str:
    """对完整用户贡献集合取稳定摘要，写入 manifest 与缓存状态。"""
    material = {
        "funnel": [_funnel_to_json(item) for item in sorted(funnel, key=lambda c: c.user_id)],
        "retention": [
            _retention_to_json(item)
            for item in sorted(retention, key=lambda c: c.user_id)
        ],
    }
    return sha256_json(material)


def full_contributions(
    included_events: tuple[Event, ...], spec: AnalysisSpecification
) -> tuple[list[FunnelContribution], list[RetentionContribution]]:
    """全量正确路径：重算所有用户的漏斗/留存贡献。"""
    return (
        funnel_contributions(included_events, spec),
        retention_contributions(included_events, spec),
    )


def incremental_contributions(
    included_events: tuple[Event, ...],
    spec: AnalysisSpecification,
    state: dict[str, Any],
    plan: IncrementalPlan,
) -> tuple[list[FunnelContribution], list[RetentionContribution], dict[str, int]]:
    """增量正确路径：只重算受影响用户，其余用户复用缓存贡献。"""
    grouped = group_by_user(included_events)
    funnel_output: list[FunnelContribution] = []
    retention_output: list[RetentionContribution] = []
    cached_users = state["users"]
    reused = 0
    recomputed = 0

    for user_id in sorted(grouped):
        if user_id in plan.affected_users:
            recomputed += 1
            funnel_contribution = user_funnel_contribution(
                user_id, grouped[user_id], spec
            )
            retention_contribution = user_retention_contribution(
                user_id, grouped[user_id], spec
            )
        else:
            cached = cached_users.get(user_id)
            # plan_incremental 已证明结构合法，这里再防御一次，否则安全降级。
            if cached is None or not _valid_contribution_shape(cached):
                raise CacheProofError(f"缓存中缺少用户 {user_id} 的可验证贡献")
            reused += 1
            funnel_contribution = (
                _funnel_from_json(cached["funnel"]) if cached["funnel"] else None
            )
            retention_contribution = (
                _retention_from_json(cached["retention"])
                if cached["retention"]
                else None
            )
        if funnel_contribution is not None:
            funnel_output.append(funnel_contribution)
        if retention_contribution is not None:
            retention_output.append(retention_contribution)

    stats = {
        "reused_user_count": reused,
        "recomputed_user_count": recomputed,
        "affected_user_count": len(plan.affected_users),
    }
    return funnel_output, retention_output, stats


def aggregate(
    funnel: list[FunnelContribution],
    retention: list[RetentionContribution],
    spec: AnalysisSpecification,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return aggregate_funnel(funnel, spec), aggregate_retention(retention, spec)


# ---------------------------------------------------------------------------
# 状态落盘（原子）与新状态构建
# ---------------------------------------------------------------------------


class CacheProofError(RuntimeError):
    """增量复用前提无法证明；调用方必须退回全量计算。"""


def build_state(
    *,
    spec_bundle: SpecBundle,
    raw: RawInput,
    run_id: str,
    groups: dict[str, list[dict[str, int | str]]],
    funnel: list[FunnelContribution],
    retention: list[RetentionContribution],
    manifest_sha256: str,
) -> dict[str, Any]:
    users: dict[str, Any] = {}
    funnel_by_user = {item.user_id: item for item in funnel}
    retention_by_user = {item.user_id: item for item in retention}
    for user_id in sorted(set(funnel_by_user) | set(retention_by_user)):
        users[user_id] = {
            "funnel": _funnel_to_json(funnel_by_user.get(user_id)),
            "retention": _retention_to_json(retention_by_user.get(user_id)),
        }
    state = slot_identity(spec_bundle)
    state.update(
        {
            "input": input_digest(raw),
            "run_id": run_id,
            "manifest_sha256": manifest_sha256,
            "contribution_sha256": contribution_set_sha256(funnel, retention),
            "groups": groups,
            "users": users,
        }
    )
    return state


def write_state_atomic(path: str | Path, state: dict[str, Any]) -> None:
    """临时文件 + os.replace：中断只可能留下旧状态或临时文件，不会出现半组。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    payload = json.dumps(
        state, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    _fire_hook("before_cache_commit", path=target, state=state)
    os.replace(temporary, target)


def discard_staging_files(root: str | Path) -> None:
    """清理上次中断遗留的 *.tmp 暂存文件；正式缓存不受影响。"""
    cache_dir = Path(root) / CACHE_DIR_NAME
    if not cache_dir.is_dir():
        return
    for temporary in cache_dir.glob(f"{SLOT_PREFIX}*{SLOT_SUFFIX}.tmp"):
        try:
            temporary.unlink()
        except OSError:
            pass
