from __future__ import annotations

import json
from pathlib import Path

import pytest

from xi.cli import main
from xi.events import Event, JsonlSessionStore
from xi.models import ScriptedModel
from xi.replay import load_trace
from xi.runtime import AgentRuntime


def test_cli_recovers_safe_incomplete_run_without_replaying_completed_patch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    trace_path = tmp_path / ".xi" / "traces" / "interrupted.jsonl"

    def interrupt_after_safe_event(event: Event) -> None:
        if event.type == "file_changed":
            raise KeyboardInterrupt

    with JsonlSessionStore(trace_path, on_append=interrupt_after_safe_event) as store:
        runtime = AgentRuntime(
            ScriptedModel(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "patch-once",
                                "name": "apply_patch",
                                "arguments": {
                                    "patch": (
                                        "*** Begin Patch\n"
                                        "*** Update File: note.txt\n"
                                        "@@\n"
                                        "-before\n"
                                        "+after\n"
                                        "*** End Patch"
                                    )
                                },
                            }
                        ]
                    },
                ]
            ),
            workspace=tmp_path,
            session_store=store,
        )
        with pytest.raises(KeyboardInterrupt):
            runtime.run(
                "把 note.txt 改为 after",
                interactive=False,
                continue_session=True,
            )

    assert target.read_text(encoding="utf-8") == "after\n"
    interrupted_trace = load_trace(trace_path)
    source_started = interrupted_trace.events[0]
    source_session_id = interrupted_trace.summary.session_id
    source_run_id = source_started.run_id
    checkpoint = next(
        event
        for event in interrupted_trace.events
        if event.type == "model_requested"
    )
    safe_tail = interrupted_trace.events[-1]
    assert safe_tail.type == "file_changed"

    resume_script = tmp_path / "resume-script.json"
    resume_script.write_text(
        json.dumps([{"text": "恢复后正常完成。"}], ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "resume",
            str(trace_path),
            "-p",
            "从安全检查点继续",
            "--script",
            str(resume_script),
        ]
    )

    assert exit_code == 0
    assert "恢复后正常完成。" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "after\n"

    recovered_trace = load_trace(trace_path)
    started_events = [
        event for event in recovered_trace.events if event.type == "run_started"
    ]
    assert len(started_events) == 2
    recovered_started = started_events[-1]
    assert recovered_started.session_id == source_session_id
    assert recovered_started.run_id != source_run_id
    assert recovered_started.parent_id == safe_tail.event_id
    assert {event.session_id for event in recovered_trace.events} == {
        source_session_id
    }
    assert recovered_started.payload["recovered_from"] == {
        "trace": str(trace_path.resolve()),
        "session_id": source_session_id,
        "run_id": source_run_id,
        "event_id": safe_tail.event_id,
        "state": "incomplete",
        "checkpoint_event_id": checkpoint.event_id,
    }

    recovered_request = [
        event
        for event in recovered_trace.events
        if event.type == "model_requested" and event.run_id == recovered_started.run_id
    ][0]
    messages = recovered_request.payload["messages"]
    old_assistant = next(
        message
        for message in messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert old_assistant["tool_calls"][0]["id"] == "patch-once"
    old_tool_result = next(
        message
        for message in messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "patch-once"
    )
    assert "已应用补丁" in old_tool_result["content"]
    assert any(
        message.get("role") == "user"
        and message.get("content") == "从安全检查点继续"
        for message in messages
    )

    patch_starts = [
        event
        for event in recovered_trace.events
        if event.type == "tool_started"
        and event.payload.get("call_id") == "patch-once"
    ]
    assert len(patch_starts) == 1
    assert len(
        [
            event
            for event in recovered_trace.events
            if event.type == "file_changed"
            and event.payload.get("path") == "note.txt"
        ]
    ) == 1
    assert recovered_trace.events[-1].type == "run_finished"
    assert recovered_trace.events[-1].payload["success"] is True

    rendered = recovered_trace.render()
    assert "最近恢复来源:" in rendered
    assert "incomplete" in rendered
    assert source_run_id in rendered
    assert safe_tail.event_id in rendered
    assert checkpoint.event_id in rendered
