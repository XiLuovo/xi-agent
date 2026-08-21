"""Xi — A Python Coding Agent.

Xi is an AI pair programmer that runs in your terminal.
It can read, write, and reason about code using LLMs.
"""

__version__ = "0.1.0"

from .completion import (
    CompletionContract,
    CompletionDecision,
    CompletionEvidence,
    EvidenceCompletionContract,
    PermissiveCompletionContract,
    ToolExecutionEvidence,
)
from .compaction import (
    CompactionError,
    CompactionResult,
    ContextCompactor,
    DeterministicCompactor,
    message_characters,
)
from .context import ContextBuilder, RepoMapContextBuilder, SearchContextBuilder
from .events import Event, EventCollection, JsonlSessionStore, MemorySessionStore
from .executor import DockerExecutor, DryRunExecutor, ExecutionLimits, RestrictedLocalExecutor
from .models import ModelResponse, OpenAICompatibleModel, OpenAIModel, ScriptedModel, ToolCall
from .runtime import AgentRuntime, RunResult
from .session import RecoveryPoint, SessionProjection, SessionProjectionError, project_session

__all__ = [
    "__version__",
    "AgentRuntime",
    "RunResult",
    "RecoveryPoint",
    "SessionProjection",
    "SessionProjectionError",
    "project_session",
    "CompactionError",
    "CompactionResult",
    "ContextCompactor",
    "DeterministicCompactor",
    "message_characters",
    "CompletionContract",
    "CompletionDecision",
    "CompletionEvidence",
    "EvidenceCompletionContract",
    "PermissiveCompletionContract",
    "ToolExecutionEvidence",
    "ContextBuilder",
    "SearchContextBuilder",
    "RepoMapContextBuilder",
    "Event",
    "EventCollection",
    "MemorySessionStore",
    "JsonlSessionStore",
    "ExecutionLimits",
    "RestrictedLocalExecutor",
    "DockerExecutor",
    "DryRunExecutor",
    "ModelResponse",
    "ToolCall",
    "ScriptedModel",
    "OpenAICompatibleModel",
    "OpenAIModel",
]
