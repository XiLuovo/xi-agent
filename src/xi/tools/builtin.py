"""The small built-in tool set used by the first Xi runtime."""

from __future__ import annotations

from .base import BaseTool, ToolRegistry


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取工作区内一个文本文件，可选地限定行号范围。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对于工作区的文件路径"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    }


class SearchCodeTool(BaseTool):
    name = "search_code"
    description = "在工作区文本文件中搜索字符串或正则表达式，并返回文件与行号。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string", "description": "搜索范围，默认为工作区"},
            "regex": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["query"],
        "additionalProperties": False,
    }


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = "将统一 diff 或 Xi patch 应用到工作区内的文件。"
    parameters = {
        "type": "object",
        "properties": {
            "patch": {"type": "string", "description": "统一 diff 或 *** Begin Patch 文本"},
            "path": {"type": "string", "description": "直接写入模式下的文件路径"},
            "content": {"type": "string", "description": "直接写入模式下的新文件内容"},
        },
        "anyOf": [
            {"required": ["patch"]},
            {"required": ["path", "content"]},
        ],
        "additionalProperties": False,
    }


class RunCommandTool(BaseTool):
    name = "run_command"
    description = "在受限工作区目录内运行命令，返回受限长度的标准输出和错误。"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "工作区内的相对目录"},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 300},
        },
        "required": ["command"],
        "additionalProperties": False,
    }


def default_tools() -> ToolRegistry:
    return ToolRegistry(
        [ReadFileTool(), SearchCodeTool(), ApplyPatchTool(), RunCommandTool()]
    )


__all__ = [
    "ReadFileTool",
    "SearchCodeTool",
    "ApplyPatchTool",
    "RunCommandTool",
    "default_tools",
]
