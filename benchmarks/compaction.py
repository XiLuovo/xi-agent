"""Run the deterministic long-session compaction experiment.

This module is deliberately a thin adapter around ``benchmarks/run.py``.  The
Runner remains the single owner of fixture execution and benchmark scoring;
this script only varies the character budget and aggregates its result files.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = Path(__file__).resolve().with_name("run.py")
DEFAULT_CASE = "long_session_compaction"
DEFAULT_BUDGETS = (1800, 4200)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".xi" / "benchmark-compaction"


class CompactionExperimentError(RuntimeError):
    """Raised when the experiment cannot produce a trustworthy report."""


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
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="比较 Xi 长会话在关闭和不同字符预算下的上下文压缩表现",
    )
    parser.add_argument(
        "--case",
        default=DEFAULT_CASE,
        help="要运行的长会话 Case，默认 long_session_compaction",
    )
    parser.add_argument(
        "--mode",
        choices=("scripted", "live"),
        default="scripted",
        help="默认 scripted；live 仅保留入口，不在本实验中调用",
    )
    parser.add_argument(
        "--budgets",
        nargs="+",
        type=_positive_int,
        default=list(DEFAULT_BUDGETS),
        help="要比较的正字符预算，默认 1800 4200",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="实验报告和隔离 Runner 产物根目录",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_windows_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    budgets = _normalize_budgets(args.budgets, parser)
    experiment_id = _new_run_id()
    experiment_dir = (
        args.output_root.expanduser().resolve()
        / _artifact_name(args.case)
        / experiment_id
    )
    experiment_dir.mkdir(parents=True, exist_ok=False)

    report = _run_experiment(
        case=args.case,
        mode=args.mode,
        budgets=budgets,
        experiment_id=experiment_id,
        experiment_dir=experiment_dir,
    )
    json_path = experiment_dir / "report.json"
    markdown_path = experiment_dir / "report.md"
    report["artifacts"] = {
        "run_directory": str(experiment_dir),
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
        "runs_directory": str(experiment_dir / "runs"),
    }
    _write_json(json_path, report)
    markdown_path.write_text(
        _render_markdown(report),
        encoding="utf-8",
        newline="\n",
    )

    status = "PASS" if report["passed"] else "FAIL"
    print(
        f"[{status}] compaction case={args.case} mode={args.mode} "
        f"conditions={len(report['conditions'])}"
    )
    for condition in report["conditions"]:
        print(
            f"  {condition['condition']}: "
            f"{'PASS' if condition['passed'] else 'FAIL'} "
            f"compactions={condition['context_compactions']} "
            f"requests={condition['model_request_count']}"
        )
    print(f"JSON 报告: {json_path}")
    print(f"Markdown 报告: {markdown_path}")
    return 0 if report["passed"] else 1


def _run_experiment(
    *,
    case: str,
    mode: str,
    budgets: list[int],
    experiment_id: str,
    experiment_dir: Path,
) -> dict[str, Any]:
    started_at = time.monotonic()
    runs_dir = experiment_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=False)
    conditions: list[dict[str, Any]] = []
    specifications: list[tuple[str, int | None]] = [("off", None)]
    specifications.extend((f"budget-{budget}", budget) for budget in budgets)
    for condition, budget in specifications:
        conditions.append(
            _run_condition(
                case=case,
                mode=mode,
                condition=condition,
                budget=budget,
                output_root=runs_dir / condition,
            )
        )

    off = next(item for item in conditions if item["condition"] == "off")
    budget_conditions = [item for item in conditions if item["budget_chars"] is not None]
    assertions = {
        "off_compactions_zero": off["context_compactions"] == 0,
        "budget_compaction_observed": any(
            item["context_compactions"] > 0 for item in budget_conditions
        ),
        "budget_reduction_observed": any(
            item["context_compactions"] > 0
            and item["compaction_before_characters"]
            > item["compaction_after_characters"]
            for item in budget_conditions
        ),
        "all_agent_completed": all(item["agent_completed"] for item in conditions),
        "all_verification_passed": all(
            item["verification_passed"] for item in conditions
        ),
        "all_protected_files_unchanged": all(
            item["protected_files_unchanged"] for item in conditions
        ),
        "all_allowed_paths_respected": all(
            item["allowed_changed_paths_respected"] for item in conditions
        ),
        "all_runner_invocations_zero": all(
            item["runner_exit_code"] == 0 for item in conditions
        ),
    }
    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "case": case,
        "mode": mode,
        "budgets": budgets,
        "conditions": conditions,
        "assertions": assertions,
        "passed": all(assertions.values()),
        "duration_seconds": round(time.monotonic() - started_at, 3),
    }


def _run_condition(
    *,
    case: str,
    mode: str,
    condition: str,
    budget: int | None,
    output_root: Path,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        str(RUNNER_PATH),
        "--case",
        case,
        "--mode",
        mode,
        "--output-root",
        str(output_root),
        "--json",
    ]
    if budget is not None:
        command.extend(["--context-budget-chars", str(budget)])

    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        exit_code: int | None = completed.returncode
        launch_error: str | None = None
    except (OSError, subprocess.TimeoutExpired) as exc:
        exit_code = 124 if isinstance(exc, subprocess.TimeoutExpired) else None
        launch_error = f"runner 调用失败: {type(exc).__name__}"
    duration_seconds = round(time.monotonic() - started_at, 3)

    result_path = _locate_case_result(output_root)
    result: dict[str, Any] | None = None
    if result_path is not None:
        try:
            result = _read_json_object(result_path)
        except CompactionExperimentError as exc:
            launch_error = str(exc)

    trace = _mapping(result.get("trace") if result else None)
    criteria = _mapping(result.get("criteria") if result else None)
    workspace_changes = _mapping(result.get("workspace_changes") if result else None)
    tool_calls = _mapping(trace.get("tool_calls"))
    before = _count(
        trace.get(
            "compaction_before_characters",
            result.get("compaction_before_characters", 0) if result else 0,
        )
    )
    after = _count(
        trace.get(
            "compaction_after_characters",
            result.get("compaction_after_characters", 0) if result else 0,
        )
    )
    reduction = round((before - after) / before * 100, 2) if before > 0 else 0.0
    failure_reasons = list(result.get("failure_reasons", [])) if result else []
    if launch_error:
        failure_reasons.append(launch_error)
    runner_completed = exit_code == 0 and result is not None
    case_passed = bool(result.get("passed")) if result else False
    agent_completed = bool(criteria.get("agent_completed"))
    verification_passed = bool(criteria.get("verification_passed"))
    protected_unchanged = bool(criteria.get("protected_files_unchanged"))
    allowed_paths_respected = bool(criteria.get("allowed_changed_paths_respected"))
    condition_passed = (
        runner_completed
        and case_passed
        and agent_completed
        and verification_passed
        and protected_unchanged
        and allowed_paths_respected
    )
    changed_paths = workspace_changes.get("changed_paths") or trace.get(
        "files_changed", []
    )
    return {
        "condition": condition,
        "budget_chars": budget,
        "passed": condition_passed,
        "runner_completed": runner_completed,
        "runner_exit_code": exit_code,
        "case_passed": case_passed,
        "agent_completed": agent_completed,
        "verification_passed": verification_passed,
        "protected_files_unchanged": protected_unchanged,
        "allowed_changed_paths_respected": allowed_paths_respected,
        "context_compactions": _count(trace.get("context_compactions", 0)),
        "compaction_before_characters": before,
        "compaction_after_characters": after,
        "max_model_request_characters": _count(
            trace.get("max_model_request_characters", 0)
        ),
        "total_model_request_characters": _count(
            trace.get("total_model_request_characters", 0)
        ),
        "model_request_count": _count(trace.get("model_request_count", 0)),
        "steps": trace.get("steps"),
        "tool_calls": dict(tool_calls),
        "tool_call_total": sum(_count(value) for value in tool_calls.values()),
        "duration_seconds": _number(
            result.get("duration_seconds", duration_seconds) if result else duration_seconds
        ),
        "reduction_percentage": reduction,
        "files_changed": list(changed_paths) if isinstance(changed_paths, list) else [],
        "artifact_path": result.get("artifacts", {}).get("result") if result else None,
        "run_directory": result.get("artifacts", {}).get("run_directory") if result else None,
        "runner_result_path": str(result_path) if result_path else None,
        "failure_reasons": failure_reasons,
    }


def _locate_case_result(output_root: Path) -> Path | None:
    candidates = sorted(
        path for path in output_root.glob("*/*/result.json") if path.is_file()
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        raise CompactionExperimentError(
            f"Runner output-root 中存在多个结果，无法确定实验条件产物: {output_root}"
        )
    return candidates[0]


def _normalize_budgets(
    values: Sequence[int],
    parser: argparse.ArgumentParser,
) -> list[int]:
    budgets = [int(value) for value in values]
    if len(budgets) < 2:
        parser.error("--budgets 至少需要两个正整数条件")
    if len(set(budgets)) != len(budgets):
        parser.error("--budgets 不能包含重复值")
    return budgets


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompactionExperimentError(f"无法读取 Runner 结果 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CompactionExperimentError(f"Runner 结果必须是 JSON 对象: {path}")
    return value


def _render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Xi 上下文压缩实验：{report['case']}",
        "",
        f"- 模式：`{report['mode']}`（字符预算，不是 token 计数）",
        f"- 实验 ID：`{report['experiment_id']}`",
        f"- 总耗时：{_format_number(report['duration_seconds'])} 秒",
        f"- 总体判定：{'通过' if report['passed'] else '未通过'}",
        "",
        "## 条件结果",
        "",
        "| 条件 | 预算字符 | 通过 | 压缩次数 | 压缩前→后 | 减少比例 | 模型请求字符 max/total | 步骤 | 工具调用 | 耗时(s) | 改动文件 |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report["conditions"]:
        budget = "关闭" if item["budget_chars"] is None else str(item["budget_chars"])
        before = _format_number(item["compaction_before_characters"])
        after = _format_number(item["compaction_after_characters"])
        files = ", ".join(item["files_changed"]) or "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{item['condition']}`",
                    budget,
                    "是" if item["passed"] else "否",
                    str(item["context_compactions"]),
                    f"{before} → {after}",
                    f"{item['reduction_percentage']:.2f}%",
                    f"{_format_number(item['max_model_request_characters'])} / "
                    f"{_format_number(item['total_model_request_characters'])}",
                    str(item["steps"] if item["steps"] is not None else "-"),
                    str(item["tool_call_total"]),
                    _format_number(item["duration_seconds"]),
                    files,
                ]
            )
            + " |"
        )
    lines.extend(["", "## 实验断言", ""])
    for name, value in report["assertions"].items():
        lines.append(f"- `{name}`：{'通过' if value else '失败'}")
    lines.extend(
        [
            "",
            "> 每个条件使用独立工作区并调用现有 Benchmark Runner；Runner 非零退出码不会被视为成功。",
            "> 原始 Trace 和 Case 产物保存在对应条件的 `runs/` 目录。",
            "",
        ]
    )
    return "\n".join(lines)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number >= 0 else 0.0


def _count(value: Any) -> int:
    return int(_number(value))


def _format_number(value: Any) -> str:
    number = _number(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _artifact_name(value: str) -> str:
    name = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in value
    ).strip("._")
    return name or "experiment"


def _new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
