from __future__ import annotations

import json
from pathlib import Path

import pytest

from xi.cli import main
from xi.events import JsonlSessionStore
from xi.models import ScriptedModel
from xi.replay import load_trace
from xi.runtime import AgentRuntime


def test_cli_fork_from_first_of_two_runs_creates_an_independent_branch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_trace_path = tmp_path / ".xi" / "traces" / "source.jsonl"
    with JsonlSessionStore(source_trace_path) as store:
        runtime = AgentRuntime(
            ScriptedModel(
                [
                    {"text": "第一轮结论。"},
                    {"text": "第二轮结论。"},
                ]
            ),
            workspace=tmp_path,
            session_store=store,
        )
        first_result = runtime.run(
            "第一轮问题",
            interactive=False,
            continue_session=True,
        )
        second_result = runtime.run(
            "第二轮问题",
            interactive=False,
            continue_session=True,
        )

    assert first_result.success is True
    assert second_result.success is True
    first_finished = first_result.trace_events[-1]
    assert first_finished.type == "run_finished"
    source_before = source_trace_path.read_bytes()
    source_trace = load_trace(source_trace_path)
    assert source_trace.summary.runs == 2

    fork_script_path = tmp_path / "fork-script.json"
    fork_script_path.write_text(
        json.dumps([{"text": "分支回答。"}], ensure_ascii=False),
        encoding="utf-8",
    )
    fork_trace_path = tmp_path / ".xi" / "traces" / "fork.jsonl"

    exit_code = main(
        [
            "fork",
            str(source_trace_path),
            "--at-event",
            first_finished.event_id,
            "-p",
            "分支问题",
            "--script",
            str(fork_script_path),
            "--trace",
            str(fork_trace_path),
        ]
    )

    assert exit_code == 0
    assert "分支回答。" in capsys.readouterr().out
    assert source_trace_path.read_bytes() == source_before

    fork_trace = load_trace(fork_trace_path)
    assert fork_trace.summary.runs == 1
    assert fork_trace.summary.session_id != source_trace.summary.session_id
    assert fork_trace.summary.run_id not in source_trace.summary.run_ids
    assert {event.session_id for event in fork_trace.events} == {
        fork_trace.summary.session_id
    }

    fork_started = fork_trace.events[0]
    assert fork_started.type == "run_started"
    assert fork_started.parent_id is None
    assert fork_started.payload["session_continued"] is False
    assert fork_started.payload["forked_from"] == {
        "trace": str(source_trace_path.resolve()),
        "session_id": source_trace.summary.session_id,
        "run_id": first_result.run_id,
        "event_id": first_finished.event_id,
    }

    fork_request = next(
        event for event in fork_trace.events if event.type == "model_requested"
    )
    contents = [
        message.get("content")
        for message in fork_request.payload["messages"]
        if isinstance(message, dict)
    ]
    assert "第一轮问题" in contents
    assert "第一轮结论。" in contents
    assert "分支问题" in contents
    assert "第二轮问题" not in contents
    assert "第二轮结论。" not in contents

    rendered = fork_trace.render()
    assert "分支来源:" in rendered
    assert source_trace.summary.session_id in rendered
    assert first_result.run_id in rendered
    assert first_finished.event_id in rendered
