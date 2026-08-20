"""Tool protocol and registry.

Tools describe *what* the model may request.  The executor owns *how* the
operation touches the local machine, keeping policy and execution concerns out
of individual tool adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


@dataclass(slots=True)
class ToolResult:
    output: str
    success: bool = True
    files_changed: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_message(self) -> str:
        if self.success:
            return self.output
        prefix = "工具执行失败"
        if self.error and self.error != self.output:
            prefix += f"（{self.error}）"
        if self.output and self.output != self.error:
            return f"{prefix}:\n{self.output}"
        return prefix


class ToolExecutor(Protocol):
    def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> ToolResult:
        ...


class Tool(Protocol):
    name: str
    description: str
    parameters: Mapping[str, Any]

    def invoke(self, executor: ToolExecutor, arguments: Mapping[str, Any]) -> ToolResult:
        ...

    def definition(self) -> Mapping[str, Any]:
        ...


class BaseTool:
    """Convenience implementation for the four built-in tools."""

    name = ""
    description = ""
    parameters: Mapping[str, Any] = {"type": "object", "properties": {}}

    def invoke(self, executor: ToolExecutor, arguments: Mapping[str, Any]) -> ToolResult:
        return executor.execute(self.name, arguments)

    def definition(self) -> Mapping[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


class ToolRegistry:
    """Name-based tool lookup with a deliberately tiny public surface."""

    def __init__(self, tools: Sequence[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool name cannot be empty")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return tool

    def definitions(self) -> list[Mapping[str, Any]]:
        return [tool.definition() for tool in self._tools.values()]

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def invoke(self, name: str, executor: ToolExecutor, arguments: Mapping[str, Any]) -> ToolResult:
        return self.require(name).invoke(executor, arguments)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())


__all__ = ["Tool", "ToolExecutor", "ToolResult", "BaseTool", "ToolRegistry"]
