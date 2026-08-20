from __future__ import annotations

import json
from pathlib import Path

from xi.cli import main


def test_repo_map_cli_records_bounded_safe_context(tmp_path: Path) -> None:
    """The repo-map strategy exposes an index without leaking ignored files."""

    (tmp_path / "app.py").write_text(
        "class Service:\n    def run(self, value):\n        return value\n",
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text("ordinary file\n", encoding="utf-8")
    (tmp_path / ".env").write_text("XI_API_KEY=must-not-appear\n", encoding="utf-8")
    hidden_dir = tmp_path / ".xi"
    hidden_dir.mkdir()
    (hidden_dir / "trace.jsonl").write_text("secret trace\n", encoding="utf-8")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "credentials.py").write_text(
        "def private_token():\n    return 'hidden'\n", encoding="utf-8"
    )
    script_path = tmp_path / "responses.json"
    script_path.write_text(json.dumps([{"text": "已完成"}], ensure_ascii=False), encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"

    assert (
        main(
            [
                "-p",
                "请检查仓库结构",
                "--workspace",
                str(tmp_path),
                "--script",
                str(script_path),
                "--trace",
                str(trace_path),
                "--context-strategy",
                "repo-map",
            ]
        )
        == 0
    )

    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    started = events[0]
    requested = next(event for event in events if event["type"] == "model_requested")
    system_context = requested["payload"]["messages"][0]["content"]

    assert started["type"] == "run_started"
    assert started["payload"]["context_strategy"] == "repo-map"
    assert started["payload"]["context_characters"] == len(system_context)
    assert "app.py" in system_context
    assert "class Service" in system_context
    assert ".env" not in system_context
    assert ".xi" not in system_context
    assert "credentials.py" not in system_context
    assert "must-not-appear" not in system_context
