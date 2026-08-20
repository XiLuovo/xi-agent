"""Compare Xi context strategies by orchestrating the existing suite runner."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = Path(__file__).resolve().with_name("run.py")
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".xi" / "benchmark-comparisons"
DEFAULT_STRATEGIES = ("search", "repo-map")
SUPPORTED_STRATEGIES = frozenset(DEFAULT_STRATEGIES)


class CompareError(RuntimeError):
    """Raised when comparison inputs or runner artifacts are invalid."""


def _configure_windows_utf8_streams() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            continue


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="多轮比较 Xi 上下文策略，并生成 JSON/Markdown 报告",
    )
    parser.add_argument("--suite", required=True, help="benchmarks/suites 下的 suite 名")
    parser.add_argument(
        "--mode",
        choices=("scripted", "live"),
        default="scripted",
        help="scripted 完全离线；live 调用配置的真实模型",
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=list(DEFAULT_STRATEGIES),
        help="要比较的上下文策略，默认 search repo-map",
    )
    parser.add_argument(
        "--repeat",
        type=_positive_int,
        default=1,
        help="每种策略运行 Suite 的次数，默认 1",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Compare 报告和隔离 Runner 产物的根目录",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_windows_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        strategies = _normalize_strategies(args.strategies)
    except CompareError as exc:
        parser.error(str(exc))
    comparison_id = _new_run_id()
    comparison_dir = (
        args.output_root.expanduser().resolve()
        / _artifact_name(args.suite)
        / comparison_id
    )
    comparison_dir.mkdir(parents=True, exist_ok=False)

    report = _run_comparison(
        suite=args.suite,
        mode=args.mode,
        strategies=strategies,
        repeat=args.repeat,
        comparison_id=comparison_id,
        comparison_dir=comparison_dir,
    )
    json_path = comparison_dir / "report.json"
    markdown_path = comparison_dir / "report.md"
    report["artifacts"] = {
        "run_directory": str(comparison_dir),
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
        "runner_runs_directory": str(comparison_dir / "runs"),
    }
    _write_json(json_path, report)
    markdown_path.write_text(_render_markdown(report), encoding="utf-8", newline="\n")

    status = "COMPLETE" if report["all_invocations_completed"] else "INCOMPLETE"
    print(
        f"[{status}] compare suite={args.suite} mode={args.mode} "
        f"invocations={report['completed_invocations']}/{report['total_invocations']}"
    )
    print(f"JSON 报告: {json_path}")
    print(f"Markdown 报告: {markdown_path}")
    return 0 if report["all_invocations_completed"] else 1


def _run_comparison(
    *,
    suite: str,
    mode: str,
    strategies: list[str],
    repeat: int,
    comparison_id: str,
    comparison_dir: Path,
) -> dict[str, Any]:
    started_at = time.monotonic()
    invocations: list[dict[str, Any]] = []
    runs_root = comparison_dir / "runs"
    runs_root.mkdir(parents=True, exist_ok=False)

    invocation_index = 0
    for round_number in range(1, repeat + 1):
        round_order = _rotated_order(strategies, round_number - 1)
        for position, strategy in enumerate(round_order, start=1):
            invocation_index += 1
            run_output_root = (
                runs_root
                / f"round-{round_number:03d}"
                / f"{position:02d}-{_artifact_name(strategy)}"
            )
            invocations.append(
                _invoke_suite_runner(
                    invocation_index=invocation_index,
                    round_number=round_number,
                    order_position=position,
                    suite=suite,
                    mode=mode,
                    strategy=strategy,
                    run_output_root=run_output_root,
                )
            )

    completed_invocations = sum(1 for item in invocations if item["completed"])
    total_invocations = len(invocations)
    all_invocations_completed = completed_invocations == total_invocations
    return {
        "schema_version": 1,
        "comparison_id": comparison_id,
        "suite": suite,
        "mode": mode,
        "strategies": strategies,
        "repeat": repeat,
        "total_invocations": total_invocations,
        "completed_invocations": completed_invocations,
        "failed_invocations": total_invocations - completed_invocations,
        "all_invocations_completed": all_invocations_completed,
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "execution_order": [
            {
                "invocation": item["invocation"],
                "round": item["round"],
                "position": item["position"],
                "strategy": item["strategy"],
            }
            for item in invocations
        ],
        "strategies_summary": {
            strategy: _aggregate_strategy(strategy, invocations)
            for strategy in strategies
        },
        "invocations": invocations,
    }


def _invoke_suite_runner(
    *,
    invocation_index: int,
    round_number: int,
    order_position: int,
    suite: str,
    mode: str,
    strategy: str,
    run_output_root: Path,
) -> dict[str, Any]:
    run_output_root.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(RUNNER_PATH),
        "--suite",
        suite,
        "--mode",
        mode,
        "--context-strategy",
        strategy,
        "--output-root",
        str(run_output_root),
        "--json",
    ]
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        exit_code: int | None = completed.returncode
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        invocation_error: str | None = None
    except OSError as exc:
        exit_code = None
        stdout = ""
        stderr = f"无法启动 benchmark runner: {type(exc).__name__}: {exc}"
        invocation_error = stderr
    duration_seconds = round(time.monotonic() - started_at, 3)

    try:
        result_path = _locate_suite_result(run_output_root)
    except CompareError as exc:
        result_path = None
        invocation_error = str(exc)
    suite_result: dict[str, Any] | None = None
    if result_path is not None:
        try:
            suite_result = _read_json_object(result_path)
        except CompareError as exc:
            invocation_error = str(exc)
        else:
            artifact_strategy = suite_result.get("context_strategy")
            if artifact_strategy != strategy:
                invocation_error = (
                    "Runner 结果的 context_strategy 不匹配: "
                    f"expected={strategy}, actual={artifact_strategy}"
                )
                suite_result = None
    elif invocation_error is None:
        invocation_error = "runner 未生成 suite result.json"

    completed_call = suite_result is not None
    suite_passed = bool(suite_result.get("passed")) if suite_result else False
    return {
        "invocation": invocation_index,
        "round": round_number,
        "position": order_position,
        "strategy": strategy,
        "completed": completed_call,
        "exit_code": exit_code,
        "suite_passed": suite_passed,
        "duration_seconds": duration_seconds,
        "stdout": stdout,
        "stderr": stderr,
        "error": invocation_error,
        "runner_output_root": str(run_output_root),
        "suite_result_path": str(result_path) if result_path is not None else None,
        "suite_result": suite_result,
    }


def _locate_suite_result(output_root: Path) -> Path | None:
    suites_root = output_root / "suites"
    if not suites_root.is_dir():
        return None
    candidates = sorted(
        (path for path in suites_root.glob("*/*/result.json") if path.is_file()),
        key=lambda path: path.as_posix(),
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        raise CompareError(
            f"Runner output-root 中存在多个 Suite 结果，无法确定本次产物: {output_root}"
        )
    return candidates[0]


def _aggregate_strategy(
    strategy: str,
    invocations: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = [item for item in invocations if item["strategy"] == strategy]
    completed = [
        item for item in selected
        if item["completed"] and isinstance(item.get("suite_result"), Mapping)
    ]
    suite_results = [item["suite_result"] for item in completed]
    suite_passes = sum(1 for result in suite_results if result.get("passed"))
    total_cases = sum(_number(result.get("total_cases")) for result in suite_results)
    passed_cases = sum(_number(result.get("passed_cases")) for result in suite_results)

    steps_per_suite = [
        sum(_case_metric(result, "steps"))
        for result in suite_results
    ]
    tools_per_suite = [
        sum(_numeric_values(result.get("tool_calls")))
        for result in suite_results
    ]
    prompt_tokens = [_usage_metric(result, "prompt_tokens") for result in suite_results]
    completion_tokens = [_usage_metric(result, "completion_tokens") for result in suite_results]
    total_tokens = [_usage_metric(result, "total_tokens") for result in suite_results]
    context_characters = [
        _number(result.get("context_characters"))
        or sum(_context_characters(result))
        for result in suite_results
    ]
    completion_rejections = [
        _number(result.get("completion_rejections"))
        for result in suite_results
    ]
    suite_duration = [_number(result.get("total_duration_seconds")) for result in suite_results]
    invocation_duration = [_number(item.get("duration_seconds")) for item in completed]

    tool_calls: Counter[str] = Counter()
    for result in suite_results:
        tool_calls.update(
            {
                str(name): _number(count)
                for name, count in _mapping(result.get("tool_calls")).items()
            }
        )

    return {
        "requested_invocations": len(selected),
        "completed_invocations": len(completed),
        "failed_invocations": len(selected) - len(completed),
        "case_success_rate": _rate(passed_cases, total_cases),
        "suite_pass_rate": _rate(suite_passes, len(suite_results)),
        "passed_cases": int(passed_cases),
        "total_cases": int(total_cases),
        "passed_suites": suite_passes,
        "total_suites": len(suite_results),
        "metrics_per_suite": {
            "steps": _describe(steps_per_suite),
            "tool_calls": _describe(tools_per_suite),
            "prompt_tokens": _describe(prompt_tokens),
            "completion_tokens": _describe(completion_tokens),
            "total_tokens": _describe(total_tokens),
            "context_characters": _describe(context_characters),
            "completion_rejections": _describe(completion_rejections),
            "suite_duration_seconds": _describe(suite_duration),
            "invocation_duration_seconds": _describe(invocation_duration),
        },
        "tool_calls_total": dict(sorted(tool_calls.items())),
    }


def _case_metric(result: Mapping[str, Any], key: str) -> list[float]:
    cases = result.get("cases")
    if not isinstance(cases, list):
        return []
    return [
        _number(case.get(key))
        for case in cases
        if isinstance(case, Mapping)
    ]


def _context_characters(result: Mapping[str, Any]) -> list[float]:
    cases = result.get("cases")
    if not isinstance(cases, list):
        return []
    values: list[float] = []
    for case in cases:
        if not isinstance(case, Mapping):
            continue
        context = case.get("context")
        if isinstance(context, Mapping):
            values.append(_number(context.get("characters")))
    return values


def _usage_metric(result: Mapping[str, Any], key: str) -> float:
    usage = result.get("usage")
    if isinstance(usage, Mapping):
        direct = usage.get(key)
        if _is_number(direct):
            return float(direct)
    cases = result.get("cases")
    if not isinstance(cases, list):
        return 0.0
    return sum(
        _number(case_usage.get(key))
        for case in cases
        if isinstance(case, Mapping)
        and isinstance((case_usage := case.get("usage")), Mapping)
    )


def _describe(values: Iterable[float]) -> dict[str, float | int | None]:
    normalized = [float(value) for value in values if _is_number(value)]
    if not normalized:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "pstdev": None,
        }
    return {
        "count": len(normalized),
        "mean": round(statistics.fmean(normalized), 4),
        "median": round(statistics.median(normalized), 4),
        "min": round(min(normalized), 4),
        "max": round(max(normalized), 4),
        "pstdev": round(statistics.pstdev(normalized), 4),
    }


def _rate(numerator: float, denominator: int | float) -> dict[str, float | int | None]:
    if denominator <= 0:
        return {
            "passed": int(numerator),
            "total": int(denominator),
            "rate": None,
            "percent": None,
        }
    rate = numerator / denominator
    return {
        "passed": int(numerator),
        "total": int(denominator),
        "rate": round(rate, 6),
        "percent": round(rate * 100, 2),
    }


def _render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Xi Benchmark Compare：{report['suite']}",
        "",
        f"- 模式：`{report['mode']}`",
        f"- 每种策略轮数：{report['repeat']}",
        f"- 调用完成：{report['completed_invocations']}/{report['total_invocations']}",
        f"- 总耗时：{_format_number(report['duration_seconds'])} 秒",
        "",
        "## 策略汇总",
        "",
        "| 策略 | Case 成功率 | Suite 完整通过率 | 步骤 mean/median/min/max | 工具调用 mean/median/min/max | Prompt tokens mean | Completion tokens mean | Total tokens mean | 上下文字符 mean | 完成拒绝 mean | Suite 耗时 mean(s) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    summaries = _mapping(report.get("strategies_summary"))
    for strategy in report["strategies"]:
        summary = _mapping(summaries.get(strategy))
        metrics = _mapping(summary.get("metrics_per_suite"))
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{strategy}`",
                    _format_rate(summary.get("case_success_rate")),
                    _format_rate(summary.get("suite_pass_rate")),
                    _format_four(metrics.get("steps")),
                    _format_four(metrics.get("tool_calls")),
                    _format_stat(metrics.get("prompt_tokens"), "mean"),
                    _format_stat(metrics.get("completion_tokens"), "mean"),
                    _format_stat(metrics.get("total_tokens"), "mean"),
                    _format_stat(metrics.get("context_characters"), "mean"),
                    _format_stat(metrics.get("completion_rejections"), "mean"),
                    _format_stat(metrics.get("suite_duration_seconds"), "mean"),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 调用明细",
            "",
            "| # | 轮次 | 顺位 | 策略 | 调用完成 | Exit code | Suite 通过 | Suite 结果 | 错误 |",
            "| ---: | ---: | ---: | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for item in report["invocations"]:
        result_path = item.get("suite_result_path")
        result_link = f"`{result_path}`" if result_path else "-"
        error = _markdown_cell(item.get("error") or item.get("stderr") or "-")
        lines.append(
            f"| {item['invocation']} | {item['round']} | {item['position']} | "
            f"`{item['strategy']}` | {_yes_no(item['completed'])} | "
            f"{item['exit_code'] if item['exit_code'] is not None else '-'} | "
            f"{_yes_no(item['suite_passed'])} | {result_link} | {error} |"
        )
    lines.append("")
    lines.append(
        "> Compare 自身的退出码只表示是否成功获得全部 Runner 的 Suite 结果；"
        "明细中的 Runner exit code 仍会保留。Case 或 Suite 未通过不会阻止报告生成。"
    )
    lines.append("")
    return "\n".join(lines)


def _normalize_strategies(values: Sequence[str]) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if not normalized or any(not value for value in normalized):
        raise CompareError("--strategies 至少需要一个非空策略")
    if len(set(normalized)) != len(normalized):
        raise CompareError("--strategies 不能包含重复策略")
    unsupported = [value for value in normalized if value not in SUPPORTED_STRATEGIES]
    if unsupported:
        raise CompareError(
            "不支持的上下文策略: " + ", ".join(unsupported)
            + "；可选值为 search、repo-map"
        )
    return normalized


def _rotated_order(values: list[str], offset: int) -> list[str]:
    if not values:
        return []
    index = offset % len(values)
    return [*values[index:], *values[:index]]


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompareError(f"无法读取 Runner 结果 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CompareError(f"Runner 结果必须是 JSON 对象: {path}")
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _numeric_values(value: Any) -> list[float]:
    if not isinstance(value, Mapping):
        return []
    return [_number(item) for item in value.values()]


def _number(value: Any) -> float:
    return float(value) if _is_number(value) else 0.0


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _format_rate(value: Any) -> str:
    rate = _mapping(value)
    percent = rate.get("percent")
    if percent is None:
        return "-"
    return f"{percent:.2f}% ({rate.get('passed', 0)}/{rate.get('total', 0)})"


def _format_four(value: Any) -> str:
    stats = _mapping(value)
    if stats.get("count", 0) == 0:
        return "-"
    return "/".join(
        _format_number(stats.get(key))
        for key in ("mean", "median", "min", "max")
    )


def _format_stat(value: Any, key: str) -> str:
    stats = _mapping(value)
    return _format_number(stats.get(key)) if stats.get("count", 0) else "-"


def _format_number(value: Any) -> str:
    if not _is_number(value):
        return "-"
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.4f}".rstrip("0").rstrip(".")


def _yes_no(value: Any) -> str:
    return "是" if value else "否"


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _artifact_name(value: str) -> str:
    name = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in value
    ).strip("._")
    return name or "comparison"


def _new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    raise SystemExit(main())
