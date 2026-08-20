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
from typing import Any, Mapping

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
class SessionProjection:
    """Minimal durable state required to resume or fork a Xi Session.

    Resume uses ``last_event_id`` as the next ``run_started`` parent in the
    same Trace. Fork records it as the source endpoint but deliberately starts
    the new Trace with no parent.
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


def project_session(
    path: str | Path,
    *,
    at_event_id: str | None = None,
) -> SessionProjection:
    """Project a successful Run endpoint into reusable model context.

    The most recent ``model_requested`` event already contains the canonical
    conversation sent to the model. Projection restores that snapshot and
    folds in the response that completed the selected turn. ``at_event_id``
    limits projection to that event and everything before it, which lets Fork
    inherit historical context without seeing later Runs in the source Trace.
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
    if (
        final_event.type != "run_finished"
        or final_event.payload.get("success", True) is not True
    ):
        raise SessionProjectionError("目标事件必须是成功的 run_finished")

    started_events = [event for event in selected_events if event.type == "run_started"]
    if not started_events:
        raise SessionProjectionError("trace 缺少 run_started，无法确定工作区")

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

    request_index = -1
    raw_messages: Any = None
    for index, event in enumerate(selected_events):
        if (
            event.run_id == final_event.run_id
            and event.type == "model_requested"
            and isinstance(event.payload.get("messages"), list)
        ):
            request_index = index
            raw_messages = event.payload["messages"]
    if request_index < 0 or raw_messages is None:
        raise SessionProjectionError(
            "trace 未保存 model_requested.messages，无法恢复模型上下文"
        )

    messages = _normalize_messages(raw_messages)
    response_seen = False
    for event in selected_events[request_index + 1 :]:
        if event.run_id != final_event.run_id:
            continue
        payload = event.payload
        if event.type == "model_responded" and not response_seen:
            messages.append(_assistant_message(payload))
            response_seen = True
        elif event.type == "tool_finished" and response_seen:
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _tool_message(payload),
                    }
                )
        elif event.type == "completion_decided" and payload.get("accepted") is False:
            feedback = payload.get("feedback")
            if not isinstance(feedback, str) or not feedback:
                feedback = _completion_feedback(payload.get("missing"))
            messages.append({"role": "user", "content": feedback})

    if not response_seen:
        raise SessionProjectionError("trace 缺少完成当前轮次的 model_responded 事件")

    target_started_events = [
        event for event in started_events if event.run_id == final_event.run_id
    ]
    if not target_started_events:
        raise SessionProjectionError("目标 Run 缺少 run_started")
    latest_started = target_started_events[-1].payload
    context_strategy = latest_started.get("context_strategy", "search")
    if not isinstance(context_strategy, str) or not context_strategy.strip():
        context_strategy = "search"
    return SessionProjection(
        source=trace.source,
        session_id=final_event.session_id or final_event.run_id,
        run_id=final_event.run_id,
        workspace=workspace,
        messages=tuple(deepcopy(messages)),
        last_event_id=final_event.event_id,
        context_strategy=context_strategy.strip(),
        turns=len(started_events),
    )


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
    "SessionProjection",
    "SessionProjectionError",
    "project_session",
]
