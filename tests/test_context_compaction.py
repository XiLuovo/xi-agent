from __future__ import annotations

import json
from pathlib import Path

import pytest

from xi.cli import main
from xi.context import SearchContextBuilder
from xi.events import Event, JsonlSessionStore
from xi.models import ScriptedModel
from xi.replay import load_trace
from xi.runtime import AgentRuntime


def test_cli_recovers_from_flushed_compaction_without_replaying_tools(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "long-note.txt"
    target.write_text("before\n" + ("旧历史内容" * 1200), encoding="utf-8")
    trace_path = tmp_path / ".xi" / "traces" / "compacted-interruption.jsonl"
    source_task = "读取长文件，并且只把 long-note.txt 改为 after"
    budget = 1400

    def interrupt_after_compaction(event: Event) -> None:
        if event.type == "context_compacted":
            raise KeyboardInterrupt

    with JsonlSessionStore(trace_path, on_append=interrupt_after_compaction) as store:
        runtime = AgentRuntime(
            ScriptedModel(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "read-long-history",
                                "name": "read_file",
                                "arguments": {"path": "long-note.txt"},
                            },
                            {
                                "id": "write-once",
                                "name": "apply_patch",
                                "arguments": {
                                    "path": "long-note.txt",
                                    "content": "after\n",
                                },
                            },
                        ]
                    }
                ]
            ),
            workspace=tmp_path,
            session_store=store,
            context_builder=SearchContextBuilder(max_instruction_chars=0),
            context_budget_chars=budget,
            max_steps=3,
        )
        with pytest.raises(KeyboardInterrupt):
            runtime.run(source_task, interactive=False, continue_session=True)

    assert target.read_text(encoding="utf-8") == "after\n"
    interrupted = load_trace(trace_path)
    compacted = interrupted.events[-1]
    assert compacted.type == "context_compacted"
    assert not any(event.type in {"run_finished", "run_failed"} for event in interrupted.events)
    payload = compacted.payload
    assert payload["strategy"] == "deterministic-v1"
    assert payload["budget_chars"] == budget
    assert payload["before_characters"] > payload["after_characters"]
    assert payload["after_characters"] <= budget
    assert payload["before_message_count"] > payload["after_message_count"]
    compacted_messages = payload["messages"]
    assert compacted_messages[0]["role"] == "system"
    assert source_task in [
        message.get("content")
        for message in compacted_messages
        if message.get("role") == "user"
    ]
    assert any(
        "Xi 历史压缩摘要" in str(message.get("content", ""))
        for message in compacted_messages
    )
    assert _has_legal_tool_groups(compacted_messages)

    source_started = interrupted.events[0]
    source_session_id = interrupted.summary.session_id
    source_run_id = source_started.run_id
    assert compacted.parent_id == next(
        event.event_id
        for event in reversed(interrupted.events[:-1])
        if event.type == "file_changed"
    )

    script_path = tmp_path / "resume-script.json"
    script_path.write_text(
        json.dumps([{"text": "从压缩检查点恢复后正常完成。"}], ensure_ascii=False),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "resume",
            str(trace_path),
            "-p",
            "继续并确认之前的修改",
            "--script",
            str(script_path),
        ]
    )

    assert exit_code == 0
    assert "从压缩检查点恢复后正常完成。" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "after\n"

    recovered = load_trace(trace_path)
    started_events = [event for event in recovered.events if event.type == "run_started"]
    assert len(started_events) == 2
    recovered_started = started_events[-1]
    assert recovered_started.session_id == source_session_id
    assert recovered_started.run_id != source_run_id
    assert recovered_started.parent_id == compacted.event_id
    assert recovered_started.payload["context_budget_chars"] == budget
    assert recovered_started.payload["recovered_from"] == {
        "trace": str(trace_path.resolve()),
        "session_id": source_session_id,
        "run_id": source_run_id,
        "event_id": compacted.event_id,
        "state": "incomplete",
        "checkpoint_event_id": compacted.event_id,
    }

    recovered_request = next(
        event
        for event in recovered.events
        if event.type == "model_requested" and event.run_id == recovered_started.run_id
    )
    assert recovered_request.payload["messages"][:-1] == compacted_messages
    assert recovered_request.payload["messages"][-1] == {
        "role": "user",
        "content": "继续并确认之前的修改",
    }
    assert recovered_request.parent_id == recovered_started.event_id
    assert len(
        [
            event
            for event in recovered.events
            if event.type == "tool_started"
            and event.payload.get("call_id") in {"read-long-history", "write-once"}
        ]
    ) == 2
    assert len(
        [
            event
            for event in recovered.events
            if event.type == "file_changed"
            and event.payload.get("path") == "long-note.txt"
        ]
    ) == 1
    assert recovered.events[-1].type == "run_finished"
    assert recovered.events[-1].payload["success"] is True

    rendered = recovered.render()
    assert "[context_compacted]" in rendered
    assert "上下文压缩: 1 次" in rendered
    assert "最近恢复来源:" in rendered
    assert compacted.event_id in rendered


def _has_legal_tool_groups(messages: list[dict[str, object]]) -> bool:
    expected: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            if expected:
                return False
            calls = message["tool_calls"]
            if not isinstance(calls, list):
                return False
            expected = [
                str(call.get("id"))
                for call in calls
                if isinstance(call, dict) and call.get("id")
            ]
            if len(expected) != len(calls):
                return False
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not expected or call_id != expected[0]:
                return False
            expected.pop(0)
        elif expected:
            return False
    return not expected
