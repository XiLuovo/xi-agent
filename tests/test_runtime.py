from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from xi.completion import EvidenceCompletionContract
from xi.events import JsonlSessionStore
from xi.executor import RestrictedLocalExecutor
from xi.models import OpenAICompatibleModel, ScriptedModel
from xi.policy import DefaultPolicy
from xi.runtime import AgentRuntime


def test_openai_compatible_model_identifies_as_xi(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def create_client(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=create_client))
    model = OpenAICompatibleModel(
        model="test-model",
        api_key="test-key",
        base_url="https://example.test/v1",
    )

    assert model.client is sentinel
    assert captured["default_headers"] == {"User-Agent": "xi-agent/0.1.0"}


def test_scripted_runtime_reads_patches_runs_command_and_records_trace(tmp_path: Path) -> None:
    bug = tmp_path / "calculator.py"
    bug.write_text(
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_calculator.py"
    test_file.write_text(
        "import unittest\n\n"
        "from calculator import add\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    test_command = f'"{sys.executable}" -m unittest -q test_calculator.py'
    model = ScriptedModel(
        [
            {
                "tool_calls": [
                    {
                        "id": "search-1",
                        "name": "search_code",
                        "arguments": {"query": "return a - b"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "read-1",
                        "name": "read_file",
                        "arguments": {"path": "calculator.py"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "patch-1",
                        "name": "apply_patch",
                        "arguments": {
                            "patch": (
                                "*** Begin Patch\n"
                                "*** Update File: calculator.py\n"
                                "@@\n"
                                " def add(a, b):\n"
                                "-    return a - b\n"
                                "+    return a + b\n"
                                "*** End Patch"
                            )
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "command-1",
                        "name": "run_command",
                        "arguments": {"command": test_command},
                    }
                ]
            },
            {"text": "已修复并验证。"},
        ]
    )
    trace_path = tmp_path / ".xi" / "traces" / "test.jsonl"
    with JsonlSessionStore(trace_path) as store:
        runtime = AgentRuntime(
            model,
            workspace=tmp_path,
            executor=RestrictedLocalExecutor(tmp_path),
            policy=DefaultPolicy(allowed_commands=[test_command]),
            session_store=store,
            max_steps=6,
        )
        result = runtime.run("修复 bug 并验证", interactive=False)
        events = store.events()

    assert result.success is True
    assert result.text == "已修复并验证。"
    assert bug.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    event_types = [event.type for event in events]
    assert event_types.index("tool_started") < event_types.index("file_changed")
    assert event_types.count("tool_started") == 4
    assert event_types.count("tool_finished") == 4
    assert all(
        event.payload["success"]
        for event in events
        if event.type == "tool_finished"
    )
    assert event_types[-1] == "run_finished"


def test_evidence_contract_rejects_premature_completion_until_edit_and_verification(
    tmp_path: Path,
) -> None:
    implementation = tmp_path / "calculator.py"
    implementation.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    test_file = tmp_path / "test_calculator.py"
    test_file.write_text(
        "import unittest\n\n"
        "from calculator import add\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    test_command = f'"{sys.executable}" -m unittest -q test_calculator.py'
    model = ScriptedModel(
        [
            {
                "tool_calls": [
                    {
                        "id": "read-1",
                        "name": "read_file",
                        "arguments": {"path": "calculator.py"},
                    }
                ]
            },
            {"text": "我已经定位问题，接下来会修改并运行测试。"},
            {
                "tool_calls": [
                    {
                        "id": "patch-1",
                        "name": "apply_patch",
                        "arguments": {
                            "patch": (
                                "*** Begin Patch\n"
                                "*** Update File: calculator.py\n"
                                "@@\n"
                                " def add(a, b):\n"
                                "-    return a - b\n"
                                "+    return a + b\n"
                                "*** End Patch"
                            )
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "command-1",
                        "name": "run_command",
                        "arguments": {"command": test_command},
                    }
                ]
            },
            {"text": "已完成修改并通过验证。"},
        ]
    )
    trace_path = tmp_path / ".xi" / "traces" / "completion.jsonl"

    with JsonlSessionStore(trace_path) as store:
        runtime = AgentRuntime(
            model,
            workspace=tmp_path,
            executor=RestrictedLocalExecutor(tmp_path),
            policy=DefaultPolicy(allowed_commands=[test_command]),
            session_store=store,
            completion_contract=EvidenceCompletionContract(
                require_file_change=True,
                required_commands=[test_command],
            ),
            max_steps=6,
        )
        result = runtime.run("修复加法并验证", interactive=False)
        events = store.events()

    decisions = [event for event in events if event.type == "completion_decided"]
    assert [event.payload["accepted"] for event in decisions] == [False, True]
    assert "尚未产生任何成功的文件修改" in decisions[0].payload["missing"]
    assert any(test_command in item for item in decisions[0].payload["missing"])
    assert result.success is True
    assert result.steps == 5
    assert result.text == "已完成修改并通过验证。"
    assert implementation.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
