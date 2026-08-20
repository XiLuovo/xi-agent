from __future__ import annotations

import json
from pathlib import Path

import pytest

from xi.cli import main
from xi.events import Event
from xi.replay import load_trace


def test_replay_is_read_only_and_rejects_integrity_corruption(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    event_specs = [
        ("run_started", None, {"task": "回放一个安全任务", "workspace": str(tmp_path)}),
        ("model_requested", "e1", {"step": 1, "message_count": 2, "tools": ["run_command"]}),
        ("model_responded", "e2", {"step": 1, "text": "", "tool_calls": [{"name": "run_command"}]}),
        (
            "tool_proposed",
            "e3",
            {
                "step": 1,
                "tool": "run_command",
                "arguments": {"command": "echo XI_API_KEY=opaque-test-value-123456"},
            },
        ),
        ("policy_decided", "e4", {"tool": "run_command", "decision": "allow", "reason": "固定命令"}),
        ("tool_started", "e5", {"tool": "run_command", "arguments": {"command": "echo ok"}}),
        (
            "tool_finished",
            "e6",
            {
                "tool": "run_command",
                "success": True,
                "output": "XI_API_KEY=opaque-test-value-123456 " + ("x" * 400),
            },
        ),
        ("file_changed", "e7", {"path": "calculator.py"}),
        ("run_finished", "e8", {"success": True, "steps": 1, "duration_seconds": 0.25, "text": "完成"}),
    ]
    events = []
    for index, (event_type, parent_id, payload) in enumerate(event_specs, start=1):
        events.append(
            Event(
                type=event_type,
                run_id="run-replay-1",
                event_id=f"e{index}",
                parent_id=parent_id,
                payload=payload,
                timestamp=f"2026-08-20T00:00:0{index}Z",
            )
        )
    trace_path.write_text("\n".join(event.to_json() for event in events) + "\n", encoding="utf-8")
    original_trace = trace_path.read_bytes()

    replay = load_trace(trace_path)
    assert replay.summary.status == "success"
    assert replay.summary.tool_calls == {"run_command": 1}
    assert replay.summary.changed_files == ("calculator.py",)

    assert main(["replay", str(trace_path)]) == 0
    captured = capsys.readouterr()
    assert "[tool_proposed]" in captured.out
    assert "[file_changed] path=calculator.py" in captured.out
    assert "最终状态: success" in captured.out
    assert "opaque-test-value-123456" not in captured.out
    assert "x" * 200 not in captured.out
    assert "…" in captured.out
    assert trace_path.read_bytes() == original_trace
    assert sentinel.read_text(encoding="utf-8") == "untouched"

    corrupted = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    corrupted[-1]["event_id"] = corrupted[0]["event_id"]
    trace_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in corrupted) + "\n",
        encoding="utf-8",
    )
    assert main(["replay", str(trace_path)]) == 1
    assert "event_id 重复" in capsys.readouterr().err

    corrupted[-1]["event_id"] = "e9"
    corrupted[2]["parent_id"] = "missing-event"
    trace_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in corrupted) + "\n",
        encoding="utf-8",
    )
    assert main(["replay", str(trace_path)]) == 1
    assert "parent_id" in capsys.readouterr().err
