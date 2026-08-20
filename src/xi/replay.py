"""安全、只读的 Xi JSONL trace 回放。

Replay 只把已经存在的事件投影成一条人类可读的时间线。它不创建
``AgentRuntime``，不加载模型，不实例化执行器，也不会向 trace 或工作区写入
任何内容；因此它和 resume/重新执行是有意不同的能力。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .events import EVENT_SCHEMA_VERSION, LEGACY_EVENT_SCHEMA_VERSION, Event


class ReplayError(ValueError):
    """Raised when a JSONL trace cannot be safely replayed."""


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    """Session-level summary with ``run_id`` pointing at the latest Run."""

    run_id: str
    task: str
    status: str
    steps: int
    duration_seconds: float | None
    tool_calls: dict[str, int]
    policy_decisions: dict[str, int]
    changed_files: tuple[str, ...]
    events: int
    final_text: str = ""
    error: str | None = None
    session_id: str = ""
    run_ids: tuple[str, ...] = ()
    run_count: int = 0

    @property
    def runs(self) -> int:
        return self.run_count or len(self.run_ids) or 1


@dataclass(frozen=True, slots=True)
class ReplayTrace:
    """Validated events plus the derived summary used by the CLI renderer."""

    source: Path
    events: tuple[Event, ...]
    summary: ReplaySummary

    def render(self, *, max_text: int = 180) -> str:
        return render_trace(self, max_text=max_text)


_SECRET_KEY_RE = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|authorization|bearer|token|secret|password|credential)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:[a-z0-9]+[_-])*(?:api[_ -]?key|access[_ -]?token|authorization|token|secret|password|credential)\b"
    r"\s*[:=]\s*)([\"']?)([^\"'\s,;}]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_KEY_RE = re.compile(r"\b(?:sk|xai|rk|pk)-[A-Za-z0-9_-]{8,}\b")

_CONTENT_KEYS = frozenset({"content", "patch", "diff", "messages", "output", "text"})
_DISPLAY_KEYS = frozenset(
    {"path", "cwd", "command", "query", "regex", "max_results", "timeout_seconds"}
)


def load_trace(path: str | Path) -> ReplayTrace:
    """Read and validate one JSONL trace without performing side effects."""

    source = Path(path).expanduser()
    if not source.exists():
        raise ReplayError(f"trace 文件不存在: {source}")
    if not source.is_file():
        raise ReplayError(f"trace 路径不是普通文件: {source}")
    try:
        source = source.resolve()
    except OSError as exc:
        raise ReplayError(f"无法解析 trace 路径: {exc}") from exc

    events: list[Event] = []
    seen_event_ids: set[str] = set()
    session_id: str | None = None
    run_ids: list[str] = []
    seen_run_ids: set[str] = set()
    try:
        handle = source.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise ReplayError(f"无法读取 trace 文件: {exc}") from exc

    try:
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise ReplayError(f"第 {line_number} 行为空；JSONL 每行必须是合法 JSON")
                try:
                    raw = json.loads(line, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ReplayError(f"第 {line_number} 行不是合法 JSON: {exc}") from exc
                if not isinstance(raw, Mapping):
                    raise ReplayError(f"第 {line_number} 行必须是 JSON 对象")
                event = _parse_event(raw, line_number)
                if event.event_id in seen_event_ids:
                    raise ReplayError(
                        f"第 {line_number} 行 event_id 重复: {event.event_id}"
                    )
                if event.parent_id:
                    if event.parent_id not in seen_event_ids:
                        raise ReplayError(
                            f"第 {line_number} 行 parent_id 未指向此前事件: {event.parent_id}"
                        )
                if session_id is None:
                    session_id = event.session_id
                elif event.session_id != session_id:
                    raise ReplayError(
                        f"第 {line_number} 行包含多个 session_id；"
                        f"已有 {session_id}，发现 {event.session_id}"
                    )
                if event.run_id not in seen_run_ids:
                    seen_run_ids.add(event.run_id)
                    run_ids.append(event.run_id)
                seen_event_ids.add(event.event_id)
                events.append(event)
    except UnicodeDecodeError as exc:
        raise ReplayError(f"trace 不是 UTF-8 文本，无法读取: {exc}") from exc
    except OSError as exc:
        raise ReplayError(f"读取 trace 时发生 I/O 错误: {exc}") from exc

    if not events or session_id is None or not run_ids:
        raise ReplayError("trace 为空；至少需要一条事件")
    summary = _summarize(events, session_id, tuple(run_ids))
    return ReplayTrace(source=source, events=tuple(events), summary=summary)


def replay_trace(path: str | Path, *, max_text: int = 180) -> str:
    """Load, validate, and render a trace in one read-only operation."""

    return load_trace(path).render(max_text=max_text)


def render_trace(trace: ReplayTrace, *, max_text: int = 180) -> str:
    """Render a compact timeline and derived run summary."""

    limit = max(int(max_text), 40)
    summary = trace.summary
    lines = [
        "Xi Trace Replay（离线只读：不会调用模型、工具或命令）",
        f"trace: {_clip(str(trace.source), limit)}",
        f"session_id: {_clip(summary.session_id, limit)}",
        f"runs: {summary.runs}",
        f"run_ids: {_format_run_ids(summary.run_ids, limit)}",
        f"latest_run_id: {_clip(summary.run_id, limit)}",
        f"latest_task: {_clip(summary.task, limit)}",
        "",
        "时间线:",
    ]
    first_time = _parse_timestamp(trace.events[0].timestamp)
    for event in trace.events:
        offset = _relative_time(first_time, event.timestamp)
        lines.append(f"{offset:>10} {_event_line(event, limit)}")

    lines.extend(
        [
            "",
            "摘要:",
            f"  最终状态: {summary.status}",
            f"  步骤: {summary.steps}",
            f"  耗时: {_format_duration(summary.duration_seconds)}",
            f"  事件数: {summary.events}",
            f"  工具调用: {_format_counts(summary.tool_calls)}",
            f"  策略决策: {_format_counts(summary.policy_decisions)}",
            f"  改动文件: {_format_files(summary.changed_files, limit)}",
        ]
    )
    if summary.final_text:
        lines.append(f"  最终消息: {_clip(summary.final_text, limit)}")
    if summary.error:
        lines.append(f"  错误: {_clip(summary.error, limit)}")
    return "\n".join(lines)


def _parse_event(raw: Mapping[str, Any], line_number: int) -> Event:
    data = dict(raw)
    event_type = data.get("type")
    legacy_type = data.get("event_type")
    if event_type is None:
        event_type = legacy_type
    elif legacy_type is not None and legacy_type != event_type:
        raise ReplayError(f"第 {line_number} 行 type 与 event_type 不一致")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ReplayError(f"第 {line_number} 行缺少有效 type")
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ReplayError(f"第 {line_number} 行缺少有效 event_id")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ReplayError(f"第 {line_number} 行缺少有效 run_id")
    schema_version = data.get("schema_version", LEGACY_EVENT_SCHEMA_VERSION)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ReplayError(f"第 {line_number} 行 schema_version 必须是整数")
    if schema_version not in {LEGACY_EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION}:
        raise ReplayError(
            f"第 {line_number} 行 schema_version={schema_version} 不受支持"
        )
    session_id = data.get("session_id")
    if schema_version >= EVENT_SCHEMA_VERSION:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ReplayError(f"第 {line_number} 行 v2 事件缺少有效 session_id")
    elif session_id is None:
        session_id = run_id
    elif not isinstance(session_id, str) or not session_id.strip():
        raise ReplayError(f"第 {line_number} 行 session_id 必须是非空字符串")
    parent_id = data.get("parent_id")
    if parent_id == "":
        parent_id = None
    if parent_id is not None and not isinstance(parent_id, str):
        raise ReplayError(f"第 {line_number} 行 parent_id 必须是字符串或 null")
    payload = data.get("payload", {})
    if not isinstance(payload, Mapping):
        raise ReplayError(f"第 {line_number} 行 payload 必须是 JSON 对象")
    timestamp = data.get("timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        raise ReplayError(f"第 {line_number} 行 timestamp 必须是字符串")
    usage = data.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        raise ReplayError(f"第 {line_number} 行 usage 必须是对象或 null")

    normalized = dict(data)
    normalized["type"] = event_type
    normalized["event_id"] = event_id
    normalized["run_id"] = run_id
    normalized["session_id"] = session_id
    normalized["schema_version"] = schema_version
    normalized["parent_id"] = parent_id
    normalized["payload"] = _redact_value(dict(payload))
    # Event.from_dict() otherwise fills a missing timestamp with "now", which
    # would make an offline replay nondeterministic and could invent a duration.
    normalized["timestamp"] = timestamp or ""
    if usage is not None:
        normalized["usage"] = _redact_value(dict(usage))
    try:
        return Event.from_dict(normalized)
    except (TypeError, ValueError) as exc:
        raise ReplayError(f"第 {line_number} 行事件字段无效: {exc}") from exc


def _summarize(
    events: list[Event],
    session_id: str,
    run_ids: tuple[str, ...],
) -> ReplaySummary:
    tool_calls: Counter[str] = Counter()
    policy_decisions: Counter[str] = Counter()
    changed_files: list[str] = []
    changed_seen: set[str] = set()
    task = ""
    status = "incomplete"
    run_steps: list[int] = []
    run_durations: list[float] = []
    duration: float | None = None
    final_text = ""
    error: str | None = None

    for event in events:
        payload = event.payload
        if not isinstance(payload, Mapping):
            continue
        if event.type == "run_started":
            run_steps.append(0)
            event_task = _as_text(payload.get("task"))
            if event_task:
                task = event_task
            status = "incomplete"
            final_text = ""
            error = None
        step_value = _as_nonnegative_int(payload.get("step"))
        if step_value is not None:
            if not run_steps:
                run_steps.append(0)
            run_steps[-1] = max(run_steps[-1], step_value)
        if event.type == "tool_proposed":
            tool_name = _as_text(payload.get("tool")) or "<unknown>"
            tool_calls[tool_name] += 1
        elif event.type == "policy_decided":
            decision = _as_text(payload.get("decision")) or "<unknown>"
            policy_decisions[decision] += 1
        elif event.type == "file_changed":
            path = _as_text(payload.get("path"))
            if path and path not in changed_seen:
                changed_seen.add(path)
                changed_files.append(path)
        elif event.type == "run_finished":
            status = "success" if payload.get("success", True) else "failed"
            if not run_steps:
                run_steps.append(0)
            run_steps[-1] = max(
                run_steps[-1], _as_nonnegative_int(payload.get("steps")) or 0
            )
            event_duration = _as_nonnegative_float(payload.get("duration_seconds"))
            if event_duration is not None:
                run_durations.append(event_duration)
            final_text = _as_text(payload.get("text"))
            error = None
        elif event.type == "run_failed":
            status = "failed"
            if not run_steps:
                run_steps.append(0)
            run_steps[-1] = max(
                run_steps[-1], _as_nonnegative_int(payload.get("steps")) or 0
            )
            error = _as_text(payload.get("error")) or None
        elif event.type == "model_responded" and not final_text:
            # Useful for incomplete traces that ended after a model response.
            final_text = _as_text(payload.get("text"))

    steps = sum(run_steps)
    run_count = len(run_steps) or 1
    if run_durations and len(run_durations) == run_count:
        duration = sum(run_durations)
    elif run_count == 1:
        start = _parse_timestamp(events[0].timestamp)
        end = _parse_timestamp(events[-1].timestamp)
        if start is not None and end is not None:
            try:
                duration = max((end - start).total_seconds(), 0.0)
            except TypeError:
                # Mixed offset-aware and naive timestamps are still displayable,
                # but cannot produce a trustworthy elapsed duration.
                duration = None
    return ReplaySummary(
        run_id=run_ids[-1],
        task=task,
        status=status,
        steps=steps,
        duration_seconds=duration,
        tool_calls=dict(tool_calls),
        policy_decisions=dict(policy_decisions),
        changed_files=tuple(changed_files),
        events=len(events),
        final_text=final_text,
        error=error,
        session_id=session_id,
        run_ids=run_ids,
        run_count=run_count,
    )


def _event_line(event: Event, limit: int) -> str:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    event_type = event.type
    if event_type == "run_started":
        parent = _clip(event.parent_id or "-", 16)
        return (
            f"[run_started] run_id={_clip(event.run_id, 16)} "
            f"parent={parent} task={_clip(_as_text(payload.get('task')), limit)}"
        )
    if event_type == "model_requested":
        tools = payload.get("tools") or []
        tool_text = ",".join(_clip(_as_text(item), 40) for item in tools) or "-"
        return (
            f"[model_requested] step={_as_text(payload.get('step')) or '?'} "
            f"messages={_as_text(payload.get('message_count')) or '?'} tools={tool_text}"
        )
    if event_type == "model_responded":
        calls = payload.get("tool_calls") or []
        names = []
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, Mapping):
                    names.append(_as_text(call.get("name")) or "<unknown>")
        detail = f"tool_calls={','.join(names)}" if names else "tool_calls=0"
        text = _clip(_as_text(payload.get("text")), limit)
        return f"[model_responded] step={_as_text(payload.get('step')) or '?'} {detail}" + (
            f" text={text}" if text else ""
        )
    if event_type == "tool_proposed":
        return (
            f"[tool_proposed] step={_as_text(payload.get('step')) or '?'} "
            f"tool={_as_text(payload.get('tool')) or '<unknown>'} "
            f"args={_format_args(payload.get('arguments'), limit)}"
        )
    if event_type == "policy_decided":
        return (
            f"[policy_decided] tool={_as_text(payload.get('tool')) or '<unknown>'} "
            f"decision={_as_text(payload.get('decision')) or '<unknown>'} "
            f"reason={_clip(_as_text(payload.get('reason')), limit)}"
        )
    if event_type == "tool_started":
        return (
            f"[tool_started] tool={_as_text(payload.get('tool')) or '<unknown>'} "
            f"args={_format_args(payload.get('arguments'), limit)}"
        )
    if event_type == "tool_finished":
        success = "ok" if payload.get("success") else "failed"
        output = _clip(_as_text(payload.get("output")), limit)
        error = _clip(_as_text(payload.get("error")), limit)
        detail = output or error
        return (
            f"[tool_finished] tool={_as_text(payload.get('tool')) or '<unknown>'} "
            f"status={success}" + (f" detail={detail}" if detail else "")
        )
    if event_type == "file_changed":
        path = _clip(_as_text(payload.get("path")), limit) or "<unknown>"
        return f"[file_changed] path={path}"
    if event_type == "completion_decided":
        accepted = "accepted" if payload.get("accepted") else "rejected"
        missing = payload.get("missing") or []
        detail = ", ".join(str(item) for item in missing) if isinstance(missing, list) else ""
        return (
            f"[completion_decided] status={accepted} "
            f"contract={_as_text(payload.get('contract')) or '<unknown>'}"
            + (f" missing={_clip(detail, limit)}" if detail else "")
        )
    if event_type == "run_finished":
        return (
            f"[run_finished] status={'success' if payload.get('success', True) else 'failed'} "
            f"steps={_as_text(payload.get('steps')) or '?'}"
        )
    if event_type == "run_failed":
        return (
            f"[run_failed] steps={_as_text(payload.get('steps')) or '?'} "
            f"error={_clip(_as_text(payload.get('error')), limit)}"
        )
    return f"[{event_type}]"


def _format_args(value: Any, limit: int) -> str:
    if not isinstance(value, Mapping):
        return "{}"
    parts: list[str] = []
    for key, raw_value in value.items():
        key_text = str(key)
        if _SECRET_KEY_RE.search(key_text):
            shown = "<redacted>"
        elif key_text in _CONTENT_KEYS:
            shown = f"<{_value_size(raw_value)} chars>"
        elif key_text in _DISPLAY_KEYS:
            shown = _clip(_as_text(raw_value), max(40, min(limit, 100)))
        elif isinstance(raw_value, (Mapping, list, tuple)):
            shown = f"<{_value_size(raw_value)} items>"
        else:
            shown = _clip(_as_text(raw_value), 60)
        parts.append(f"{key_text}={shown}")
    return "{" + ", ".join(parts) + "}"


def _format_counts(values: Mapping[str, int]) -> str:
    if not values:
        return "0"
    return ", ".join(f"{key}={value}" for key, value in values.items())


def _format_run_ids(run_ids: tuple[str, ...], limit: int) -> str:
    if not run_ids:
        return "无"
    return _clip(" -> ".join(run_ids), max(limit, 100))


def _format_files(files: tuple[str, ...], limit: int) -> str:
    if not files:
        return "无"
    shown = ", ".join(_clip(path, min(limit, 100)) for path in files[:8])
    if len(files) > 8:
        shown += f", …（另有 {len(files) - 8} 个）"
    return _clip(shown, max(limit, 100))


def _format_duration(value: float | None) -> str:
    return "未知" if value is None else f"{value:.3f}s"


def _value_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (Mapping, list, tuple)):
        return len(value)
    return len(str(value))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (Mapping, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _as_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _as_nonnegative_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _relative_time(first: datetime | None, current: Any) -> str:
    if first is None:
        return "[--]"
    parsed = _parse_timestamp(current)
    if parsed is None:
        return "[--]"
    try:
        seconds = max((parsed - first).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return "[--]"
    return f"[{seconds:8.3f}s]"


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = "<redacted>" if _SECRET_KEY_RE.search(key_text) else _redact_value(item)
        return result
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    # Redact bearer credentials first; otherwise an Authorization assignment
    # could consume only the word "Bearer" and leave the credential behind.
    redacted = _BEARER_RE.sub("Bearer <redacted>", value)
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", redacted)
    return _KNOWN_KEY_RE.sub("<redacted>", redacted)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"不允许 JSON 常量 {value}")


def _clip(value: str, limit: int) -> str:
    value = _redact_text(value).replace("\r", " ").replace("\n", "\\n")
    if len(value) <= limit:
        return value
    marker = "…"
    return value[: max(limit - len(marker), 0)] + marker


__all__ = [
    "ReplayError",
    "ReplaySummary",
    "ReplayTrace",
    "load_trace",
    "replay_trace",
    "render_trace",
]
