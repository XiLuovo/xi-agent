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
from .context import ContextBuilder, RepoMapContextBuilder, SearchContextBuilder
from .events import Event, EventCollection, JsonlSessionStore, MemorySessionStore
from .models import ModelResponse, OpenAICompatibleModel, OpenAIModel, ScriptedModel, ToolCall
from .runtime import AgentRuntime, RunResult
from .session import SessionProjection, SessionProjectionError, project_session

__all__ = [
    "__version__",
    "AgentRuntime",
    "RunResult",
    "SessionProjection",
    "SessionProjectionError",
    "project_session",
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
    "ModelResponse",
    "ToolCall",
    "ScriptedModel",
    "OpenAICompatibleModel",
    "OpenAIModel",
]
