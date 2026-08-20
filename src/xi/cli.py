"""Command-line adapters for the shared Xi AgentRuntime."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence
from uuid import uuid4

try:  # Optional at import time; the package remains usable with ScriptedModel.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only in dependency-free installs
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

try:  # Rich is declared by the project, but keep ``xi --help`` usable before install.
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.text import Text
except ImportError:  # pragma: no cover - exercised only in dependency-free installs
    class Text:
        def __init__(self, value: Any = "", style: str | None = None) -> None:
            self.value = str(value)

        def append(self, value: Any, style: str | None = None) -> None:
            self.value += str(value)

        def __str__(self) -> str:
            return self.value

    class Console:
        def __init__(self, *, stderr: bool = False) -> None:
            self._stream = sys.stderr if stderr else sys.stdout

        def print(self, *values: Any, **_kwargs: Any) -> None:
            rendered = " ".join(_strip_markup(str(value)) for value in values)
            print(rendered, file=self._stream)

    class Panel:
        def __init__(self, value: Any, **kwargs: Any) -> None:
            self.value = value

        @classmethod
        def fit(cls, value: Any, **kwargs: Any) -> "Panel":
            return cls(value, **kwargs)

        def __str__(self) -> str:
            return str(self.value)

    class Prompt:
        @staticmethod
        def ask(prompt: str, *, console: Console | None = None) -> str:
            return input(_strip_markup(prompt) + ": ")

    class Confirm:
        @staticmethod
        def ask(prompt: str, *, default: bool = False, console: Console | None = None) -> bool:
            answer = input(_strip_markup(prompt) + (" [Y/n]: " if default else " [y/N]: "))
            if not answer.strip():
                return default
            return answer.strip().lower() in {"y", "yes", "是"}

    def _strip_markup(value: str) -> str:
        return re.sub(r"\[/?[A-Za-z][^\]]*\]", "", value)

from .completion import EvidenceCompletionContract
from .context import RepoMapContextBuilder, SearchContextBuilder
from .events import JsonlSessionStore
from .executor import DryRunExecutor, RestrictedLocalExecutor
from .models import OpenAICompatibleModel, ScriptedModel
from .policy import DefaultPolicy
from .replay import ReplayError, load_trace
from .runtime import AgentRuntime
from .session import SessionProjection, SessionProjectionError, project_session


_console = Console()
_error_console = Console(stderr=True)


def _configure_windows_utf8_streams() -> None:
    """Use UTF-8 for Windows stdout/stderr when their wrappers allow it."""

    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            # Embedded callers may replace the standard streams with wrappers
            # that expose but do not support reconfiguration. CLI startup must
            # remain usable in those hosts.
            continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xi",
        description="Xi：在受限工作区内搜索、修改并验证代码的 Python Coding Agent",
    )
    parser.add_argument("command", nargs="?", help="可直接写任务，或使用 run 子命令")
    parser.add_argument("command_args", nargs="*", help="run 子命令后的任务文本")
    parser.add_argument("--task", help="--prompt 的兼容别名")
    parser.add_argument("-p", "--prompt", help="一次性执行任务后退出（headless）")
    parser.add_argument(
        "--workspace",
        help="受限工作区目录；普通运行默认当前目录，resume 默认使用 trace 中记录的目录",
    )
    parser.add_argument("--trace", help="JSONL trace 路径；默认写入 .xi/traces")
    parser.add_argument("--jsonl", action="store_true", help="--json 的兼容别名")
    parser.add_argument("--script", help="ScriptedModel 的 JSON/JSONL 响应文件")
    parser.add_argument("--model", help="OpenAI-compatible 模型名")
    parser.add_argument("--base-url", help="OpenAI-compatible API base URL")
    parser.add_argument("--api-key", help="API key；也可使用 XI_API_KEY/OPENAI_API_KEY")
    parser.add_argument(
        "--context-strategy",
        choices=("search", "repo-map"),
        default="search",
        help="上下文策略：search 按需搜索；repo-map 预先提供受限仓库地图",
    )
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-duration", type=float, default=300.0)
    parser.add_argument("--model-timeout", type=float, default=60.0)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    parser.add_argument(
        "--allow-command",
        action="append",
        default=[],
        help="精确允许一条命令；可重复使用，适合固定 benchmark 测试命令",
    )
    parser.add_argument(
        "--require-file-change",
        action="store_true",
        help="完成前必须产生至少一次成功的文件修改；默认关闭",
    )
    parser.add_argument(
        "--require-successful-command",
        action="append",
        default=[],
        help="完成前必须成功执行的精确命令；可重复使用，文件修改后需重新执行",
    )
    parser.add_argument("--auto-approve", action="store_true", help="自动批准未命中拒绝规则的命令")
    parser.add_argument("--dry-run", action="store_true", help="只记录修改和命令，不实际执行")
    parser.add_argument("--json", action="store_true", help="将事件以 JSONL 输出到 stdout")
    parser.add_argument("--version", action="version", version="xi-agent 0.1.0")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_windows_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = args.json or args.jsonl
    if args.command == "replay":
        return _run_replay_command(parser, args)
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    projection: SessionProjection | None = None
    if args.command == "resume":
        projection = _load_resume_projection(parser, args)
        if projection is None:
            return 1
        prompt = args.prompt if args.prompt is not None else args.task
    else:
        prompt = _resolve_prompt(args)
    if args.json and prompt is None:
        parser.error("--json/--jsonl 仅用于一次性 headless 任务")
    workspace = (
        projection.workspace
        if projection is not None and args.workspace is None
        else Path(args.workspace or ".").expanduser().resolve()
    )
    if projection is not None and workspace != projection.workspace:
        parser.error(
            "resume 必须使用 trace 中记录的工作区；当前为 "
            f"{projection.workspace}"
        )
    if not workspace.exists() or not workspace.is_dir():
        parser.error(f"工作区不是目录: {workspace}")
    trace_path = projection.source if projection is not None else _resolve_trace_path(args.trace, workspace)
    if projection is not None and args.trace is not None:
        requested_trace = Path(args.trace).expanduser().resolve()
        if requested_trace != projection.source:
            parser.error("resume 会继续写入原 trace，不能通过 --trace 改写到其他文件")

    on_append = _print_event if args.json else (_render_interactive_event if prompt is None else None)
    store = JsonlSessionStore(trace_path, on_append=on_append)
    try:
        model = _build_model(args)
        if args.dry_run:
            executor = DryRunExecutor(workspace)
        else:
            executor = RestrictedLocalExecutor(
                workspace,
                command_timeout_seconds=args.command_timeout,
            )
        context_strategy = projection.context_strategy if projection is not None else args.context_strategy
        runtime = AgentRuntime(
            model,
            workspace=workspace,
            executor=executor,
            policy=DefaultPolicy(allowed_commands=args.allow_command),
            session_store=store,
            context_builder=(
                RepoMapContextBuilder()
                if context_strategy == "repo-map"
                else SearchContextBuilder()
            ),
            completion_contract=(
                EvidenceCompletionContract(
                    require_file_change=args.require_file_change,
                    required_commands=args.require_successful_command,
                )
                if args.require_file_change or args.require_successful_command
                else None
            ),
            max_steps=args.max_steps,
            max_duration_seconds=args.max_duration,
            interactive=prompt is None,
            auto_approve=args.auto_approve,
            approval_callback=_approval_prompt,
        )
        if projection is not None:
            runtime.restore_session(projection)
        if prompt is not None:
            result = runtime.run(
                prompt,
                interactive=False,
                continue_session=projection is not None,
            )
            if not args.json:
                _console.print(Text(result.text))
                _error_console.print(Text(f"trace: {trace_path}", style="dim"))
            return 0 if result.success else 1
        return _interactive_loop(runtime, trace_path)
    except KeyboardInterrupt:
        _error_console.print("\n已退出。")
        return 130
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            message = Text("xi 启动失败：", style="red")
            message.append(str(exc))
            _error_console.print(message)
        return 1
    finally:
        store.close()


def _run_replay_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.prompt is not None or args.task is not None:
        parser.error("replay 不接受任务 prompt；用法：xi replay <trace.jsonl>")
    if len(args.command_args) != 1:
        parser.error("用法：xi replay <trace.jsonl>")
    try:
        trace = load_trace(args.command_args[0])
    except ReplayError as exc:
        message = Text("replay 失败：", style="red")
        message.append(str(exc))
        _error_console.print(message)
        return 1
    _console.print(Text(trace.render()))
    return 0


def _load_resume_projection(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> SessionProjection | None:
    if len(args.command_args) != 1:
        parser.error("用法：xi resume <trace.jsonl>；继续任务可加 -p \"任务\"")
    try:
        return project_session(args.command_args[0])
    except SessionProjectionError as exc:
        message = Text("resume 失败：", style="red")
        message.append(str(exc))
        _error_console.print(message)
        return None


def _resolve_prompt(args: argparse.Namespace) -> str | None:
    if args.prompt is not None:
        return args.prompt
    if args.task is not None:
        return args.task
    if args.command == "run":
        value = " ".join(args.command_args).strip()
        return value or None
    if args.command:
        suffix = " ".join(args.command_args).strip()
        return (args.command + (" " + suffix if suffix else "")).strip()
    return None


def _build_model(args: argparse.Namespace):
    if args.script:
        path = Path(args.script).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"script 文件不存在: {path}")
        return ScriptedModel(_load_script(path))
    return OpenAICompatibleModel(
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        timeout_seconds=args.model_timeout,
    )


def _load_script(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, list):
        return value
    return [value]


def _resolve_trace_path(raw: str | None, workspace: Path) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return workspace / ".xi" / "traces" / f"{stamp}-{uuid4().hex[:8]}.jsonl"


def _print_event(event) -> None:
    print(event.to_json(), flush=True)


def _render_interactive_event(event) -> None:
    payload = event.payload
    if event.type == "model_requested":
        _console.print(Text(f"思考中（第 {payload.get('step')} 步）…", style="dim"))
    elif event.type == "tool_started":
        line = Text("→ ", style="cyan")
        line.append(str(payload.get("tool")))
        _console.print(line)
    elif event.type == "tool_finished":
        succeeded = bool(payload.get("success"))
        line = Text("✓ " if succeeded else "✗ ", style="green" if succeeded else "red")
        line.append(str(payload.get("tool")))
        _console.print(line)
    elif event.type == "policy_decided" and payload.get("decision") == "deny":
        line = Text("策略拒绝：", style="yellow")
        line.append(str(payload.get("reason")))
        _console.print(line)
    elif event.type == "completion_decided" and payload.get("accepted") is False:
        missing = payload.get("missing") or []
        line = Text("完成契约要求继续：", style="yellow")
        line.append("；".join(str(item) for item in missing))
        _console.print(line)


def _approval_prompt(tool_name, arguments, _decision) -> bool:
    details = json.dumps(dict(arguments), ensure_ascii=False, indent=2)
    _console.print(Panel(Text(details), title=f"需要确认：{tool_name}", border_style="yellow"))
    return Confirm.ask("允许执行？", default=False, console=_console)


def _interactive_loop(runtime: AgentRuntime, trace_path: Path) -> int:
    _console.print(
        Panel.fit(
            "输入任务开始；[bold]/help[/bold] 查看命令；[bold]/exit[/bold] 退出。",
            title="Xi Coding Agent",
            border_style="cyan",
        )
    )
    while True:
        try:
            task = Prompt.ask("\n[bold cyan]你[/bold cyan]", console=_console).strip()
        except EOFError:
            task = "/exit"
        if not task:
            continue
        if task in {"/exit", "/quit", ":q"}:
            _console.print(Text(f"会话 trace: {trace_path}", style="dim"))
            return 0
        if task == "/help":
            _console.print("直接输入任务；[bold]/reset[/bold] 清除对话上下文；[bold]/exit[/bold] 退出。")
            continue
        if task == "/reset":
            runtime.reset_conversation()
            _console.print("已清除当前对话上下文。")
            continue
        result = runtime.run(task, interactive=True, continue_session=True)
        style = "green" if result.success else "red"
        _console.print(Panel(Text(result.text), title="Xi", border_style=style))


__all__ = ["build_parser", "main"]
