from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import xi.executor as executor_module
from xi.executor import DockerExecutor


def test_docker_unavailable_never_falls_back_to_host_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    host_process_calls: list[object] = []

    monkeypatch.setattr(executor_module.shutil, "which", lambda _binary: None)

    def reject_host_process(*args, **kwargs):
        host_process_calls.append((args, kwargs))
        raise AssertionError("Docker 不可用时不应启动任何宿主机命令")

    monkeypatch.setattr(executor_module.subprocess, "run", reject_host_process)
    executor = DockerExecutor(tmp_path, docker_binary="missing-docker")

    result = executor.execute(
        "run_command",
        {"command": "python -c \"print('should-not-run')\""},
    )

    assert result.success is False
    assert result.error == "docker unavailable"
    assert result.metadata["executor"] == "docker"
    assert result.metadata["fallback"] is False
    assert "找不到 Docker CLI" in result.output
    assert host_process_calls == []
