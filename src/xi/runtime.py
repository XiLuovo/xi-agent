"""The shared interactive/headless Xi Agent Runtime."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .completion import (
    CompletionContract,
    PermissiveCompletionContract,
    ToolExecutionEvidence,
    evidence_from_executions,
)
from .compaction import (
    CompactionError,
    ContextCompactor,
    DeterministicCompactor,
)
from .context import ContextBuilder, SearchContextBuilder
from .events import Event, JsonlSessionStore, SessionStore
from .executor import RestrictedLocalExecutor
from .models import Model, ModelResponse, ToolCall
from .policy import Decision, DefaultPolicy, Policy, PolicyContext, PolicyDecision
from .session import SessionProjection
from .tools import ToolExecutor, ToolRegistry, ToolResult, default_tools


@dataclass(slots=True)
class RunResult:
    run_id: str
    task: str
    text: str
    success: bool
    steps: int
    trace_events: list[Event] = field(default_factory=list)
    error: str | None = None
    session_id: str = ""


class AgentRuntime:
    """Run tasks through one model/tool/policy/executor loop.

    The public seam is :meth:`run`; CLI surfaces only provide adapters around
    it.  ``continue_session=True`` keeps the model conversation for an
    interactive session while a headless invocation starts with a clean task.
    """

    def __init__(
        self,
        model: Model,
        *,
        workspace: str | Path = ".",
        tools: ToolRegistry | Sequence[Any] | None = None,
        policy: Policy | None = None,
        executor: ToolExecutor | None = None,
        session_store: SessionStore | None = None,
        context_builder: ContextBuilder | None = None,
        completion_contract: CompletionContract | None = None,
        max_steps: int = 20,
        max_duration_seconds: float = 300.0,
        interactive: bool = False,
        auto_approve: bool = False,
        approval_callback: Callable[[str, Mapping[str, Any], PolicyDecision], bool] | None = None,
        max_observation_chars: int = 12_000,
        session_id: str | None = None,
        context_budget_chars: int | None = None,
        compactor: ContextCompactor | None = None,
        workspace_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.exists() or not self.workspace.is_dir():
            raise ValueError(f"工作区不是目录: {self.workspace}")
        if tools is None:
            self.tools = default_tools()
        elif isinstance(tools, ToolRegistry):
            self.tools = tools
        else:
            self.tools = ToolRegistry(list(tools))
        self.executor = executor if executor is not None else RestrictedLocalExecutor(self.workspace)
        self.policy = policy if policy is not None else DefaultPolicy()
        self.session_store = (
            session_store
            if session_store is not None
            else JsonlSessionStore(_default_trace_path(self.workspace))
        )
        self.context_builder = context_builder if context_builder is not None else SearchContextBuilder()
        self.completion_contract = (
            completion_contract
            if completion_contract is not None
            else PermissiveCompletionContract()
        )
        self.max_steps = max(1, int(max_steps))
        self.max_duration_seconds = max(0.1, float(max_duration_seconds))
        self.interactive = interactive
        self.auto_approve = auto_approve
        self.approval_callback = approval_callback
        self.max_observation_chars = max(int(max_observation_chars), 256)
        if context_budget_chars is not None:
            if isinstance(context_budget_chars, bool):
                raise ValueError("context_budget_chars 必须是正整数")
            try:
                configured_budget = int(context_budget_chars)
            except (TypeError, ValueError) as exc:
                raise ValueError("context_budget_chars 必须是正整数") from exc
            if configured_budget <= 0:
                raise ValueError("context_budget_chars 必须是正整数")
            self.context_budget_chars: int | None = configured_budget
        else:
            self.context_budget_chars = None
        self.compactor = compactor if compactor is not None else DeterministicCompactor()
        self.workspace_metadata = _json_safe(dict(workspace_metadata or {}))
        self._worktree_created_emitted = False
        self._conversation: list[dict[str, Any]] = []
        self._session_id = session_id or uuid4().hex
        self._session_parent_id: str | None = None
        self._forked_from: dict[str, str] | None = None
        self._recovered_from: dict[str, str] | None = None
        self._last_compaction_fingerprint: str | None = None

    @property
    def conversation(self) -> list[dict[str, Any]]:
        return deepcopy(self._conversation)

    @property
    def session_id(self) -> str:
        """Stable identity for the in-memory Session lineage."""

        return self._session_id

    def reset_conversation(self) -> None:
        self._conversation.clear()
        self._last_compaction_fingerprint = None

    def restore_session(self, projection: SessionProjection) -> None:
        """Restore conversation and lineage from the current session store.

        The store must already point at ``projection.source`` so the restored
        parent event exists in the same append-only event stream.
        """

        if projection.workspace != self.workspace:
            raise ValueError(
                "恢复会话的工作区与 Runtime 不一致: "
                f"{projection.workspace} != {self.workspace}"
            )
        stored_event_ids = {event.event_id for event in self.session_store.events}
        if projection.last_event_id not in stored_event_ids:
            raise ValueError("当前 session store 不包含待恢复 trace 的最后事件")
        self._conversation = deepcopy(list(projection.messages))
        self._session_id = projection.session_id or projection.run_id
        self._session_parent_id = projection.last_event_id
        self._forked_from = None
        self._last_compaction_fingerprint = _messages_fingerprint(self._conversation)
        self._recovered_from = (
            {
                "trace": str(projection.source),
                "session_id": projection.session_id or projection.run_id,
                "run_id": projection.run_id,
                "event_id": projection.last_event_id,
                "state": projection.recovery.state,
                "checkpoint_event_id": projection.recovery.checkpoint_event_id,
            }
            if projection.recovery is not None
            else None
        )

    def fork_session(self, projection: SessionProjection) -> None:
        """Seed a new Session from historical context in another Trace.

        Unlike :meth:`restore_session`, Fork keeps no cross-Trace parent link
        and never reuses the source Session identity. The source location is
        recorded once on the new Trace's first ``run_started`` event.
        """

        if projection.workspace != self.workspace:
            raise ValueError(
                "分叉会话的工作区与 Runtime 不一致: "
                f"{projection.workspace} != {self.workspace}"
            )
        if any(True for _event in self.session_store.events):
            raise ValueError("fork 必须写入空的新 trace")
        self._conversation = deepcopy(list(projection.messages))
        self._session_id = uuid4().hex
        self._session_parent_id = None
        self._recovered_from = None
        self._last_compaction_fingerprint = _messages_fingerprint(self._conversation)
        self._forked_from = {
            "trace": str(projection.source),
            "session_id": projection.session_id or projection.run_id,
            "run_id": projection.run_id,
            "event_id": projection.last_event_id,
        }

    def close(self) -> None:
        self.session_store.close()

    def __enter__(self) -> "AgentRuntime":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def run(
        self,
        task: str,
        *,
        interactive: bool | None = None,
        continue_session: bool = False,
        approval_callback: Callable[[str, Mapping[str, Any], PolicyDecision], bool] | None = None,
    ) -> RunResult:
        """Execute one task and return a stable result object."""

        task = str(task).strip()
        if not task:
            raise ValueError("任务不能为空")
        is_interactive = self.interactive if interactive is None else interactive
        approve = approval_callback or self.approval_callback
        is_session_run = bool(continue_session)
        forked_from = deepcopy(self._forked_from)
        recovered_from = deepcopy(self._recovered_from)
        is_fork_run = forked_from is not None
        # Every user turn is a distinct Run. Resume/Recovery reuse the Session
        # identity and prior event; the first Fork Run starts a new causal root.
        run_id = uuid4().hex
        session_parent_id = self._session_parent_id if is_session_run else None
        run_events: list[Event] = []
        started_at = time.monotonic()

        def emit(
            event_type: str,
            payload: Mapping[str, Any] | None = None,
            *,
            parent_id: str | None = None,
            usage: Mapping[str, Any] | None = None,
        ) -> Event:
            event = Event(
                type=event_type,
                run_id=run_id,
                session_id=self._session_id,
                parent_id=parent_id,
                payload=_json_safe(dict(payload or {})),
                usage=_json_safe(dict(usage)) if usage else None,
            )
            self.session_store.append(event)
            run_events.append(event)
            return event

        context_strategy = _context_strategy_name(self.context_builder)
        completion_contract_name = _completion_contract_name(self.completion_contract)
        context_error: Exception | None = None
        try:
            if continue_session and self._conversation:
                messages = [dict(item) for item in self._conversation]
                context_characters = sum(
                    len(str(message.get("content", "")))
                    for message in messages
                    if message.get("role") == "system"
                )
            else:
                system = self.context_builder.build(task, self.workspace)
                messages = [{"role": "system", "content": system}]
                context_characters = len(system)
        except Exception as exc:
            messages = []
            context_characters = 0
            context_error = exc

        started_payload: dict[str, Any] = {
            "task": task,
            "workspace": str(self.workspace),
            "executor": _executor_name(self.executor),
            "interactive": is_interactive,
            "max_steps": self.max_steps,
            "context_strategy": context_strategy,
            "context_characters": context_characters,
            "completion_contract": completion_contract_name,
            "session_continued": bool(
                is_session_run and self._conversation and not is_fork_run
            ),
        }
        if self.workspace_metadata:
            started_payload.update(self.workspace_metadata)
        if forked_from is not None:
            started_payload["forked_from"] = forked_from
        if recovered_from is not None:
            started_payload["recovered_from"] = recovered_from
        if self.context_budget_chars is not None:
            started_payload["context_budget_chars"] = self.context_budget_chars
        started = emit(
            "run_started",
            started_payload,
            parent_id=session_parent_id,
        )
        lifecycle_event: Event | None = None
        if (
            self.workspace_metadata.get("workspace_mode") == "worktree"
            and not self._worktree_created_emitted
        ):
            lifecycle_event = emit(
                "worktree_created",
                dict(self.workspace_metadata),
                parent_id=started.event_id,
            )
            self._worktree_created_emitted = True
        if is_fork_run:
            self._forked_from = None
        if recovered_from is not None:
            self._recovered_from = None

        if context_error is not None:
            return self._failed(
                emit,
                run_events,
                run_id,
                task,
                0,
                f"运行时异常: {context_error}",
                parent_id=started.event_id,
                continue_session=is_session_run,
                messages=messages,
            )

        try:
            messages.append({"role": "user", "content": task})
            tool_definitions = self.tools.definitions()
            final_text = ""
            total_usage: dict[str, Any] = {}
            execution_evidence: list[ToolExecutionEvidence] = []
            turn_parent_id = lifecycle_event.event_id if lifecycle_event else started.event_id

            for step in range(1, self.max_steps + 1):
                elapsed = time.monotonic() - started_at
                if elapsed > self.max_duration_seconds:
                    return self._failed(
                        emit,
                        run_events,
                        run_id,
                        task,
                        step - 1,
                        "达到运行时间上限",
                        continue_session=is_session_run,
                        messages=messages,
                    )
                if self.context_budget_chars is not None:
                    current_fingerprint = _messages_fingerprint(messages)
                    if current_fingerprint != self._last_compaction_fingerprint:
                        try:
                            compaction = self.compactor.compact(
                                messages,
                                self.context_budget_chars,
                            )
                        except CompactionError as exc:
                            return self._failed(
                                emit,
                                run_events,
                                run_id,
                                task,
                                step - 1,
                                f"上下文压缩失败: {exc}",
                                parent_id=turn_parent_id,
                                continue_session=is_session_run,
                                messages=messages,
                            )
                        if compaction.before_characters > self.context_budget_chars:
                            compacted_event = emit(
                                "context_compacted",
                                {
                                    "strategy": compaction.strategy,
                                    "budget_chars": self.context_budget_chars,
                                    "before_message_count": compaction.before_message_count,
                                    "after_message_count": compaction.after_message_count,
                                    "before_characters": compaction.before_characters,
                                    "after_characters": compaction.after_characters,
                                    "dropped_message_count": compaction.dropped_message_count,
                                    "messages": _json_safe(
                                        [dict(message) for message in compaction.messages]
                                    ),
                                    "summary": compaction.summary,
                                },
                                parent_id=turn_parent_id,
                            )
                            messages = [dict(message) for message in compaction.messages]
                            turn_parent_id = compacted_event.event_id
                            self._last_compaction_fingerprint = _messages_fingerprint(messages)
                        else:
                            # The compactor may be called for an under-budget
                            # context; remember the snapshot so an unchanged
                            # context is not reconsidered on the next iteration.
                            self._last_compaction_fingerprint = current_fingerprint
                request_event = emit(
                    "model_requested",
                    {
                        "step": step,
                        "message_count": len(messages),
                        "tool_count": len(tool_definitions),
                        "messages": _trace_messages(messages, self.max_observation_chars),
                        "tools": [
                            definition.get("function", {}).get("name", "")
                            for definition in tool_definitions
                        ],
                    },
                    parent_id=turn_parent_id,
                )
                try:
                    response = self.model.complete(messages, tool_definitions)
                    response = ModelResponse.from_value(response)
                except Exception as exc:  # model adapters expose errors as run failures
                    return self._failed(
                        emit,
                        run_events,
                        run_id,
                        task,
                        step,
                        f"模型调用失败: {exc}",
                        parent_id=request_event.event_id,
                        continue_session=is_session_run,
                        messages=messages,
                    )
                _merge_usage(total_usage, response.usage)
                response_event = emit(
                    "model_responded",
                    {
                        "step": step,
                        "text": _clip(response.text, self.max_observation_chars),
                        "tool_calls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}
                            for call in response.tool_calls
                        ],
                        "finish_reason": response.finish_reason,
                    },
                    parent_id=request_event.event_id,
                    usage=response.usage,
                )
                messages.append(_assistant_message(response))
                if not response.tool_calls:
                    final_text = response.text or "模型未返回文本。"
                    completion = self.completion_contract.evaluate(
                        evidence_from_executions(execution_evidence)
                    )
                    completion_event = emit(
                        "completion_decided",
                        {
                            "step": step,
                            "contract": completion_contract_name,
                            "accepted": completion.accepted,
                            "reason": completion.reason,
                            "missing": list(completion.missing),
                            "feedback": completion.feedback,
                        },
                        parent_id=response_event.event_id,
                    )
                    if not completion.accepted:
                        messages.append(
                            {
                                "role": "user",
                                "content": completion.feedback,
                            }
                        )
                        turn_parent_id = completion_event.event_id
                        continue
                    finished_event = emit(
                        "run_finished",
                        {
                            "success": True,
                            "steps": step,
                            "text": _clip(final_text, self.max_observation_chars),
                            "duration_seconds": round(time.monotonic() - started_at, 3),
                            "completion_contract": completion_contract_name,
                        },
                        parent_id=completion_event.event_id,
                        usage=total_usage,
                    )
                    if continue_session:
                        self._conversation = deepcopy(messages)
                        self._session_parent_id = finished_event.event_id
                    else:
                        self._conversation = []
                    return RunResult(
                        run_id,
                        task,
                        final_text,
                        True,
                        step,
                        run_events,
                        session_id=self._session_id,
                    )

                for call in response.tool_calls:
                    proposal = emit(
                        "tool_proposed",
                        {"step": step, "call_id": call.id, "tool": call.name, "arguments": call.arguments},
                        parent_id=response_event.event_id,
                    )
                    context = PolicyContext(
                        workspace=self.workspace,
                        interactive=is_interactive,
                        auto_approve=self.auto_approve,
                        step=step,
                        max_steps=self.max_steps,
                    )
                    decision = self.policy.decide(call.name, call.arguments, context)
                    policy_event = emit(
                        "policy_decided",
                        {
                            "call_id": call.id,
                            "tool": call.name,
                            "decision": decision.decision.value,
                            "reason": decision.reason,
                            "rule": decision.rule,
                        },
                        parent_id=proposal.event_id,
                    )
                    if decision.decision is Decision.ASK:
                        emit(
                            "approval_requested",
                            {"call_id": call.id, "tool": call.name, "arguments": call.arguments},
                            parent_id=policy_event.event_id,
                        )
                        approved = self.auto_approve
                        approval_error: str | None = None
                        if not approved and is_interactive and approve is not None:
                            try:
                                approved = bool(approve(call.name, call.arguments, decision))
                            except Exception as exc:
                                approval_error = str(exc)
                                approved = False
                        if approved:
                            approval_reason = "人工确认通过"
                        elif approval_error:
                            approval_reason = f"确认流程失败: {approval_error}"
                        else:
                            approval_reason = "未获得人工确认"
                        decision = PolicyDecision(
                            Decision.ALLOW if approved else Decision.DENY,
                            approval_reason,
                            "approval",
                        )
                        policy_event = emit(
                            "policy_decided",
                            {
                                "call_id": call.id,
                                "tool": call.name,
                                "decision": decision.decision.value,
                                "reason": decision.reason,
                                "rule": decision.rule,
                            },
                            parent_id=policy_event.event_id,
                        )
                    if decision.decision is Decision.DENY:
                        result = ToolResult(
                            output=decision.reason,
                            success=False,
                            error=decision.reason,
                            metadata={"policy": "deny", "rule": decision.rule},
                        )
                        finished_tool = emit(
                            "tool_finished",
                            _tool_payload(call, result, step, denied=True),
                            parent_id=policy_event.event_id,
                        )
                    else:
                        started_tool = emit(
                            "tool_started",
                            {"step": step, "call_id": call.id, "tool": call.name, "arguments": call.arguments},
                            parent_id=policy_event.event_id,
                        )
                        try:
                            result = self.tools.invoke(call.name, self.executor, call.arguments)
                            result = _normalize_tool_result(result)
                        except Exception as exc:
                            result = ToolResult(output=f"工具异常: {exc}", success=False, error=str(exc))
                        finished_tool = emit(
                            "tool_finished",
                            _tool_payload(call, result, step),
                            parent_id=started_tool.event_id,
                        )
                        turn_parent_id = finished_tool.event_id
                        for changed in result.files_changed:
                            changed_event = emit(
                                "file_changed",
                                {"call_id": call.id, "tool": call.name, "path": changed},
                                parent_id=finished_tool.event_id,
                            )
                            if self.context_budget_chars is not None:
                                turn_parent_id = changed_event.event_id
                    if decision.decision is Decision.DENY:
                        turn_parent_id = finished_tool.event_id
                    execution_evidence.append(
                        ToolExecutionEvidence(
                            tool=call.name,
                            arguments=dict(call.arguments),
                            success=result.success,
                            files_changed=tuple(result.files_changed),
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": _clip(result.as_message(), self.max_observation_chars),
                        }
                    )

            return self._failed(
                emit,
                run_events,
                run_id,
                task,
                self.max_steps,
                f"达到最大步数 {self.max_steps}，模型仍未完成任务",
                continue_session=is_session_run,
                messages=messages,
            )
        except Exception as exc:
            return self._failed(
                emit,
                run_events,
                run_id,
                task,
                0,
                f"运行时异常: {exc}",
                continue_session=is_session_run,
                messages=messages,
            )

    def _failed(
        self,
        emit: Callable[..., Event],
        run_events: list[Event],
        run_id: str,
        task: str,
        steps: int,
        error: str,
        *,
        parent_id: str | None = None,
        continue_session: bool = False,
        messages: Sequence[Mapping[str, Any]] = (),
    ) -> RunResult:
        failed_event = emit(
            "run_failed",
            {"success": False, "steps": steps, "error": error},
            parent_id=parent_id,
        )
        if continue_session:
            self._conversation = deepcopy([dict(message) for message in messages])
            self._session_parent_id = failed_event.event_id
        else:
            self._conversation = []
        return RunResult(
            run_id,
            task,
            error,
            False,
            steps,
            run_events,
            error=error,
            session_id=self._session_id,
        )

    run_task = run


def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": response.text or ""}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in response.tool_calls
        ]
    return message


def _tool_payload(call: ToolCall, result: ToolResult, step: int, *, denied: bool = False) -> dict[str, Any]:
    return {
        "step": step,
        "call_id": call.id,
        "tool": call.name,
        "success": result.success,
        "output": _clip(result.output, 12_000),
        "error": result.error,
        "files_changed": list(result.files_changed),
        "metadata": result.metadata,
        "denied": denied,
    }


def _normalize_tool_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult(output=value)
    if isinstance(value, Mapping):
        return ToolResult(
            output=str(value.get("output", value.get("content", ""))),
            success=bool(value.get("success", True)),
            files_changed=list(value.get("files_changed", [])),
            metadata=dict(value.get("metadata", {})),
            error=value.get("error"),
        )
    return ToolResult(output=str(value))


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(limit - 24, 0)] + "…[已截断]"


def _trace_messages(messages: Sequence[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        copied = dict(message)
        if "content" in copied:
            copied["content"] = _clip(copied["content"], limit)
        result.append(_json_safe(copied))
    return result


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if isinstance(value, Mapping):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return str(value)


def _messages_fingerprint(messages: Sequence[Mapping[str, Any]]) -> str:
    """Stable identity used to avoid compacting an unchanged context twice."""

    return json.dumps(
        [dict(message) for message in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _merge_usage(total: dict[str, Any], current: Mapping[str, Any]) -> None:
    for key, value in current.items():
        previous = total.get(key)
        if (
            isinstance(previous, (int, float))
            and not isinstance(previous, bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            total[key] = previous + value
        elif previous is None:
            total[key] = value
        else:
            total[key] = value


def _default_trace_path(workspace: Path) -> Path:
    return workspace / ".xi" / "traces" / f"{uuid4().hex}.jsonl"


def _context_strategy_name(builder: ContextBuilder) -> str:
    strategy = getattr(builder, "strategy", None)
    if isinstance(strategy, str) and strategy.strip():
        return strategy.strip()
    return type(builder).__name__


def _completion_contract_name(contract: CompletionContract) -> str:
    name = getattr(contract, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return type(contract).__name__


def _executor_name(executor: ToolExecutor) -> str:
    name = getattr(executor, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return type(executor).__name__


__all__ = ["AgentRuntime", "RunResult"]
