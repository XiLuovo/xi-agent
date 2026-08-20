"""Evidence-driven completion contracts for the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class ToolExecutionEvidence:
    """One completed tool attempt observed by the runtime."""

    tool: str
    arguments: Mapping[str, Any]
    success: bool
    files_changed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletionEvidence:
    """Immutable evidence available when the model proposes completion."""

    executions: tuple[ToolExecutionEvidence, ...] = ()

    @property
    def changed_files(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for execution in self.executions:
            if not execution.success:
                continue
            for path in execution.files_changed:
                seen.setdefault(path, None)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    """Whether a candidate final response has enough execution evidence."""

    accepted: bool
    reason: str
    missing: tuple[str, ...] = ()
    feedback: str = ""


class CompletionContract(Protocol):
    """Small seam used by the runtime before accepting a final response."""

    name: str

    def evaluate(self, evidence: CompletionEvidence) -> CompletionDecision:
        ...


class PermissiveCompletionContract:
    """Accept any final model response, preserving Xi's default behaviour."""

    name = "permissive"

    def evaluate(self, evidence: CompletionEvidence) -> CompletionDecision:
        return CompletionDecision(True, "未配置完成证据要求")


class EvidenceCompletionContract:
    """Require observable edits and successful post-edit verification commands."""

    name = "evidence"

    def __init__(
        self,
        *,
        require_file_change: bool = False,
        required_commands: Sequence[str] = (),
    ) -> None:
        self.require_file_change = bool(require_file_change)
        self.required_commands = tuple(
            dict.fromkeys(command.strip() for command in required_commands if command.strip())
        )
        if not self.require_file_change and not self.required_commands:
            raise ValueError("EvidenceCompletionContract 至少需要一项证据要求")

    def evaluate(self, evidence: CompletionEvidence) -> CompletionDecision:
        missing: list[str] = []
        change_indices = [
            index
            for index, execution in enumerate(evidence.executions)
            if execution.success and execution.files_changed
        ]
        last_change_index = max(change_indices, default=-1)

        if self.require_file_change and not change_indices:
            missing.append("尚未产生任何成功的文件修改")

        for command in self.required_commands:
            successful_indices = [
                index
                for index, execution in enumerate(evidence.executions)
                if execution.tool == "run_command"
                and execution.success
                and str(execution.arguments.get("command", "")).strip() == command
            ]
            if not successful_indices:
                missing.append(f"指定验证命令尚未成功执行：{command}")
            elif last_change_index >= 0 and max(successful_indices) <= last_change_index:
                missing.append(f"文件修改后尚未重新成功执行验证命令：{command}")

        if not missing:
            return CompletionDecision(True, "完成证据已满足")

        feedback = (
            "完成契约未满足，当前回答不会结束任务。\n"
            "缺少证据：\n"
            + "\n".join(f"- {item}" for item in missing)
            + "\n请继续使用工具完成缺失项；只有实际执行并成功后再给出最终总结。"
        )
        return CompletionDecision(
            False,
            "缺少完成证据",
            missing=tuple(missing),
            feedback=feedback,
        )


def evidence_from_executions(
    executions: Sequence[ToolExecutionEvidence],
) -> CompletionEvidence:
    return CompletionEvidence(tuple(executions))


__all__ = [
    "CompletionContract",
    "CompletionDecision",
    "CompletionEvidence",
    "EvidenceCompletionContract",
    "PermissiveCompletionContract",
    "ToolExecutionEvidence",
    "evidence_from_executions",
]
