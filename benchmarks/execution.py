"""Local-vs-Docker execution-backend A/B experiment orchestration.

This module deliberately delegates Case/Suite execution and scoring to
``benchmarks/run.py``.  It owns only condition isolation, result validation,
metric projection, and report rendering.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = Path(__file__).resolve().parent / "run.py"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".xi" / "benchmark-execution"
DEFAULT_DOCKER_IMAGE = "python:3.11-slim"


class ExecutionExperimentError(RuntimeError):
    """Raised when the A/B orchestration or artifact contract is invalid."""


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="比较 Xi local 与 Docker Executor 的可重复执行隔离 A/B"
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--suite", default="core", help="要比较的 Suite 名称")
    target.add_argument("--case", help="只比较一个 Benchmark Case")
    parser.add_argument(
        "--mode",
        choices=("scripted", "live"),
        default="scripted",
        help="默认 scripted；live 会调用真实模型",
    )
    parser.add_argument(
        "--docker-image",
        default=DEFAULT_DOCKER_IMAGE,
        help=f"Docker 条件使用的 Linux 镜像；默认 {DEFAULT_DOCKER_IMAGE}",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="实验报告根目录",
    )
    parser.add_argument("--json", action="store_true", help="额外输出一行 JSON 报告")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_windows_utf8_streams()
    args = build_parser().parse_args(argv)
    target_kind = "case" if args.case else "suite"
    target_name = args.case or args.suite
    experiment_id = _new_id()
    experiment_dir = (
        args.output_root.expanduser().resolve()
        / _artifact_name(target_name)
        / experiment_id
    )
    experiment_dir.mkdir(parents=True, exist_ok=False)
    report = _run_experiment(
        target_kind=target_kind,
        target_name=target_name,
        mode=args.mode,
        docker_image=args.docker_image,
        experiment_id=experiment_id,
        experiment_dir=experiment_dir,
    )
    json_path = experiment_dir / "report.json"
    markdown_path = experiment_dir / "report.md"
    report["artifacts"] = {
        "run_directory": str(experiment_dir),
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
    }
    _write_json(json_path, report)
    markdown_path.write_text(_render_markdown(report), encoding="utf-8", newline="\n")
    _print_summary(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


def _run_experiment(
    *,
    target_kind: str,
    target_name: str,
    mode: str,
    docker_image: str | None,
    experiment_id: str,
    experiment_dir: Path,
) -> dict[str, Any]:
    started_at = time.monotonic()
    conditions = [
        _run_condition(
            executor="local",
            target_kind=target_kind,
            target_name=target_name,
            mode=mode,
            docker_image=None,
            output_root=experiment_dir / "local",
        ),
        _run_condition(
            executor="docker",
            target_kind=target_kind,
            target_name=target_name,
            mode=mode,
            docker_image=docker_image,
            output_root=experiment_dir / "docker",
        ),
    ]
    local = conditions[0]
    docker = conditions[1]
    assertions = {
        "both_runner_results_complete": all(
            item["runner_completed"] for item in conditions
        ),
        "local_executor_recorded": local["executor"] == "local",
        "docker_executor_recorded": docker["executor"] == "docker",
        "both_trace_executors_match": all(
            item["trace_executor_matches"] for item in conditions
        ),
        "both_agent_completed": all(item["agent_completed"] for item in conditions),
        "both_verifications_passed": all(
            item["verification_passed"] for item in conditions
        ),
        "both_conditions_passed": all(item["passed"] for item in conditions),
        "docker_no_fallback": docker["fallback_count"] == 0,
        "baseline_and_verification_are_host_explicit": all(
            item["execution_locations"].get("baseline") == "host"
            and item["execution_locations"].get("verification") == "host"
            for item in conditions
        ),
    }
    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "target_kind": target_kind,
        "target": target_name,
        "mode": mode,
        "docker_image_requested": docker_image,
        "conditions": conditions,
        "local_to_docker": _compare_conditions(local, docker),
        "assertions": assertions,
        "passed": all(assertions.values()),
        "duration_seconds": round(time.monotonic() - started_at, 3),
    }


def _run_condition(
    *,
    executor: str,
    target_kind: str,
    target_name: str,
    mode: str,
    docker_image: str | None,
    output_root: Path,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        str(RUNNER_PATH),
        f"--{target_kind}",
        target_name,
        "--mode",
        mode,
        "--executor",
        executor,
        "--output-root",
        str(output_root),
        "--json",
    ]
    if executor == "docker" and docker_image:
        command.extend(["--docker-image", docker_image])
    started_at = time.monotonic()
    launch_error: str | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            check=False,
        )
        exit_code: int | None = completed.returncode
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = _clip(_timeout_text(exc.stdout), 4000)
        stderr = _clip(_timeout_text(exc.stderr), 4000)
        launch_error = "benchmark runner 超时"
    except OSError as exc:
        exit_code = None
        stdout = ""
        stderr = f"无法启动 benchmark runner: {type(exc).__name__}: {exc}"
        launch_error = stderr
    duration_seconds = round(time.monotonic() - started_at, 3)

    result_path: Path | None = None
    result: dict[str, Any] | None = None
    artifact_error: str | None = None
    try:
        result_path = _locate_result(output_root, target_kind)
        if result_path is None:
            artifact_error = "Runner 未生成唯一 result.json"
        else:
            result = _read_json_object(result_path)
    except ExecutionExperimentError as exc:
        artifact_error = str(exc)

    reasons: list[str] = []
    if launch_error:
        reasons.append(launch_error)
    if artifact_error:
        reasons.append(artifact_error)
    if exit_code not in (0, None):
        reasons.append(f"Runner exit_code={exit_code}")

    expected_executor = executor
    recorded_executor = str(result.get("executor", "local")) if result else None
    if result is not None and recorded_executor != expected_executor:
        reasons.append(
            f"结果 executor 不匹配: expected={expected_executor}, actual={recorded_executor}"
        )
    trace_executor_matches = False
    if result is not None:
        trace_executors = _result_trace_executors(result, target_kind)
        expected_trace_count = _result_case_count(result, target_kind)
        trace_executor_matches = (
            len(trace_executors) == expected_trace_count
            and all(value == expected_executor for value in trace_executors)
        )
        if not trace_executor_matches:
            actual = ", ".join(trace_executors) if trace_executors else "<missing>"
            reasons.append(
                "Trace executor 不匹配或缺失: "
                f"expected={expected_executor}, actual={actual}"
            )

    metrics = _project_result(result, target_kind)
    reasons.extend(metrics.pop("failure_reasons"))
    runner_completed = exit_code == 0 and result is not None and not artifact_error
    passed = (
        runner_completed
        and metrics["passed"]
        and metrics["agent_completed"]
        and metrics["verification_passed"]
        and recorded_executor == expected_executor
        and trace_executor_matches
    )
    execution_locations = metrics.pop("execution_locations")
    return {
        "condition": executor,
        "executor": executor,
        "docker_image": metrics.pop("docker_image"),
        "mode": mode,
        "passed": passed,
        "runner_completed": runner_completed,
        "runner_exit_code": exit_code,
        "result_readable": result is not None and artifact_error is None,
        "trace_executor_matches": trace_executor_matches,
        "result_path": str(result_path) if result_path else None,
        "runner_output_root": str(output_root),
        "runner_command": command,
        "stdout": _clip(stdout, 4000),
        "stderr": _clip(stderr, 4000),
        "failure_reasons": reasons,
        "execution_locations": execution_locations,
        **metrics,
    }


def _project_result(result: Mapping[str, Any] | None, target_kind: str) -> dict[str, Any]:
    if result is None:
        return {
            "passed": False,
            "success_rate": {"passed": 0, "total": 0, "rate": 0.0, "percent": 0.0},
            "agent_completed": False,
            "verification_passed": False,
            "steps": 0,
            "tool_calls": {},
            "tool_call_total": 0,
            "agent_duration_seconds": 0.0,
            "total_duration_seconds": 0.0,
            "docker_command_count": 0,
            "docker_failure_count": 0,
            "fallback_count": 0,
            "files_changed": [],
            "docker_image": None,
            "execution_locations": {
                "agent_commands": None,
                "baseline": "host",
                "verification": "host",
            },
            "failure_reasons": ["缺少 Runner result.json"],
        }
    cases = result.get("cases")
    if not isinstance(cases, list):
        cases = [result] if target_kind == "case" else []
    case_maps = [item for item in cases if isinstance(item, Mapping)]
    total_cases = _count(result.get("total_cases")) or len(case_maps)
    raw_passed_cases = result.get("passed_cases")
    passed_cases = (
        sum(1 for item in case_maps if _case_bool(item, "passed"))
        if raw_passed_cases is None
        else _count(raw_passed_cases)
    )
    if target_kind == "case" and not result.get("cases"):
        passed_cases = 1 if result.get("passed") else 0
    agent_completed = _bool_metric(
        result.get("agent_completed_cases"),
        default=all(_case_bool(item, "agent_completed") for item in case_maps),
    )
    verification_passed = _bool_metric(
        result.get("verification_passed_cases"),
        default=all(_case_bool(item, "verification_passed") for item in case_maps),
    )
    if total_cases and result.get("agent_completed_cases") is not None:
        agent_completed = _count(result.get("agent_completed_cases")) == total_cases
    if total_cases and result.get("verification_passed_cases") is not None:
        verification_passed = _count(result.get("verification_passed_cases")) == total_cases
    tool_calls = _mapping(result.get("tool_calls"))
    if not tool_calls:
        for item in case_maps:
            for name, count in _mapping(item.get("tool_calls")).items():
                tool_calls[name] = _count(tool_calls.get(name)) + _count(count)
    steps = _count(result.get("steps"))
    if not steps:
        steps = sum(_count(item.get("steps")) for item in case_maps)
    agent_duration = _number(result.get("agent_duration_seconds"))
    if not agent_duration:
        agent_duration = sum(_number(item.get("agent_duration_seconds")) for item in case_maps)
    total_duration = _number(
        result.get("total_duration_seconds", result.get("duration_seconds"))
    )
    if not total_duration:
        total_duration = sum(_number(item.get("duration_seconds")) for item in case_maps)
    docker_failures = _count(result.get("docker_failure_count"))
    docker_commands = _count(result.get("docker_command_count"))
    fallback_count = _count(result.get("fallback_count"))
    if not docker_commands:
        docker_commands = sum(_count(item.get("docker_command_count")) for item in case_maps)
    if not docker_failures:
        docker_failures = sum(_count(item.get("docker_failure_count")) for item in case_maps)
    if not fallback_count:
        fallback_count = sum(_count(item.get("fallback_count")) for item in case_maps)
    files: list[str] = []
    reasons: list[str] = []
    for item in case_maps:
        for path in item.get("files_changed", []) or []:
            if path not in files:
                files.append(str(path))
        for reason in item.get("failure_reasons", []) or []:
            if str(reason) not in reasons:
                reasons.append(str(reason))
    for reason in result.get("failure_reasons", []) or []:
        if str(reason) not in reasons:
            reasons.append(str(reason))
    execution_locations = _mapping(result.get("execution_locations"))
    if not execution_locations and case_maps:
        execution_locations = _mapping(case_maps[0].get("execution"))
    agent_location = execution_locations.get("agent_commands")
    if agent_location is None:
        agent_location = execution_locations.get("agent_command_location")
    baseline_location = execution_locations.get(
        "baseline", execution_locations.get("baseline_location", "host")
    )
    verification_location = execution_locations.get(
        "verification", execution_locations.get("verification_location", "host")
    )
    return {
        "passed": bool(result.get("passed")) and passed_cases == total_cases,
        "success_rate": {
            "passed": passed_cases,
            "total": total_cases,
            "rate": round(passed_cases / total_cases, 4) if total_cases else 0.0,
            "percent": round(passed_cases / total_cases * 100, 2) if total_cases else 0.0,
        },
        "agent_completed": agent_completed,
        "verification_passed": verification_passed,
        "steps": steps,
        "tool_calls": {str(name): _count(count) for name, count in tool_calls.items()},
        "tool_call_total": sum(_count(count) for count in tool_calls.values()),
        "agent_duration_seconds": agent_duration,
        "total_duration_seconds": total_duration,
        "docker_command_count": docker_commands,
        "docker_failure_count": docker_failures,
        "fallback_count": fallback_count,
        "files_changed": files,
        "docker_image": result.get("docker_image")
        or (DEFAULT_DOCKER_IMAGE if str(result.get("executor")) == "docker" else None),
        "execution_locations": {
            "agent_commands": agent_location,
            "baseline": baseline_location,
            "verification": verification_location,
        },
        "failure_reasons": reasons,
    }


def _result_trace_executors(
    result: Mapping[str, Any],
    target_kind: str,
) -> list[str]:
    if target_kind == "case":
        trace = _mapping(result.get("trace"))
        value = trace.get("executor")
        return [str(value)] if value is not None else []
    cases = result.get("cases")
    if not isinstance(cases, list):
        return []
    return [
        str(value)
        for item in cases
        if isinstance(item, Mapping)
        for value in [item.get("trace_executor")]
        if value is not None
    ]


def _result_case_count(result: Mapping[str, Any], target_kind: str) -> int:
    if target_kind == "case":
        return 1
    cases = result.get("cases")
    case_count = len(cases) if isinstance(cases, list) else 0
    return _count(result.get("total_cases")) or case_count


def _compare_conditions(local: Mapping[str, Any], docker: Mapping[str, Any]) -> dict[str, Any]:
    metrics = (
        "steps",
        "tool_call_total",
        "agent_duration_seconds",
        "total_duration_seconds",
        "docker_command_count",
        "docker_failure_count",
        "fallback_count",
    )
    delta: dict[str, Any] = {}
    for key in metrics:
        local_value = _number(local.get(key))
        docker_value = _number(docker.get(key))
        delta[key] = {
            "local": _round_metric(local_value),
            "docker": _round_metric(docker_value),
            "docker_minus_local": _round_metric(docker_value - local_value),
            "overhead_percentage": (
                round((docker_value - local_value) / local_value * 100, 2)
                if local_value > 0
                else None
            ),
        }
    return delta


def _locate_result(output_root: Path, target_kind: str) -> Path | None:
    pattern = "suites/*/*/result.json" if target_kind == "suite" else "*/*/result.json"
    candidates = sorted(path for path in output_root.glob(pattern) if path.is_file())
    if len(candidates) > 1:
        raise ExecutionExperimentError(
            f"Runner output-root 中存在多个 result.json，无法确定本次产物: {output_root}"
        )
    return candidates[0] if candidates else None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionExperimentError(f"无法读取 Runner 结果 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExecutionExperimentError(f"Runner 结果必须是 JSON 对象: {path}")
    return value


def _render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Xi 执行隔离 A/B：{report['target']}",
        "",
        f"- 模式：`{report['mode']}`",
        f"- 实验 ID：`{report['experiment_id']}`",
        f"- 总体判定：{'通过' if report['passed'] else '未通过'}",
        f"- 总耗时：{_format_number(report['duration_seconds'])} 秒",
        "",
        "## 条件结果",
        "",
        "| 条件 | Case 成功率 | 通过 | Agent 完成 | 验证通过 | 步骤 | 工具调用 | Agent 耗时(s) | 总耗时(s) | Docker 命令 | Docker 失败 | fallback | 镜像 | 产物 |",
        "| --- | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for item in report["conditions"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{item['executor']}`",
                    f"{item['success_rate']['percent']:.2f}%",
                    _yes_no(item["passed"]),
                    _yes_no(item["agent_completed"]),
                    _yes_no(item["verification_passed"]),
                    str(item["steps"]),
                    str(item["tool_call_total"]),
                    _format_number(item["agent_duration_seconds"]),
                    _format_number(item["total_duration_seconds"]),
                    str(item["docker_command_count"]),
                    str(item["docker_failure_count"]),
                    str(item["fallback_count"]),
                    str(item["docker_image"] or "-"),
                    f"`{item['result_path'] or '-'}`",
                ]
            )
            + "|"
        )
    lines.extend(["", "## local → docker 差值", "", "| 指标 | local | docker | 差值 | 开销百分比 |", "| --- | ---: | ---: | ---: | ---: |"])
    for key, item in report["local_to_docker"].items():
        percentage = "-" if item["overhead_percentage"] is None else f"{item['overhead_percentage']:.2f}%"
        lines.append(
            f"| `{key}` | {_format_number(item['local'])} | {_format_number(item['docker'])} | "
            f"{_format_number(item['docker_minus_local'])} | {percentage} |"
        )
    lines.extend(["", "## 实验断言", ""])
    for name, value in report["assertions"].items():
        lines.append(f"- `{name}`：{'通过' if value else '失败'}")
    lines.extend(
        [
            "",
            "> Agent 的 run_command 在所选 Executor 中执行；baseline 与最终 verification 仍在宿主机运行，报告不会把它们误称为容器验证。",
            "> 这是执行后端 A/B 评测，不是 Docker 绝对安全性证明。工作区 bind mount、容器网络/资源限制和工作区内凭据可见性仍是明确边界。",
            "",
        ]
    )
    for item in report["conditions"]:
        if item["failure_reasons"]:
            lines.extend([f"### `{item['executor']}` 失败原因", ""])
            lines.extend(f"- {reason}" for reason in item["failure_reasons"])
            lines.append("")
    return "\n".join(lines)


def _print_summary(report: Mapping[str, Any]) -> None:
    status = "PASS" if report["passed"] else "FAIL"
    print(
        f"[{status}] execution target={report['target']} mode={report['mode']} "
        f"{len(report['conditions'])} conditions"
    )
    print("  CONDITION   PASS RATE    AGENT VERIFY STEPS TOOLS AGENT(s) TOTAL(s) DOCKER_CMD FAIL FALLBACK")
    for item in report["conditions"]:
        print(
            f"  {item['executor']:<10} {str(item['passed']):<5} {item['success_rate']['percent']:>6.2f}% "
            f"{str(item['agent_completed']):<5} {str(item['verification_passed']):<6} {item['steps']:>5} {item['tool_call_total']:>5} "
            f"{item['agent_duration_seconds']:>8.3f} {item['total_duration_seconds']:>8.3f} "
            f"{item['docker_command_count']:>10} {item['docker_failure_count']:>4} "
            f"{item['fallback_count']:>8}"
        )
        if item["failure_reasons"]:
            print(f"    原因: {'; '.join(item['failure_reasons'])}")
        print(f"    产物: {item['result_path']}")
    print(f"  报告: {report.get('artifacts', {}).get('run_directory', '-')}")


def _case_bool(item: Mapping[str, Any], key: str) -> bool:
    if key in item:
        return bool(item[key])
    criteria = _mapping(item.get("criteria"))
    return bool(criteria.get(key))


def _bool_metric(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


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


def _round_metric(value: float) -> float | int:
    rounded = round(value, 3)
    return int(rounded) if rounded.is_integer() else rounded


def _format_number(value: Any) -> str:
    number = _number(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


def _clip(value: Any, limit: int) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    return text if len(text) <= limit else text[: max(limit - 20, 0)] + "…[已截断]"


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _artifact_name(value: str) -> str:
    name = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in value
    ).strip("._")
    return name or "experiment"


def _new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
