"""Structured events and append-only session stores for Xi.

The runtime emits events before and after every externally visible action.  The
event object is intentionally small: a caller only needs to know the event
type, run lineage, and a JSON-compatible payload.  This keeps the seam useful
for both the interactive UI and headless evaluation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from uuid import uuid4


EVENT_SCHEMA_VERSION = 2
LEGACY_EVENT_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Event:
    """One append-only runtime event.

    ``payload`` must contain JSON-compatible values.  ``parent_id`` links an
    event to the model response or tool proposal that caused it, which makes a
    flat JSONL file replayable as a causal tree.
    """

    type: str
    run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    parent_id: str | None = None
    timestamp: str = field(default_factory=_now)
    usage: dict[str, Any] | None = None
    schema_version: int = EVENT_SCHEMA_VERSION
    # v1 traces did not carry a session id.  Keeping this optional at the
    # dataclass seam lets callers still construct legacy-shaped events while
    # ``__post_init__`` gives them the deterministic compatibility identity.
    session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = self.run_id

    @property
    def event_type(self) -> str:
        """Readable alias used by a few integrations."""

        return self.type

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""

        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        """Rehydrate an event read from a JSONL trace."""

        data = dict(value)
        if "type" not in data and "event_type" in data:
            data["type"] = data.pop("event_type")
        # A missing schema marker predates the explicit v2 field.  Treat it as
        # v1 and derive the session identity from the run identity below.
        data.setdefault("schema_version", LEGACY_EVENT_SCHEMA_VERSION)
        data.setdefault("payload", {})
        if not data.get("session_id") and data.get("run_id"):
            data["session_id"] = data["run_id"]
        allowed = {
            "type",
            "run_id",
            "payload",
            "event_id",
            "parent_id",
            "timestamp",
            "usage",
            "schema_version",
            "session_id",
        }
        return cls(**{key: item for key, item in data.items() if key in allowed})


class EventCollection(list[Event]):
    """List-like event view that also supports the legacy ``events()`` call."""

    def __call__(self) -> list[Event]:
        return list(self)


class SessionStore(Protocol):
    """Small seam for durable or in-memory event storage."""

    def append(self, event: Event) -> None:
        ...

    @property
    def events(self) -> Iterable[Event]:
        ...

    def close(self) -> None:
        ...


class MemorySessionStore:
    """An in-memory adapter useful for tests and embedding."""

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, event: Event) -> None:
        self._events.append(event)

    @property
    def events(self) -> EventCollection:
        return EventCollection(self._events)

    def read_events(self) -> EventCollection:
        return self.events

    def close(self) -> None:
        return None

    def __iter__(self):
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __enter__(self) -> "MemorySessionStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class JsonlSessionStore:
    """Append events to a UTF-8 JSONL file and flush after every event."""

    def __init__(
        self,
        path: str | Path,
        *,
        on_append: Callable[[Event], None] | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        self._on_append = on_append
        self._closed = False

    def append(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("session store is closed")
        self._handle.write(event.to_json() + "\n")
        self._handle.flush()
        if self._on_append is not None:
            self._on_append(event)

    @property
    def events(self) -> EventCollection:
        if not self._closed:
            self._handle.flush()
        if not self.path.exists():
            return EventCollection()
        result: EventCollection = EventCollection()
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    result.append(Event.from_dict(json.loads(line)))
        return result

    def read_events(self) -> EventCollection:
        return self.events

    def close(self) -> None:
        if not self._closed:
            self._handle.close()
            self._closed = True

    def __enter__(self) -> "JsonlSessionStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


JsonlStore = JsonlSessionStore
MemoryStore = MemorySessionStore


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "LEGACY_EVENT_SCHEMA_VERSION",
    "Event",
    "EventCollection",
    "SessionStore",
    "MemorySessionStore",
    "JsonlSessionStore",
    "JsonlStore",
    "MemoryStore",
]
