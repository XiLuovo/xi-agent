from __future__ import annotations

import json
from pathlib import Path

import pytest

from xi.cli import main
from xi.events import JsonlSessionStore
from xi.models import ScriptedModel
from xi.replay import load_trace
from xi.runtime import AgentRuntime
from xi.session import project_session


def test_cli_resume_projects_context_and_continues_same_event_stream(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trace_path = tmp_path / ".xi" / "traces" / "session.jsonl"
    with JsonlSessionStore(trace_path) as store:
        runtime = AgentRuntime(
            ScriptedModel([{"text": "第一轮已经完成。"}]),
            workspace=tmp_path,
            session_store=store,
        )
        first_result = runtime.run(
            "记住第一轮的结论",
            interactive=False,
            continue_session=True,
        )

    assert first_result.success is True
    first_trace = load_trace(trace_path)
    first_final_id = first_trace.events[-1].event_id

    script_path = tmp_path / "resume-script.json"
    script_path.write_text(
        json.dumps([{"text": "第二轮基于旧上下文完成。"}], ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "resume",
            str(trace_path),
            "-p",
            "基于上一轮继续",
            "--script",
            str(script_path),
        ]
    )

    assert exit_code == 0
    assert "第二轮基于旧上下文完成。" in capsys.readouterr().out

    resumed_trace = load_trace(trace_path)
    started_events = [event for event in resumed_trace.events if event.type == "run_started"]
    assert len(started_events) == 2
    assert {event.run_id for event in resumed_trace.events} == {first_result.run_id}
    assert started_events[1].parent_id == first_final_id
    assert started_events[1].payload["session_continued"] is True

    latest_request = [
        event for event in resumed_trace.events if event.type == "model_requested"
    ][-1]
    contents = [
        message.get("content")
        for message in latest_request.payload["messages"]
        if isinstance(message, dict)
    ]
    assert "记住第一轮的结论" in contents
    assert "第一轮已经完成。" in contents
    assert "基于上一轮继续" in contents

    projection = project_session(trace_path)
    assert projection.turns == 2
    assert projection.messages[-1] == {
        "role": "assistant",
        "content": "第二轮基于旧上下文完成。",
    }
