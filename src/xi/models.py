"""Model adapters used by the Xi runtime.

The runtime only depends on the small :class:`Model` seam.  The production
adapter talks to any OpenAI-compatible Chat Completions endpoint; the scripted
adapter makes the same loop deterministic for tests and benchmark fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4


@dataclass(slots=True)
class ToolCall:
    """A normalized model-requested tool call."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"call_{uuid4().hex[:12]}")

    @classmethod
    def from_value(cls, value: Any) -> "ToolCall":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) and hasattr(value, "model_dump"):
            value = value.model_dump()
        if not isinstance(value, Mapping):
            raise TypeError(f"unsupported tool call: {value!r}")

        function = value.get("function")
        if isinstance(function, Mapping):
            name = function.get("name") or value.get("name")
            raw_arguments = function.get("arguments", value.get("arguments", {}))
        else:
            name = value.get("name") or value.get("tool")
            raw_arguments = value.get("arguments", value.get("input", {}))

        if not name:
            raise ValueError("tool call is missing a name")
        if isinstance(raw_arguments, str):
            try:
                parsed_arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON arguments for {name}: {exc}") from exc
            if not isinstance(parsed_arguments, Mapping):
                raise ValueError(f"tool arguments for {name} must decode to an object")
            arguments = dict(parsed_arguments)
        elif isinstance(raw_arguments, Mapping):
            arguments = dict(raw_arguments)
        else:
            raise TypeError(f"tool arguments for {name} must be an object")

        return cls(
            name=str(name),
            arguments=arguments,
            id=str(value.get("id") or value.get("call_id") or f"call_{uuid4().hex[:12]}"),
        )


@dataclass(slots=True)
class ModelResponse:
    """Normalized response returned by any model adapter."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None

    @classmethod
    def from_value(cls, value: Any) -> "ModelResponse":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(text=value)
        if not isinstance(value, Mapping) and hasattr(value, "model_dump"):
            value = value.model_dump()
        if not isinstance(value, Mapping):
            raise TypeError(f"unsupported model response: {value!r}")

        raw_calls = value.get("tool_calls", value.get("calls", value.get("tool_call", []))) or []
        if not raw_calls and (value.get("name") or value.get("tool")):
            raw_calls = [value]
        if isinstance(raw_calls, Mapping):
            raw_calls = [raw_calls]
        calls = [ToolCall.from_value(item) for item in raw_calls]
        text = value.get("text", value.get("content", value.get("message", "")))
        if isinstance(text, Mapping):
            nested = text
            nested_calls = nested.get("tool_calls", nested.get("calls", [])) or []
            if nested_calls and not calls:
                calls = [ToolCall.from_value(item) for item in nested_calls]
            text = nested.get("text", nested.get("content", ""))
        if text is None:
            text = ""
        if isinstance(text, Sequence) and not isinstance(text, (str, bytes, bytearray)):
            parts: list[str] = []
            for part in text:
                if isinstance(part, Mapping):
                    part_text = part.get("text", "")
                else:
                    part_text = getattr(part, "text", part)
                if part_text:
                    parts.append(str(part_text))
            text = "".join(parts)
        elif not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        usage = value.get("usage", {}) or {}
        if not isinstance(usage, Mapping) and hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        return cls(
            text=text,
            tool_calls=calls,
            usage=dict(usage) if isinstance(usage, Mapping) else {},
            finish_reason=value.get("finish_reason"),
        )


class Model(Protocol):
    """The intentionally narrow model seam."""

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        ...


class ScriptedModel:
    """Deterministic model adapter.

    ``responses`` may contain :class:`ModelResponse`, strings, or dictionaries
    such as ``{"tool_calls": [{"name": "read_file", "arguments": ...}]}``.
    A callable can be supplied when a fixture needs to inspect the accumulated
    messages.  The call history is exposed for evaluation and assertions.
    """

    def __init__(
        self,
        responses: Sequence[Any]
        | Callable[[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]], int], Any]
        | None = None,
        *,
        script: Sequence[Any] | None = None,
        repeat_last: bool = False,
    ) -> None:
        if isinstance(responses, (str, bytes)):
            responses = [responses]
        if isinstance(script, (str, bytes)):
            script = [script]
        if responses is not None and script is not None:
            raise ValueError("responses 与 script 不能同时提供")
        selected = script if script is not None else responses
        if selected is None:
            selected = []
        self._callable = selected if callable(selected) else None
        self._responses = list(selected) if not callable(selected) else []
        self.repeat_last = repeat_last
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        index = len(self.calls)
        self.calls.append({"messages": [dict(item) for item in messages], "tools": list(tools)})
        if self._callable is not None:
            value = self._callable(messages, tools, index)
        elif index < len(self._responses):
            value = self._responses[index]
        elif self.repeat_last and self._responses:
            value = self._responses[-1]
        else:
            return ModelResponse(text="脚本模型没有更多响应。")
        return ModelResponse.from_value(value)


class OpenAICompatibleModel:
    """Adapter for OpenAI-compatible Chat Completions APIs.

    Credentials and endpoint configuration are read from explicit arguments
    first, then ``XI_*``/``OPENAI_*`` environment variables.  The SDK import is
    lazy so ScriptedModel users do not need a configured network client.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model or os.getenv("XI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        self.api_key = api_key or os.getenv("XI_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("XI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        self.temperature = temperature
        configured_timeout = timeout_seconds
        if configured_timeout is None:
            raw_timeout = os.getenv("XI_MODEL_TIMEOUT", "60")
            try:
                configured_timeout = float(raw_timeout)
            except ValueError as exc:
                raise ValueError("XI_MODEL_TIMEOUT 必须是数字") from exc
        self.timeout_seconds = max(float(configured_timeout), 0.1)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise RuntimeError("OpenAI SDK 未安装，无法使用真实模型") from exc
            client_api_key = self.api_key or ("not-required" if self.base_url else None)
            if not client_api_key:
                raise RuntimeError("未找到 API key，请设置 XI_API_KEY 或 OPENAI_API_KEY")
            kwargs: dict[str, Any] = {
                "api_key": client_api_key,
                "timeout": self.timeout_seconds,
                "default_headers": {"User-Agent": "xi-agent/0.1.0"},
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(item) for item in messages],
        }
        if tools:
            kwargs["tools"] = [dict(item) for item in tools]
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        response = self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message
        raw_text = message.content or ""
        if isinstance(raw_text, str):
            text = raw_text
        elif isinstance(raw_text, Sequence) and not isinstance(raw_text, (bytes, bytearray)):
            parts: list[str] = []
            for part in raw_text:
                if isinstance(part, Mapping):
                    value = part.get("text", "")
                else:
                    value = getattr(part, "text", part)
                if value:
                    parts.append(str(value))
            text = "".join(parts)
        else:
            text = str(raw_text)
        calls: list[ToolCall] = []
        for call in getattr(message, "tool_calls", None) or []:
            function = call.function
            calls.append(
                ToolCall(
                    id=str(call.id),
                    name=str(function.name),
                    arguments=_parse_arguments(function.arguments),
                )
            )
        usage_value = getattr(response, "usage", None)
        usage: dict[str, Any] = {}
        if usage_value is not None:
            if hasattr(usage_value, "model_dump"):
                usage = dict(usage_value.model_dump())
            elif hasattr(usage_value, "__dict__"):
                usage = dict(vars(usage_value))
        return ModelResponse(
            text=text,
            tool_calls=calls,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", None),
        )


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, Mapping):
            raise ValueError("model tool arguments must decode to an object")
        return dict(parsed)
    raise TypeError("model tool arguments must be an object or JSON string")


OpenAIModel = OpenAICompatibleModel


__all__ = [
    "Model",
    "ModelResponse",
    "ToolCall",
    "ScriptedModel",
    "OpenAICompatibleModel",
    "OpenAIModel",
]
