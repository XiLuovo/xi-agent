"""Deterministic, local conversation compaction for Xi.

The module deliberately exposes one small seam: a caller supplies a sequence
of chat messages and a character budget and receives a legal, self-contained
message sequence.  No model, filesystem, or external service is involved.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol, Sequence


class CompactionError(ValueError):
    """Raised when a legal context cannot be represented within the budget."""


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """A deterministic compaction snapshot and its auditable measurements."""

    messages: tuple[dict[str, Any], ...]
    before_message_count: int
    after_message_count: int
    before_characters: int
    after_characters: int
    dropped_message_count: int
    summary: str
    strategy: str = "deterministic-v1"


class ContextCompactor(Protocol):
    """Small seam used by :class:`xi.runtime.AgentRuntime`."""

    def compact(
        self,
        messages: Sequence[Mapping[str, Any]],
        budget_chars: int,
    ) -> CompactionResult:
        ...


@dataclass(frozen=True, slots=True)
class _MessageGroup:
    index: int
    messages: tuple[dict[str, Any], ...]

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(str(message.get("role", "")) for message in self.messages)


class DeterministicCompactor:
    """Keep system intent and a recent legal tail, summarize older groups."""

    strategy = "deterministic-v1"

    def compact(
        self,
        messages: Sequence[Mapping[str, Any]],
        budget_chars: int,
    ) -> CompactionResult:
        budget = _validate_budget(budget_chars)
        normalized = _normalize_messages(messages)
        groups = _group_messages(normalized)
        before_characters = message_characters(normalized)
        if before_characters <= budget:
            return CompactionResult(
                messages=tuple(deepcopy(normalized)),
                before_message_count=len(normalized),
                after_message_count=len(normalized),
                before_characters=before_characters,
                after_characters=before_characters,
                dropped_message_count=0,
                summary="未超过字符预算，无需压缩。",
                strategy=self.strategy,
            )

        required = _required_group_indices(groups)
        if not required:
            raise CompactionError("上下文缺少初始 system 指令，无法安全压缩")

        # Start with the required groups, then add the newest complete groups
        # until the deterministic summary and required context no longer fit.
        selected = set(required)
        for group in reversed(groups):
            if group.index in selected:
                continue
            selected.add(group.index)
            candidate, summary = self._candidate(groups, selected, budget)
            if candidate is None:
                selected.remove(group.index)
                break

        # The previous pass may have stopped before an older optional group,
        # but removing an optional group can change the summary size.  Walk
        # backwards once more to keep the result stable and as recent as fits.
        while True:
            candidate, summary = self._candidate(groups, selected, budget)
            if candidate is not None:
                break
            optional = [index for index in sorted(selected) if index not in required]
            if not optional:
                raise CompactionError(
                    "字符预算过小，无法同时保留 system 指令、当前任务和压缩标记"
                )
            selected.remove(optional[0])

        assert candidate is not None
        assert summary is not None
        after_characters = message_characters(candidate)
        if after_characters > budget:
            raise CompactionError(
                "字符预算过小，无法生成合法的压缩上下文"
            )
        dropped_messages = sum(
            len(group.messages) for group in groups if group.index not in selected
        )
        return CompactionResult(
            messages=tuple(deepcopy(candidate)),
            before_message_count=len(normalized),
            after_message_count=len(candidate),
            before_characters=before_characters,
            after_characters=after_characters,
            dropped_message_count=dropped_messages,
            summary=summary,
            strategy=self.strategy,
        )

    def _candidate(
        self,
        groups: Sequence[_MessageGroup],
        selected: set[int],
        budget: int,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        dropped = [group for group in groups if group.index not in selected]
        summary = _history_summary(dropped)
        summary_message = (
            {"role": "system", "content": summary} if dropped else None
        )
        ordered: list[dict[str, Any]] = []
        first_system = next(
            (group for group in groups if group.index in selected and "system" in group.roles),
            None,
        )
        if first_system is None:
            return None, None
        ordered.extend(deepcopy(list(first_system.messages)))
        if summary_message is not None:
            ordered.append(summary_message)
        for group in groups:
            if group.index == first_system.index or group.index not in selected:
                continue
            ordered.extend(deepcopy(list(group.messages)))
        if message_characters(ordered) <= budget:
            return ordered, summary

        # Preserve the required messages while shrinking only the explanatory
        # marker.  The marker remains explicit even at the smallest budget.
        if summary_message is not None:
            required_messages = [
                message
                for message in ordered
                if message is not summary_message
            ]
            required_characters = message_characters(required_messages)
            available = budget - required_characters
            minimum = "[Xi 历史压缩摘要：已省略历史消息。]"
            if available < _message_characters(summary_message):
                if available < len(
                    json.dumps(
                        {"role": "system", "content": minimum},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ):
                    return None, None
                summary = _clip_summary(summary, available)
                ordered = [
                    message
                    if message is not summary_message
                    else {"role": "system", "content": summary}
                    for message in ordered
                ]
            if message_characters(ordered) <= budget:
                return ordered, summary
        return None, None


def message_characters(messages: Sequence[Mapping[str, Any]]) -> int:
    """Count normalized messages using stable JSON characters (not tokens)."""

    return sum(_message_characters(message) for message in messages)


def _message_characters(message: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            dict(message),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _validate_budget(value: int) -> int:
    if isinstance(value, bool):
        raise CompactionError("context budget 必须是正整数")
    try:
        budget = int(value)
    except (TypeError, ValueError) as exc:
        raise CompactionError("context budget 必须是正整数") from exc
    if budget <= 0:
        raise CompactionError("context budget 必须是正整数")
    return budget


def _normalize_messages(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CompactionError("模型上下文必须是消息序列")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise CompactionError(f"消息第 {index} 项不是对象")
        message = deepcopy(dict(item))
        role = message.get("role")
        if not isinstance(role, str) or not role.strip():
            raise CompactionError(f"消息第 {index} 项缺少有效 role")
        try:
            json.dumps(message, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise CompactionError(f"消息第 {index} 项不是合法 JSON 数据") from exc
        result.append(message)
    if not result:
        raise CompactionError("模型上下文为空，无法压缩")
    return result


def _group_messages(messages: Sequence[dict[str, Any]]) -> list[_MessageGroup]:
    groups: list[_MessageGroup] = []
    index = 0
    group_index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "tool":
            raise CompactionError("上下文包含没有对应 assistant tool_call 的 tool 消息")
        group_messages = [message]
        if role == "assistant" and _tool_call_ids(message):
            call_ids = _tool_call_ids(message)
            index += 1
            seen: set[str] = set()
            while index < len(messages) and messages[index].get("role") == "tool":
                tool_message = messages[index]
                call_id = tool_message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in call_ids:
                    raise CompactionError("tool 消息未对应 assistant tool_call")
                if call_id in seen:
                    raise CompactionError(f"tool_call_id 重复: {call_id}")
                seen.add(call_id)
                group_messages.append(tool_message)
                index += 1
            if seen != set(call_ids):
                raise CompactionError("assistant tool_call 缺少完整的 tool 结果")
            groups.append(_MessageGroup(group_index, tuple(deepcopy(group_messages))))
            group_index += 1
            continue
        groups.append(_MessageGroup(group_index, (deepcopy(message),)))
        group_index += 1
        index += 1
    return groups


def _required_group_indices(groups: Sequence[_MessageGroup]) -> set[int]:
    system = next(
        (group for group in groups if group.messages[0].get("role") == "system"),
        None,
    )
    if system is None:
        return set()
    latest_user = next(
        (
            group
            for group in reversed(groups)
            if any(message.get("role") == "user" for message in group.messages)
        ),
        None,
    )
    required = {system.index}
    if latest_user is not None:
        required.add(latest_user.index)
    return required


def _tool_call_ids(message: Mapping[str, Any]) -> tuple[str, ...]:
    raw_calls = message.get("tool_calls")
    if not raw_calls:
        return ()
    if not isinstance(raw_calls, list):
        raise CompactionError("assistant.tool_calls 必须是列表")
    result: list[str] = []
    for call in raw_calls:
        if not isinstance(call, Mapping):
            raise CompactionError("assistant.tool_calls 包含无效项目")
        call_id = call.get("id") or call.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise CompactionError("assistant.tool_calls 缺少有效 id")
        if call_id in result:
            raise CompactionError(f"assistant tool_call id 重复: {call_id}")
        result.append(call_id)
    return tuple(result)


def _history_summary(groups: Sequence[_MessageGroup]) -> str:
    if not groups:
        return ""
    parts = [
        f"第 {group.index + 1} 组({'/'.join(group.roles)})"
        for group in groups
    ]
    return (
        "[Xi 历史压缩摘要：以下较旧消息已保留为历史，不会重新执行工具。"
        f"共省略 {sum(len(group.messages) for group in groups)} 条消息；"
        + "；".join(parts)
        + "]"
    )


def _clip_summary(value: str, available_characters: int) -> str:
    marker = "…]"
    # available_characters is a JSON-message budget, so find the largest
    # content that fits after accounting for the role/content wrapper.
    for length in range(len(value), -1, -1):
        candidate = value[:length]
        if length < len(value):
            candidate = candidate.rstrip(" ]") + marker
        message = {"role": "system", "content": candidate}
        if _message_characters(message) <= available_characters:
            return candidate
    return "[Xi 历史压缩摘要：已省略历史消息。]"


__all__ = [
    "CompactionError",
    "CompactionResult",
    "ContextCompactor",
    "DeterministicCompactor",
    "message_characters",
]
