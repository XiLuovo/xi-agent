"""Authorization policy kept separate from the local executor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(slots=True)
class PolicyContext:
    workspace: Path
    interactive: bool = False
    auto_approve: bool = False
    step: int = 0
    max_steps: int = 0


@dataclass(slots=True)
class PolicyDecision:
    decision: Decision
    reason: str
    rule: str = "default"

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class Policy:
    def decide(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: PolicyContext,
    ) -> PolicyDecision:
        raise NotImplementedError


class DefaultPolicy(Policy):
    """Small allow/ask/deny policy for the first milestone.

    Workspace reads and patches are allowed when their paths stay inside the
    workspace.  Commands use a conservative safe-prefix list; unknown but not
    obviously destructive commands require approval in interactive mode and
    are denied by a headless run unless ``auto_approve`` is enabled.
    """

    allowed_tools = frozenset({"read_file", "search_code", "apply_patch", "run_command"})
    safe_command_patterns = (
        r"^(?:python(?:3)?|py)(?:\.exe)?\s+-m\s+(?:pytest|unittest)(?:\s+.*)?$",
        r"^pytest(?:\.exe)?(?:\s+.*)?$",
        r"^(?:uv|poetry)\s+run\s+pytest(?:\s+.*)?$",
        r"^(?:ruff|mypy)(?:\s+.*)?$",
        r"^git\s+(?:diff|status|log)(?:\s+.*)?$",
        r"^echo(?:\s+.*)?$",
    )
    denied_patterns = (
        r"\brm\s+(-[A-Za-z]*f|--force)",
        r"\brm\s+-rf\b",
        r"\bdel\b.*(/s|/q|/f)",
        r"(^|[;&|]\s*)format(?:\.com)?\s",
        r"\bshutdown\b",
        r"\breboot\b",
        r"git\s+reset\s+--hard",
        r"git\s+clean\s+-.*f",
        r"Remove-Item\b.*-Recurse",
        r"(curl|wget)\b[^|\n]*\|\s*(sh|bash|powershell)",
        r"\bmkfs\b",
    )

    def __init__(self, *, allowed_commands: Iterable[str] | None = None) -> None:
        self.allowed_commands = frozenset(
            command.strip() for command in (allowed_commands or ()) if command.strip()
        )

    def decide(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: PolicyContext,
    ) -> PolicyDecision:
        if tool_name not in self.allowed_tools:
            return PolicyDecision(Decision.DENY, f"工具未在白名单中: {tool_name}", "tool_allowlist")
        path_decision = self._check_paths(tool_name, arguments, context.workspace)
        if path_decision is not None:
            return path_decision
        if tool_name != "run_command":
            return PolicyDecision(Decision.ALLOW, "工作区内操作", "workspace_allow")

        command = str(arguments.get("command", "")).strip()
        if not command:
            return PolicyDecision(Decision.DENY, "命令不能为空", "command_required")
        lowered = command.lower()
        for pattern in self.denied_patterns:
            if re.search(pattern, command, flags=re.IGNORECASE):
                return PolicyDecision(Decision.DENY, "命令匹配危险操作规则", "dangerous_command")
        if command in self.allowed_commands:
            return PolicyDecision(Decision.ALLOW, "命令命中显式精确白名单", "explicit_command")
        command_segments = re.split(r"&&|\|\||[;|\r\n]", lowered)
        if len(command_segments) > 1:
            return PolicyDecision(
                Decision.ASK,
                "复合 Shell 命令需要人工确认",
                "compound_command",
            )
        first_segment = command_segments[0].strip()
        if re.search(r"[<>`]|\$\(", command):
            return PolicyDecision(
                Decision.ASK,
                "包含 Shell 重定向或命令替换，需要人工确认",
                "shell_metacharacter",
            )
        if re.search(r"(^|[\s=\"'])\.\.(?:[\\/]|$)", command) or re.search(
            r"(^|[\s=\"'])(?:[A-Za-z]:[\\/]|\\\\|/[^/])",
            command,
        ):
            return PolicyDecision(
                Decision.ASK,
                "命令包含工作区边界无法静态确认的路径",
                "command_path",
            )
        if context.auto_approve:
            return PolicyDecision(Decision.ALLOW, "显式启用自动批准", "auto_approve")
        if any(re.fullmatch(pattern, first_segment) for pattern in self.safe_command_patterns):
            return PolicyDecision(Decision.ALLOW, "命令属于本地开发/测试白名单", "safe_command")
        return PolicyDecision(
            Decision.ASK,
            "命令不在默认白名单中，需要人工确认",
            "unknown_command",
        )

    def _check_paths(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        workspace: Path,
    ) -> PolicyDecision | None:
        path_values: list[tuple[str, Any]] = []
        if tool_name in {"read_file", "search_code"}:
            path_values.append(("path", arguments.get("path", ".")))
        elif tool_name == "run_command":
            path_values.append(("cwd", arguments.get("cwd", ".")))
        elif tool_name == "apply_patch":
            if arguments.get("path") is not None:
                path_values.append(("path", arguments.get("path")))
            patch = arguments.get("patch", arguments.get("diff", ""))
            if isinstance(patch, str):
                for raw in _patch_paths(patch):
                    path_values.append(("patch", raw))
        for label, value in path_values:
            if not isinstance(value, str) or not value.strip():
                return PolicyDecision(Decision.DENY, f"{label} 路径不能为空", "path_required")
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                candidate.resolve(strict=False).relative_to(workspace.resolve())
            except ValueError:
                return PolicyDecision(Decision.DENY, f"{label} 路径超出工作区", "workspace_boundary")
        return None


def _patch_paths(patch: str) -> list[str]:
    result: list[str] = []
    for line in patch.replace("\r\n", "\n").split("\n"):
        match = re.match(r"\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", line)
        if match:
            result.append(match.group(1).strip().strip('"'))
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0].strip().strip('"')
            if raw != "/dev/null":
                if raw.startswith("a/") or raw.startswith("b/"):
                    raw = raw[2:]
                result.append(raw)
    return result


__all__ = ["Decision", "Policy", "PolicyContext", "PolicyDecision", "DefaultPolicy"]
