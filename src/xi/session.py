"""Project durable Xi events back into resumable model conversations.

The JSONL event stream is the durable source of truth. Callers use the small
``project_session`` interface instead of knowing which runtime events contain
the latest model context or how tool results are represented as chat messages.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal, Mapping

from .events import (
    Event,
    EventCollection,
    JsonlSessionStore,
    JsonlStore,
    MemorySessionStore,
    MemoryStore,
    SessionStore,
)
from .replay import ReplayError, load_trace


class SessionProjectionError(ValueError):
    """Raised when a trace cannot be projected into reusable Session state."""


@dataclass(frozen=True, slots=True)
class RecoveryPoint:
    """A safe persisted checkpoint for a failed or incomplete Run."""

    state: Literal["failed", "incomplete"]
    checkpoint_event_id: str


@dataclass(frozen=True, slots=True)
class SessionProjection:
    """Minimal durable state required to resume, recover, or fork a Session.

    Resume uses ``last_event_id`` as the next ``run_started`` parent in the
    same Trace. Recovery does the same from a safe checkpoint and records its
    source state. Fork records the endpoint but starts a new Trace with no
    parent.
    """

    source: Path
    # The most recent Run already present in the Trace. A resumed turn uses a
    # new run_id while keeping the Session identity stable.
    run_id: str
    workspace: Path
    messages: tuple[dict[str, Any], ...]
    last_event_id: str
    context_strategy: str
    turns: int
    # Kept at the end with a default so existing positional construction of
    # the public projection object remains compatible.
    session_id: str = ""
    recovery: RecoveryPoint | None = None
    context_budget_chars: int | None = None


def project_session(
    path: str | Path,
    *,
    at_event_id: str | None = None,
) -> SessionProjection:
    """Project a resumable or recoverable Run into reusable model context.

    The most recent ``model_requested`` or ``context_compacted`` event contains
    a canonical model conversation snapshot. Projection restores the later
    checkpoint and folds in any complete response/tool exchange persisted
    after it.
    ``at_event_id`` remains the strict Fork path and accepts only a successful
    ``run_finished`` endpoint. Without it, a failed or safely incomplete tail
    is projected as Recovery state.
    """

    try:
        trace = load_trace(path)
    except ReplayError as exc:
        raise SessionProjectionError(str(exc)) from exc

    target_index = len(trace.events) - 1
    if at_event_id is not None:
        target_index = next(
            (
                index
                for index, event in enumerate(trace.events)
                if event.event_id == at_event_id
            ),
            -1,
        )
        if target_index < 0:
            raise SessionProjectionError(f"trace 不包含指定 event_id: {at_event_id}")
    selected_events = trace.events[: target_index + 1]
    final_event = selected_events[-1]
    recovery_state = _classify_endpoint(
        selected_events,
        final_event,
        explicit_event=at_event_id is not None,
    )
    if recovery_state is not None:
        _ensure_no_unfinished_tool_execution(selected_events)

    started_events = [event for event in selected_events if event.type == "run_started"]
    if not started_events:
        raise SessionProjectionError("trace 缺少 run_started，无法确定工作区")
    if started_events[-1].run_id != final_event.run_id:
        raise SessionProjectionError("trace 尾部不属于最新 Run，无法安全投影")

    workspaces: list[Path] = []
    for event in started_events:
        raw_workspace = event.payload.get("workspace")
        if not isinstance(raw_workspace, str) or not raw_workspace.strip():
            raise SessionProjectionError("run_started 缺少有效 workspace")
        workspaces.append(Path(raw_workspace).expanduser().resolve())
    workspace = workspaces[0]
    if any(candidate != workspace for candidate in workspaces[1:]):
        raise SessionProjectionError("同一 session trace 包含多个工作区，拒绝恢复")
    if not workspace.exists() or not workspace.is_dir():
        raise SessionProjectionError(f"trace 记录的工作区不是目录: {workspace}")

    checkpoint_index = -1
    checkpoint_event: Event | None = None
    for index, event in enumerate(selected_events):
        if event.run_id != final_event.run_id:
            continue
        if event.type not in {"model_requested", "context_compacted"}:
            continue
        if not isinstance(event.payload.get("messages"), list):
            continue
        checkpoint_index = index
        checkpoint_event = event
    if checkpoint_index < 0 or checkpoint_event is None:
        raise SessionProjectionError(
            "trace 未保存可用的模型上下文检查点，无法恢复模型上下文"
        )

    messages = _project_messages_from_checkpoint(
        selected_events,
        checkpoint_index=checkpoint_index,
        checkpoint_event=checkpoint_event,
        run_id=final_event.run_id,
        require_response=recovery_state is None,
    )

    target_started_events = [
        event for event in started_events if event.run_id == final_event.run_id
    ]
    if not target_started_events:
        raise SessionProjectionError("目标 Run 缺少 run_started")
    latest_started = target_started_events[-1].payload
    context_strategy = latest_started.get("context_strategy", "search")
    if not isinstance(context_strategy, str) or not context_strategy.strip():
        context_strategy = "search"
    raw_budget = latest_started.get("context_budget_chars")
    try:
        context_budget_chars = (
            None
            if raw_budget is None
            else int(raw_budget)
        )
    except (TypeError, ValueError):
        context_budget_chars = None
    if context_budget_chars is not None and context_budget_chars <= 0:
        context_budget_chars = None
    return SessionProjection(
        source=trace.source,
        session_id=final_event.session_id or final_event.run_id,
        run_id=final_event.run_id,
        workspace=workspace,
        messages=tuple(deepcopy(messages)),
        last_event_id=final_event.event_id,
        context_strategy=context_strategy.strip(),
        turns=len(started_events),
        recovery=(
            RecoveryPoint(
                state=recovery_state,
                checkpoint_event_id=checkpoint_event.event_id,
            )
            if recovery_state is not None
            else None
        ),
        context_budget_chars=context_budget_chars,
    )


def _classify_endpoint(
    events: tuple[Event, ...],
    final_event: Event,
    *,
    explicit_event: bool,
) -> Literal["failed", "incomplete"] | None:
    if final_event.type == "run_finished":
        if final_event.payload.get("success", True) is True:
            return None
        raise SessionProjectionError("目标 run_finished 未成功，无法安全续接")
    if explicit_event:
        raise SessionProjectionError("目标事件必须是成功的 run_finished")

    target_run_events = [event for event in events if event.run_id == final_event.run_id]
    earlier_terminal = any(
        event.type in {"run_finished", "run_failed"}
        for event in target_run_events[:-1]
    )
    if earlier_terminal:
        raise SessionProjectionError("最新 Run 的终止事件后仍有事件，无法安全恢复")
    if final_event.type == "run_failed":
        return "failed"
    return "incomplete"


def _ensure_no_unfinished_tool_execution(events: tuple[Event, ...]) -> None:
    started: dict[str, Event] = {}
    for event in events:
        if event.type == "tool_started":
            started[event.event_id] = event

    finished_started_ids: set[str] = set()
    for event in events:
        if event.type != "tool_finished" or event.parent_id not in started:
            continue
        started_event = started[event.parent_id]
        started_call_id = started_event.payload.get("call_id")
        finished_call_id = event.payload.get("call_id")
        if (
            event.run_id != started_event.run_id
            or not isinstance(started_call_id, str)
            or not started_call_id
            or finished_call_id != started_call_id
        ):
            raise SessionProjectionError(
                "tool_started 与 tool_finished 的 Run 或 call_id 不一致，拒绝恢复"
            )
        if event.parent_id in finished_started_ids:
            raise SessionProjectionError(
                f"同一 tool_started 对应多个 tool_finished: {event.parent_id}"
            )
        finished_started_ids.add(event.parent_id)

    unresolved = [
        event
        for event_id, event in started.items()
        if event_id not in finished_started_ids
    ]
    if not unresolved:
        return
    event = unresolved[0]
    call_id = event.payload.get("call_id")
    call_text = call_id if isinstance(call_id, str) and call_id else "<unknown>"
    raise SessionProjectionError(
        "检测到 tool_started 缺少对应 tool_finished，工具副作用状态不确定；"
        f"拒绝恢复 call_id={call_text} event_id={event.event_id}"
    )


def _project_messages_from_checkpoint(
    events: tuple[Event, ...],
    *,
    checkpoint_index: int,
    checkpoint_event: Event,
    run_id: str,
    require_response: bool,
) -> list[dict[str, Any]]:
    raw_messages = checkpoint_event.payload.get("messages")
    if not isinstance(raw_messages, list):
        raise SessionProjectionError(
            "安全检查点缺少完整 messages，无法恢复模型上下文"
        )
    messages = _normalize_messages(raw_messages)
    tail = [event for event in events[checkpoint_index + 1 :] if event.run_id == run_id]
    # A compaction checkpoint is itself a complete model snapshot.  Runtime
    # emits it immediately before the next request, so an interruption here
    # has no response/tool suffix to reconstruct.  If a malformed trace does
    # contain a suffix, only accept a complete single response exchange.
    if checkpoint_event.type == "context_compacted" and not tail:
        if require_response:
            raise SessionProjectionError(
                "成功 Run 的压缩检查点后缺少 model_responded，无法恢复"
            )
        return messages
    responses = [event for event in tail if event.type == "model_responded"]
    if len(responses) > 1:
        raise SessionProjectionError("安全检查点之后包含多个 model_responded，无法确定上下文")
    if not responses:
        if any(
            event.type
            in {
                "tool_proposed",
                "policy_decided",
                "approval_requested",
                "tool_started",
                "tool_finished",
                "file_changed",
            }
            for event in tail
        ):
            raise SessionProjectionError(
                "安全检查点后的工具事件缺少对应 model_responded，无法恢复上下文"
            )
        if require_response:
            raise SessionProjectionError("trace 缺少完成当前轮次的 model_responded 事件")
        return messages

    response = responses[0]
    response_index = tail.index(response)
    if any(
        event.type
        in {
            "tool_proposed",
            "policy_decided",
            "approval_requested",
            "tool_started",
            "tool_finished",
            "file_changed",
            "completion_decided",
        }
        for event in tail[:response_index]
    ):
        raise SessionProjectionError("model_responded 之前出现了无法归属的工具或完成事件")
    after_response = tail[response_index + 1 :]
    call_ids = _response_tool_call_ids(response.payload)
    finished_by_call: dict[str, Event] = {}
    for event in after_response:
        if event.type != "tool_finished":
            continue
        call_id = event.payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise SessionProjectionError("tool_finished 缺少有效 call_id，无法恢复上下文")
        if call_id in finished_by_call:
            raise SessionProjectionError(f"tool_finished call_id 重复: {call_id}")
        finished_by_call[call_id] = event

    finished_event_ids = {event.event_id for event in finished_by_call.values()}
    if any(
        event.type == "file_changed" and event.parent_id not in finished_event_ids
        for event in after_response
    ):
        raise SessionProjectionError("file_changed 未指向已完成的 tool_finished，拒绝恢复")

    unexpected_finished = set(finished_by_call).difference(call_ids)
    if unexpected_finished:
        call_id = sorted(unexpected_finished)[0]
        raise SessionProjectionError(
            f"tool_finished 未对应安全检查点后的 assistant tool_call: {call_id}"
        )

    if call_ids:
        completed = [call_id for call_id in call_ids if call_id in finished_by_call]
        if not completed:
            # The assistant proposed tools, but no execution completed and the
            # unfinished-start check proved that no side effect is in flight.
            # Roll back to the persisted request snapshot instead of inventing
            # tool results or replaying the proposal.
            return messages
        if len(completed) != len(call_ids):
            raise SessionProjectionError(
                "同一 assistant 响应的工具调用仅部分完成；"
                "无法在保留已完成副作用的同时构造合法模型上下文"
            )
        messages.append(_assistant_message(response.payload))
        for call_id in call_ids:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _tool_message(finished_by_call[call_id].payload),
                }
            )
    else:
        if finished_by_call:
            raise SessionProjectionError("assistant 未声明工具调用，但 trace 包含 tool_finished")
        messages.append(_assistant_message(response.payload))

    for event in after_response:
        payload = event.payload
        if event.type != "completion_decided" or payload.get("accepted") is not False:
            continue
        feedback = payload.get("feedback")
        if not isinstance(feedback, str) or not feedback:
            feedback = _completion_feedback(payload.get("missing"))
        messages.append({"role": "user", "content": feedback})
    return messages


def _response_tool_call_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw_calls = payload.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise SessionProjectionError("model_responded.tool_calls 必须是列表")
    call_ids: list[str] = []
    for index, item in enumerate(raw_calls, start=1):
        if not isinstance(item, Mapping):
            raise SessionProjectionError(f"tool_calls 第 {index} 项不是对象")
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise SessionProjectionError(f"tool_calls 第 {index} 项缺少有效 id")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise SessionProjectionError(f"tool_calls 第 {index} 项缺少有效 name")
        arguments = item.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise SessionProjectionError(f"tool_calls 第 {index} 项 arguments 不是对象")
        if call_id in call_ids:
            raise SessionProjectionError(f"assistant tool_call id 重复: {call_id}")
        call_ids.append(call_id)
    return tuple(call_ids)


def _normalize_messages(value: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise SessionProjectionError(f"messages 第 {index} 项不是对象")
        message = deepcopy(dict(item))
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise SessionProjectionError(f"messages 第 {index} 项缺少有效 role")
        try:
            json.dumps(message, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise SessionProjectionError(f"messages 第 {index} 项不是合法 JSON 数据") from exc
        messages.append(message)
    if not messages:
        raise SessionProjectionError("trace 中的模型上下文为空")
    return messages


def _assistant_message(payload: Mapping[str, Any]) -> dict[str, Any]:
    text = payload.get("text", "")
    message: dict[str, Any] = {
        "role": "assistant",
        "content": text if isinstance(text, str) else str(text),
    }
    raw_calls = payload.get("tool_calls")
    if isinstance(raw_calls, list) and raw_calls:
        calls: list[dict[str, Any]] = []
        for item in raw_calls:
            if not isinstance(item, Mapping):
                continue
            call_id = item.get("id")
            name = item.get("name")
            arguments = item.get("arguments", {})
            if not isinstance(call_id, str) or not call_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )
        if calls:
            message["tool_calls"] = calls
    return message


def _tool_message(payload: Mapping[str, Any]) -> str:
    output = payload.get("output")
    output_text = output if isinstance(output, str) else str(output or "")
    if payload.get("success"):
        return output_text
    error = payload.get("error")
    error_text = error if isinstance(error, str) else str(error or "")
    prefix = "工具执行失败"
    if error_text and error_text != output_text:
        prefix += f"（{error_text}）"
    if output_text and output_text != error_text:
        return f"{prefix}:\n{output_text}"
    return prefix


def _completion_feedback(value: Any) -> str:
    missing = [str(item) for item in value] if isinstance(value, list) else []
    return (
        "完成契约未满足，当前回答不会结束任务。\n"
        "缺少证据：\n"
        + "\n".join(f"- {item}" for item in missing)
        + "\n请继续使用工具完成缺失项；只有实际执行并成功后再给出最终总结。"
    )


__all__ = [
    "Event",
    "EventCollection",
    "SessionStore",
    "MemorySessionStore",
    "JsonlSessionStore",
    "MemoryStore",
    "JsonlStore",
    "RecoveryPoint",
    "SessionProjection",
    "SessionProjectionError",
    "project_session",
]
