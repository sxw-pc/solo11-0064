"""命令行入口：demo / run / compare / list / serve。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import DEFAULT_RUNS_DIR
from .errors import ErrorCode, FunnelCheckError
from .ledger import list_comparisons, list_runs
from .pipeline import execute_comparison, execute_run, ledger_path, runs_root
from .web import serve, validate_loopback_host

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _pct(value: object) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.2f}%"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m funnelcheck",
        description="漏斗验算台：本地、确定性的漏斗与留存计算核验工具。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="使用内置合成 fixture 生成运行与对比产物")
    demo.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, help="运行产物目录")

    run = sub.add_parser("run", help="对本地 JSONL 事件与分析规格执行一次运行")
    run.add_argument("--events", required=True, help="JSONL 事件文件")
    run.add_argument("--spec", required=True, help="YAML 分析规格")
    run.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, help="运行产物目录")

    compare = sub.add_parser("compare", help="对比两个已存在运行")
    compare.add_argument("--baseline", required=True, help="基线 run_id")
    compare.add_argument("--candidate", required=True, help="候选 run_id")
    compare.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, help="运行产物目录")

    listing = sub.add_parser("list", help="列出本地台账中的运行与对比")
    listing.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, help="运行产物目录")

    server = sub.add_parser("serve", help="启动仅回环地址可访问的本地只读查看器")
    server.add_argument("--host", default="127.0.0.1", help="仅允许 127.0.0.1 / ::1 / localhost")
    server.add_argument("--port", type=int, default=8765, help="监听端口")
    server.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, help="运行产物目录")
    server.add_argument(
        "--log-level",
        default="warning",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
    )
    return parser


def _print_run(outcome: object) -> None:
    report = outcome.report  # type: ignore[attr-defined]
    funnel = report["funnel"]
    print(f"运行完成：{outcome.run_id}")  # type: ignore[attr-defined]
    print(f"产物目录：{outcome.artifact_dir}")  # type: ignore[attr-defined]
    print(
        "漏斗："
        f"{funnel['entered_users']} → {funnel['converted_users']}，"
        f"整体转化 {_pct(funnel['overall_conversion'])}；"
        f"留存队列 {report['retention']['cohort_size']}"
    )


def _demo(runs_dir: str) -> int:
    events = _FIXTURES / "events.jsonl"
    events_candidate = _FIXTURES / "events_candidate.jsonl"
    spec = _FIXTURES / "spec.yaml"
    spec_candidate = _FIXTURES / "spec_candidate.yaml"
    missing = [path for path in (events, events_candidate, spec, spec_candidate) if not path.is_file()]
    if missing:
        raise FunnelCheckError(
            ErrorCode.IO,
            "内置 fixture 不完整",
            hint=", ".join(str(path) for path in missing),
        )
    print("使用内置合成 fixture 生成示例……")
    baseline = execute_run(events, spec, runs_dir)
    _print_run(baseline)
    config_variant = execute_run(events, spec_candidate, runs_dir)
    print()
    print("规格变化候选：")
    _print_run(config_variant)
    input_variant = execute_run(events_candidate, spec, runs_dir)
    print()
    print("输入变化候选：")
    _print_run(input_variant)

    spec_compare = execute_comparison(baseline.run_id, config_variant.run_id, runs_dir)
    input_compare = execute_comparison(baseline.run_id, input_variant.run_id, runs_dir)
    print()
    print("对比结果：")
    print(
        f"  规格变化：{spec_compare.comparison_id} · "
        f"{spec_compare.comparison['verdict']} · {spec_compare.artifact_dir}"
    )
    print(
        f"  输入变化：{input_compare.comparison_id} · "
        f"{input_compare.comparison['verdict']} · {input_compare.artifact_dir}"
    )
    print()
    print(f"下一步：python -m funnelcheck serve --runs-dir {runs_dir}")
    return 0


def _list(runs_dir: str) -> int:
    root = runs_root(runs_dir)
    runs = list_runs(ledger_path(root))
    comparisons = list_comparisons(ledger_path(root))
    print(f"台账：{ledger_path(root)}")
    if not runs and not comparisons:
        print("暂无记录。先执行 python -m funnelcheck demo")
        return 0
    if runs:
        print("\n运行：")
        for item in runs:
            print(
                f"  {item['run_id']}  {item['title']}  "
                f"入口={item['entered_users']} 完成={item['converted_users']} "
                f"转化={_pct(item['overall_conversion'])} 队列={item['cohort_size']}  "
                f"{item['created_at_utc']}"
            )
    if comparisons:
        print("\n对比：")
        for item in comparisons:
            print(
                f"  {item['comparison_id']}  {item['baseline_run_id']} → "
                f"{item['candidate_run_id']}  {item['verdict']}  "
                f"变化指标={item['changed_metric_count']}"
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return _demo(args.runs_dir)
        if args.command == "run":
            outcome = execute_run(args.events, args.spec, args.runs_dir)
            _print_run(outcome)
            return 0
        if args.command == "compare":
            outcome = execute_comparison(args.baseline, args.candidate, args.runs_dir)
            print(f"对比完成：{outcome.comparison_id}")
            print(f"产物目录：{outcome.artifact_dir}")
            print(
                f"判定：{outcome.comparison['verdict']} "
                f"（变化指标 {outcome.comparison['changed_metric_count']} 项）"
            )
            print(outcome.comparison["explanation"])
            return 0
        if args.command == "list":
            return _list(args.runs_dir)
        if args.command == "serve":
            validate_loopback_host(args.host)
            print(f"本地只读查看器：http://{args.host}:{args.port}  (Ctrl+C 停止)")
            serve(args.runs_dir, args.host, args.port, log_level=args.log_level)
            return 0
    except FunnelCheckError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"提示：{exc.hint}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        return 130
    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
