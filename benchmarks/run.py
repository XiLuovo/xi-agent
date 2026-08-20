from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS_ROOT = Path(__file__).resolve().parent
CASES_ROOT = BENCHMARKS_ROOT / "cases"
SUITES_ROOT = BENCHMARKS_ROOT / "suites"
DEFAULT_CASE = "order_total_quantity"


class BenchmarkError(RuntimeError):
    """Raised when a benchmark manifest or artifact layout is invalid."""


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
    parser = argparse.ArgumentParser(description="运行可重复的 Xi Coding Agent benchmark")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--case", help="benchmarks/cases 下的 case 目录名")
    target.add_argument("--suite", help="benchmarks/suites 下的 suite manifest 名")
    parser.add_argument(
        "--mode",
        choices=("scripted", "live"),
        default="scripted",
        help="scripted 使用固定模型响应；live 使用 .env 中的真实模型",
    )
    parser.add_argument(
        "--context-strategy",
        choices=("search", "repo-map"),
        default="search",
        help="上下文策略：search 按需搜索；repo-map 预先提供仓库地图",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / ".xi" / "benchmarks",
        help="运行产物根目录",
    )
    parser.add_argument("--json", action="store_true", help="只向 stdout 输出 JSON 结果")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_windows_utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        if args.suite:
            result = _run_suite(
                args.suite,
                args.mode,
                args.output_root,
                context_strategy=args.context_strategy,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                _print_suite_summary(result)
        else:
            result = _run_case(
                args.case or DEFAULT_CASE,
                args.mode,
                args.output_root,
                context_strategy=args.context_strategy,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                _print_case_summary(result)
    except BenchmarkError as exc:
        raise SystemExit(f"benchmark 配置错误: {exc}") from exc
    return 0 if result["passed"] else 1


def _run_case(
    case_name: str,
    mode: str,
    output_root: Path,
    *,
    context_strategy: str = "search",
    run_dir: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    case_dir, manifest = _load_case(case_name)
    case_id = str(manifest["id"])
    run_id = run_id or _new_run_id()
    if run_dir is None:
        run_dir = output_root.expanduser().resolve() / _artifact_name(case_id) / run_id
    else:
        run_dir = run_dir.expanduser().resolve()
    workspace = run_dir / "workspace"
    trace_path = run_dir / "trace.jsonl"
    result_path = run_dir / "result.json"
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(case_dir / "fixture", workspace)

    test_file = str(manifest["test_file"])
    _require_workspace_file(workspace, test_file, "test_file")
    test_args = [sys.executable, "-m", "unittest", "-q", test_file]
    test_command = _format_command(test_args)
    task = str(manifest["task"]).replace("{{TEST_COMMAND}}", test_command)
    protected_paths = _manifest_path_list(manifest, "protected_paths") or []
    allowed_changed_paths = _manifest_path_list(manifest, "allowed_changed_paths")
    for relative in [*protected_paths, *(allowed_changed_paths or [])]:
        _resolve_under(workspace, relative, "manifest path")
    environment = _subprocess_environment()
    case_started_at = time.monotonic()

    baseline = _run_verification(test_args, workspace, environment)
    protected_before = _hash_paths(workspace, protected_paths)
    workspace_before = _snapshot_workspace(workspace)

    script_path: Path | None = None
    if mode == "scripted":
        source_script = case_dir / "scripted_responses.json"
        if not source_script.is_file():
            raise BenchmarkError(f"scripted case 缺少响应文件: {source_script}")
        try:
            script = json.loads(source_script.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BenchmarkError(f"无法读取 scripted 响应 {source_script}: {exc}") from exc
        if not isinstance(script, list):
            raise BenchmarkError(f"scripted 响应必须是 JSON 数组: {source_script}")
        script = _replace_placeholder(script, "{{TEST_COMMAND}}", test_command)
        script_path = run_dir / "scripted_responses.json"
        script_path.write_text(
            json.dumps(script, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    command = [
        sys.executable,
        "-m",
        "xi",
        "-p",
        task,
        "--workspace",
        str(workspace),
        "--trace",
        str(trace_path),
        "--context-strategy",
        context_strategy,
        "--max-steps",
        str(manifest.get("max_steps", 12)),
        "--max-duration",
        str(manifest.get("max_duration_seconds", 120)),
        "--allow-command",
        test_command,
        "--require-file-change",
        "--require-successful-command",
        test_command,
    ]
    if script_path is not None:
        command.extend(["--script", str(script_path)])

    agent_started_at = time.monotonic()
    try:
        completed_agent = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(manifest.get("max_duration_seconds", 120)) + 30,
            check=False,
        )
        agent_exit_code = completed_agent.returncode
        agent_stdout = completed_agent.stdout.strip()
        agent_stderr = completed_agent.stderr.strip()
        agent_timed_out = False
    except subprocess.TimeoutExpired as exc:
        agent_exit_code = 124
        agent_stdout = _timeout_text(exc.stdout)
        agent_stderr = _timeout_text(exc.stderr)
        agent_timed_out = True
    agent_duration_seconds = round(time.monotonic() - agent_started_at, 3)

    verification = _run_verification(test_args, workspace, environment)
    protected_after = _hash_paths(workspace, protected_paths)
    protected_unchanged = protected_before == protected_after
    workspace_after = _snapshot_workspace(workspace)
    changed_paths = _changed_paths(workspace_before, workspace_after)
    allowed_set = set(allowed_changed_paths or [])
    unexpected_paths = (
        [path for path in changed_paths if path not in allowed_set]
        if allowed_changed_paths is not None
        else []
    )
    allowed_paths_respected = allowed_changed_paths is None or not unexpected_paths
    trace_summary = _summarize_trace(trace_path)

    criteria = {
        "baseline_bug_reproduced": baseline["exit_code"] != 0,
        "agent_completed": agent_exit_code == 0,
        "verification_passed": verification["exit_code"] == 0,
        "protected_files_unchanged": protected_unchanged,
        "allowed_changed_paths_respected": allowed_paths_respected,
    }
    passed = all(criteria.values())
    duration_seconds = round(time.monotonic() - case_started_at, 3)
    failure_reasons = _case_failure_reasons(
        criteria,
        agent_exit_code=agent_exit_code,
        agent_timed_out=agent_timed_out,
        verification=verification,
        unexpected_paths=unexpected_paths,
    )
    result = {
        "schema_version": 1,
        "benchmark_id": case_id,
        "case_name": case_name,
        "title": manifest.get("title", case_id),
        "run_id": run_id,
        "mode": mode,
        "context_strategy": context_strategy,
        "passed": passed,
        "duration_seconds": duration_seconds,
        "criteria": criteria,
        "failure_reasons": failure_reasons,
        "agent": {
            "exit_code": agent_exit_code,
            "timed_out": agent_timed_out,
            "duration_seconds": agent_duration_seconds,
            "stdout": agent_stdout,
            "stderr": agent_stderr,
        },
        "baseline": baseline,
        "verification": verification,
        "trace": trace_summary,
        "workspace_changes": {
            "allowed_paths": allowed_changed_paths,
            "changed_paths": changed_paths,
            "unexpected_paths": unexpected_paths,
            "ignored": ["**/__pycache__/**", "*.pyc", "*.pyo"],
        },
        "artifacts": {
            "run_directory": str(run_dir),
            "workspace": str(workspace),
            "trace": str(trace_path),
            "result": str(result_path),
        },
    }
    _write_json(result_path, result)
    return result


def _run_suite(
    suite_name: str,
    mode: str,
    output_root: Path,
    *,
    context_strategy: str = "search",
) -> dict[str, Any]:
    manifest = _load_suite(suite_name)
    suite_id = str(manifest["id"])
    suite_run_id = _new_run_id()
    suite_dir = (
        output_root.expanduser().resolve()
        / "suites"
        / _artifact_name(suite_id)
        / suite_run_id
    )
    cases_artifact_dir = suite_dir / "cases"
    result_path = suite_dir / "result.json"
    cases_artifact_dir.mkdir(parents=True, exist_ok=False)
    suite_started_at = time.monotonic()
    case_results: list[dict[str, Any]] = []

    for index, case_name in enumerate(manifest["cases"], start=1):
        case_run_dir = cases_artifact_dir / _artifact_name(case_name)
        case_run_id = f"{suite_run_id}-{index:02d}"
        case_started_at = time.monotonic()
        try:
            case_result = _run_case(
                case_name,
                mode,
                output_root,
                context_strategy=context_strategy,
                run_dir=case_run_dir,
                run_id=case_run_id,
            )
        except Exception as exc:
            case_result = _runner_failure_result(
                case_name,
                mode,
                case_run_id,
                case_run_dir,
                round(time.monotonic() - case_started_at, 3),
                exc,
                context_strategy=context_strategy,
            )
        case_results.append(case_result)

    total_duration_seconds = round(time.monotonic() - suite_started_at, 3)
    summaries = [
        _suite_case_summary(case_name, case_result)
        for case_name, case_result in zip(manifest["cases"], case_results, strict=True)
    ]
    passed_cases = sum(1 for item in summaries if item["passed"])
    total_cases = len(summaries)
    failed_cases = total_cases - passed_cases
    success_rate = passed_cases / total_cases if total_cases else 0.0
    tool_calls: Counter[str] = Counter()
    usage: Counter[str] = Counter()
    context_characters = 0
    completion_rejections = 0
    for item in summaries:
        tool_calls.update(item["tool_calls"])
        characters = item["context"].get("characters")
        if isinstance(characters, int) and not isinstance(characters, bool):
            context_characters += characters
        completion_rejections += int(item["completion"].get("rejections") or 0)
        usage.update(
            {
                key: value
                for key, value in item["usage"].items()
                if isinstance(value, (int, float))
            }
        )

    result = {
        "schema_version": 1,
        "suite_id": suite_id,
        "title": manifest.get("title", suite_id),
        "mode": mode,
        "context_strategy": context_strategy,
        "run_id": suite_run_id,
        "passed": failed_cases == 0,
        "total_cases": total_cases,
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "success_rate": round(success_rate, 4),
        "success_rate_percent": round(success_rate * 100, 2),
        "total_duration_seconds": total_duration_seconds,
        "context_characters": context_characters,
        "completion_rejections": completion_rejections,
        "tool_calls": dict(sorted(tool_calls.items())),
        "usage": dict(sorted(usage.items())),
        "cases": summaries,
        "artifacts": {
            "run_directory": str(suite_dir),
            "cases_directory": str(cases_artifact_dir),
            "result": str(result_path),
        },
    }
    _write_json(result_path, result)
    return result


def _load_case(case_name: str) -> tuple[Path, dict[str, Any]]:
    case_dir = _resolve_under(CASES_ROOT, case_name, "benchmark case")
    manifest_path = case_dir / "case.json"
    fixture_dir = case_dir / "fixture"
    if not manifest_path.is_file() or not fixture_dir.is_dir():
        raise BenchmarkError(f"benchmark case 不完整: {case_dir}")
    manifest = _read_json_object(manifest_path)
    for key in ("id", "task", "test_file"):
        if not isinstance(manifest.get(key), str) or not str(manifest[key]).strip():
            raise BenchmarkError(f"{manifest_path} 缺少有效字段: {key}")
    return case_dir, manifest


def _load_suite(suite_name: str) -> dict[str, Any]:
    manifest_path = _resolve_under(SUITES_ROOT, f"{suite_name}.json", "benchmark suite")
    if not manifest_path.is_file():
        raise BenchmarkError(f"benchmark suite 不存在: {manifest_path}")
    manifest = _read_json_object(manifest_path)
    if not isinstance(manifest.get("id"), str) or not str(manifest["id"]).strip():
        raise BenchmarkError(f"{manifest_path} 缺少有效字段: id")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkError(f"{manifest_path} 的 cases 必须是非空数组")
    normalized_cases = [str(value).strip() for value in cases]
    if any(not value for value in normalized_cases):
        raise BenchmarkError(f"{manifest_path} 包含空 case 名称")
    if len(set(normalized_cases)) != len(normalized_cases):
        raise BenchmarkError(f"{manifest_path} 包含重复 case")
    manifest["cases"] = normalized_cases
    return manifest


def _resolve_under(root: Path, relative: str, label: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise BenchmarkError(f"{label} 超出目录: {relative}") from exc
    return candidate


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"无法读取 JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"JSON manifest 必须是对象: {path}")
    return value


def _require_workspace_file(workspace: Path, relative: str, field: str) -> None:
    candidate = _resolve_under(workspace, relative, field)
    if not candidate.is_file():
        raise BenchmarkError(f"{field} 文件不存在: {relative}")


def _manifest_path_list(
    manifest: dict[str, Any],
    key: str,
) -> list[str] | None:
    if key not in manifest:
        return None
    values = manifest[key]
    if not isinstance(values, list):
        raise BenchmarkError(f"{key} 必须是路径数组")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise BenchmarkError(f"{key} 包含无效路径")
        normalized.append(Path(value).as_posix())
    if len(set(normalized)) != len(normalized):
        raise BenchmarkError(f"{key} 包含重复路径")
    return normalized


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(PROJECT_ROOT / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root + os.pathsep + existing_pythonpath
        if existing_pythonpath
        else source_root
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def _run_verification(
    command: list[str],
    workspace: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "timed_out": False,
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "stdout": _timeout_text(exc.stdout),
            "stderr": _timeout_text(exc.stderr),
        }


def _hash_paths(workspace: Path, relative_paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in relative_paths:
        path = _resolve_under(workspace, relative, "protected path")
        if not path.is_file():
            hashes[relative] = "<missing>"
            continue
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _snapshot_workspace(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if _ignore_workspace_path(relative):
            continue
        snapshot[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _ignore_workspace_path(relative: Path) -> bool:
    return (
        "__pycache__" in relative.parts
        or relative.suffix.lower() in {".pyc", ".pyo"}
    )


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _replace_placeholder(value: Any, placeholder: str, replacement: str) -> Any:
    if isinstance(value, str):
        return value.replace(placeholder, replacement)
    if isinstance(value, list):
        return [_replace_placeholder(item, placeholder, replacement) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_placeholder(item, placeholder, replacement)
            for key, item in value.items()
        }
    return value


def _format_command(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return value.strip()


def _summarize_trace(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "exists": False,
            "event_count": 0,
            "tool_calls": {},
            "files_changed": [],
            "usage": {},
            "completion": {},
        }
    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tool_calls: dict[str, int] = {}
    files_changed: list[str] = []
    final_event = None
    usage: dict[str, Any] = {}
    context: dict[str, Any] = {}
    completion: dict[str, Any] = {"contract": None, "rejections": 0}
    for event in events:
        if event.get("type") == "run_started":
            payload = event.get("payload", {})
            context = {
                "strategy": payload.get("context_strategy"),
                "characters": payload.get("context_characters"),
            }
            completion["contract"] = payload.get("completion_contract")
        elif event.get("type") == "tool_started":
            tool = str(event.get("payload", {}).get("tool", "unknown"))
            tool_calls[tool] = tool_calls.get(tool, 0) + 1
        elif event.get("type") == "file_changed":
            changed = event.get("payload", {}).get("path")
            if changed and changed not in files_changed:
                files_changed.append(str(changed))
        elif event.get("type") == "completion_decided":
            if event.get("payload", {}).get("accepted") is False:
                completion["rejections"] += 1
        elif event.get("type") in {"run_finished", "run_failed"}:
            final_event = event
            event_usage = event.get("usage")
            if isinstance(event_usage, dict):
                usage = event_usage
    final_payload = final_event.get("payload", {}) if final_event else {}
    return {
        "exists": True,
        "event_count": len(events),
        "tool_calls": tool_calls,
        "files_changed": files_changed,
        "final_event": final_event.get("type") if final_event else None,
        "steps": final_payload.get("steps"),
        "usage": usage,
        "context": context,
        "completion": completion,
    }


def _case_failure_reasons(
    criteria: dict[str, bool],
    *,
    agent_exit_code: int,
    agent_timed_out: bool,
    verification: dict[str, Any],
    unexpected_paths: list[str],
) -> list[str]:
    reasons: list[str] = []
    if not criteria["baseline_bug_reproduced"]:
        reasons.append("baseline_bug_reproduced: 初始验证没有失败")
    if not criteria["agent_completed"]:
        suffix = "（超时）" if agent_timed_out else ""
        reasons.append(f"agent_completed: exit_code={agent_exit_code}{suffix}")
    if not criteria["verification_passed"]:
        reasons.append(f"verification_passed: exit_code={verification['exit_code']}")
    if not criteria["protected_files_unchanged"]:
        reasons.append("protected_files_unchanged: 受保护文件发生变化")
    if not criteria["allowed_changed_paths_respected"]:
        reasons.append(
            "allowed_changed_paths_respected: 非预期改动 " + ", ".join(unexpected_paths)
        )
    return reasons


def _runner_failure_result(
    case_name: str,
    mode: str,
    run_id: str,
    run_dir: Path,
    duration_seconds: float,
    exc: Exception,
    *,
    context_strategy: str = "search",
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"
    reason = f"runner_error: {type(exc).__name__}: {exc}"
    criteria = {
        "baseline_bug_reproduced": False,
        "agent_completed": False,
        "verification_passed": False,
        "protected_files_unchanged": False,
        "allowed_changed_paths_respected": False,
    }
    result = {
        "schema_version": 1,
        "benchmark_id": case_name,
        "case_name": case_name,
        "title": case_name,
        "run_id": run_id,
        "mode": mode,
        "context_strategy": context_strategy,
        "passed": False,
        "duration_seconds": duration_seconds,
        "criteria": criteria,
        "failure_reasons": [reason],
        "agent": {
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": 0.0,
            "stdout": "",
            "stderr": reason,
        },
        "baseline": {"exit_code": None},
        "verification": {"exit_code": None},
        "trace": {
            "exists": False,
            "event_count": 0,
            "tool_calls": {},
            "files_changed": [],
            "usage": {},
            "completion": {},
        },
        "workspace_changes": {
            "allowed_paths": None,
            "changed_paths": [],
            "unexpected_paths": [],
            "ignored": ["**/__pycache__/**", "*.pyc", "*.pyo"],
        },
        "artifacts": {
            "run_directory": str(run_dir),
            "workspace": str(run_dir / "workspace"),
            "trace": str(run_dir / "trace.jsonl"),
            "result": str(result_path),
        },
    }
    _write_json(result_path, result)
    return result


def _suite_case_summary(case_name: str, result: dict[str, Any]) -> dict[str, Any]:
    trace = result.get("trace", {})
    workspace_changes = result.get("workspace_changes", {})
    criteria = result.get("criteria", {})
    return {
        "case_name": case_name,
        "benchmark_id": result.get("benchmark_id", case_name),
        "title": result.get("title", case_name),
        "passed": bool(result.get("passed")),
        "steps": trace.get("steps"),
        "duration_seconds": result.get("duration_seconds"),
        "tool_calls": dict(trace.get("tool_calls", {})),
        "usage": dict(trace.get("usage", {})),
        "context": dict(trace.get("context", {})),
        "completion": dict(trace.get("completion", {})),
        "files_changed": list(
            workspace_changes.get("changed_paths") or trace.get("files_changed", [])
        ),
        "artifact_path": result.get("artifacts", {}).get("result"),
        "run_directory": result.get("artifacts", {}).get("run_directory"),
        "failed_criteria": [key for key, value in criteria.items() if not value],
        "failure_reasons": list(result.get("failure_reasons", [])),
    }


def _print_case_summary(result: dict[str, Any]) -> None:
    status = "PASS" if result["passed"] else "FAIL"
    criteria = result["criteria"]
    trace = result["trace"]
    changes = result["workspace_changes"]
    print(
        f"[{status}] {result['benchmark_id']} ({result['mode']}, "
        f"context={result.get('context_strategy', 'search')})"
    )
    print(f"  初始 Bug 可复现: {criteria['baseline_bug_reproduced']}")
    print(f"  Agent 正常结束: {criteria['agent_completed']}")
    print(f"  最终验证通过: {criteria['verification_passed']}")
    print(f"  受保护文件未变: {criteria['protected_files_unchanged']}")
    print(f"  改动路径合规: {criteria['allowed_changed_paths_respected']}")
    print(f"  工具调用: {trace.get('tool_calls', {})}")
    print(f"  模型用量: {trace.get('usage', {})}")
    print(f"  完成契约拒绝次数: {trace.get('completion', {}).get('rejections', 0)}")
    print(f"  改动文件: {changes.get('changed_paths', [])}")
    if result["failure_reasons"]:
        print(f"  失败原因: {'; '.join(result['failure_reasons'])}")
    print(f"  产物目录: {result['artifacts']['run_directory']}")


def _print_suite_summary(result: dict[str, Any]) -> None:
    status = "PASS" if result["passed"] else "FAIL"
    print(
        f"[{status}] suite {result['suite_id']} ({result['mode']}, "
        f"context={result.get('context_strategy', 'search')}) "
        f"{result['passed_cases']}/{result['total_cases']} "
        f"({result['success_rate_percent']:.2f}%)"
    )
    print("  CASE                         STATUS STEPS DURATION  TOOLS FILES")
    for item in result["cases"]:
        case_name = str(item["case_name"])
        case_status = "PASS" if item["passed"] else "FAIL"
        steps = "-" if item["steps"] is None else str(item["steps"])
        duration = float(item["duration_seconds"] or 0.0)
        tool_count = sum(int(value) for value in item["tool_calls"].values())
        files = ",".join(item["files_changed"]) or "-"
        print(
            f"  {case_name:<28} {case_status:<6} {steps:>5} "
            f"{duration:>8.3f}s {tool_count:>5} {files}"
        )
        if item["failure_reasons"]:
            print(f"    原因: {'; '.join(item['failure_reasons'])}")
        print(f"    产物: {item['artifact_path']}")
    print(f"  汇总工具调用: {result['tool_calls']}")
    print(f"  汇总首次上下文字符数: {result.get('context_characters', 0)}")
    print(f"  汇总完成契约拒绝次数: {result.get('completion_rejections', 0)}")
    print(f"  汇总模型用量: {result.get('usage', {})}")
    print(f"  总耗时: {result['total_duration_seconds']:.3f}s")
    print(f"  Suite 产物: {result['artifacts']['run_directory']}")


def _artifact_name(value: str) -> str:
    name = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in value
    ).strip("._")
    return name or "case"


def _new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
