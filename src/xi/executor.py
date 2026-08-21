"""Execution adapters for Xi's built-in tools.

The local adapter intentionally calls itself *restricted*, not an OS sandbox.
The Docker adapter adds process isolation for shell commands while satisfying
the same small ``execute`` interface, so the agent loop and policy remain
independent from the selected execution backend.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping
from uuid import uuid4

from .tools.base import ToolResult


class WorkspaceViolation(ValueError):
    """Raised internally when a requested path leaves the workspace."""


@dataclass(slots=True)
class ExecutionLimits:
    max_output_chars: int = 12_000
    max_file_bytes: int = 2_000_000
    command_timeout_seconds: float = 30.0


class RestrictedLocalExecutor:
    """Execute the four built-in operations under a resolved workspace root."""

    name = "local"

    def __init__(
        self,
        workspace: str | Path,
        *,
        limits: ExecutionLimits | None = None,
        max_output_chars: int | None = None,
        max_file_bytes: int | None = None,
        command_timeout_seconds: float | None = None,
    ) -> None:
        root = Path(workspace).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"工作区不是目录: {root}")
        self.workspace = root
        base_limits = limits or ExecutionLimits()
        try:
            self.limits = ExecutionLimits(
                max_output_chars=max(
                    int(
                        max_output_chars
                        if max_output_chars is not None
                        else base_limits.max_output_chars
                    ),
                    256,
                ),
                max_file_bytes=max(
                    int(max_file_bytes if max_file_bytes is not None else base_limits.max_file_bytes),
                    1,
                ),
                command_timeout_seconds=max(
                    float(
                        command_timeout_seconds
                        if command_timeout_seconds is not None
                        else base_limits.command_timeout_seconds
                    ),
                    0.1,
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("执行限制必须是有效的正数") from exc

    def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            if tool_name == "read_file":
                return self._read_file(arguments)
            if tool_name == "search_code":
                return self._search_code(arguments)
            if tool_name == "apply_patch":
                return self._apply_patch(arguments)
            if tool_name == "run_command":
                return self._run_command(arguments)
            return ToolResult(
                output=f"未知工具: {tool_name}",
                success=False,
                error=f"unknown tool: {tool_name}",
            )
        except WorkspaceViolation as exc:
            return ToolResult(output=str(exc), success=False, error=str(exc))
        except (OSError, ValueError, re.error) as exc:
            return ToolResult(output=f"执行失败: {exc}", success=False, error=str(exc))

    def resolve_path(self, raw_path: str | Path, *, allow_root: bool = False) -> Path:
        """Resolve a path and reject traversal, absolute escapes, and symlinks."""

        if raw_path is None or str(raw_path).strip() == "":
            raise WorkspaceViolation("路径不能为空")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        candidate = candidate.expanduser().resolve(strict=False)
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise WorkspaceViolation(f"路径超出工作区: {raw_path}") from exc
        if candidate == self.workspace and not allow_root:
            raise WorkspaceViolation("不能把工作区根目录当作文件")
        return candidate

    def relative_path(self, path: str | Path) -> str:
        candidate = self.resolve_path(path, allow_root=True)
        return candidate.relative_to(self.workspace).as_posix() or "."

    def _read_file(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = self.resolve_path(str(arguments.get("path", "")))
        if not path.is_file():
            return ToolResult(output=f"文件不存在: {self.relative_path(path)}", success=False)
        if path.stat().st_size > self.limits.max_file_bytes:
            return ToolResult(
                output=f"文件过大（上限 {self.limits.max_file_bytes} 字节）: {self.relative_path(path)}",
                success=False,
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        start = _positive_int(arguments.get("start_line"), default=1)
        end = _positive_int(arguments.get("end_line"), default=None)
        if start != 1 or end is not None:
            lines = text.splitlines(keepends=True)
            selected = lines[start - 1 : end]
            text = "".join(selected)
        limited, truncated = _limit_text(text, self.limits.max_output_chars)
        return ToolResult(
            output=limited,
            metadata={"path": self.relative_path(path), "truncated": truncated},
        )

    def _search_code(self, arguments: Mapping[str, Any]) -> ToolResult:
        query = str(arguments.get("query", ""))
        if not query:
            return ToolResult(output="query 不能为空", success=False)
        root = self.resolve_path(str(arguments.get("path", ".")), allow_root=True)
        if not root.exists():
            return ToolResult(output=f"搜索路径不存在: {arguments.get('path')}", success=False)
        use_regex = bool(arguments.get("regex", False))
        pattern = re.compile(query) if use_regex else None
        max_results = min(max(_positive_int(arguments.get("max_results"), default=50) or 50, 1), 200)
        matches: list[str] = []
        files = [root] if root.is_file() else self._iter_text_files(root)
        for file_path in files:
            try:
                resolved_file = file_path.resolve(strict=False)
                resolved_file.relative_to(self.workspace)
                if not resolved_file.is_file():
                    continue
                if resolved_file.stat().st_size > self.limits.max_file_bytes:
                    continue
                raw = resolved_file.read_bytes()
                if b"\x00" in raw:
                    continue
                text = raw.decode("utf-8", errors="replace")
            except (OSError, ValueError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                matched = bool(pattern.search(line)) if pattern is not None else query in line
                if matched:
                    rel = resolved_file.relative_to(self.workspace).as_posix()
                    matches.append(f"{rel}:{line_number}: {line}")
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
        if not matches:
            return ToolResult(output="没有找到匹配项", metadata={"matches": 0})
        output, truncated = _limit_text("\n".join(matches), self.limits.max_output_chars)
        return ToolResult(
            output=output,
            metadata={"matches": len(matches), "truncated": truncated},
        )

    def _iter_text_files(self, root: Path):
        ignored = {".git", ".xi", "__pycache__", ".venv", "venv", "node_modules"}
        if root.is_file():
            yield root
            return
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(name for name in dirs if name not in ignored and not name.startswith("."))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                yield Path(current) / name

    def _apply_patch(self, arguments: Mapping[str, Any]) -> ToolResult:
        direct_path = arguments.get("path")
        if direct_path is not None and "content" in arguments:
            path = self.resolve_path(str(direct_path))
            content = str(arguments.get("content", ""))
            if len(content.encode("utf-8")) > self.limits.max_file_bytes:
                return ToolResult(
                    output=f"文件过大（上限 {self.limits.max_file_bytes} 字节）: {self.relative_path(path)}",
                    success=False,
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(
                output=f"已写入 {self.relative_path(path)}",
                files_changed=[self.relative_path(path)],
                metadata={"mode": "direct_write"},
            )

        patch_text = arguments.get("patch", arguments.get("diff", ""))
        if not isinstance(patch_text, str) or not patch_text.strip():
            return ToolResult(output="需要 patch，或同时提供 path 与 content", success=False)
        changes = self._parse_patch(patch_text)
        changed: list[str] = []
        for raw_path, new_content in changes.items():
            path = self.resolve_path(raw_path)
            if new_content is None:
                if path.exists():
                    if not path.is_file():
                        raise ValueError(f"拒绝删除非普通文件: {self.relative_path(path)}")
                    path.unlink()
                    changed.append(self.relative_path(path))
                continue
            if len(new_content.encode("utf-8")) > self.limits.max_file_bytes:
                raise ValueError(
                    f"文件过大（上限 {self.limits.max_file_bytes} 字节）: {self.relative_path(path)}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_content, encoding="utf-8")
            changed.append(self.relative_path(path))
        return ToolResult(
            output="已应用补丁: " + (", ".join(changed) if changed else "无文件变化"),
            files_changed=changed,
            metadata={"mode": "patch", "files": changed},
        )

    def _parse_patch(self, patch_text: str) -> dict[str, str | None]:
        normalized = patch_text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        if any(line.strip() == "*** Begin Patch" for line in lines):
            return self._parse_xi_patch(lines)
        return self._parse_unified_patch(lines)

    def _parse_xi_patch(self, lines: list[str]) -> dict[str, str | None]:
        changes: dict[str, str | None] = {}
        i = 0
        while i < len(lines) and lines[i].strip() != "*** Begin Patch":
            i += 1
        i += 1
        while i < len(lines):
            header = lines[i]
            if header.strip() == "*** End Patch":
                break
            match = re.match(r"\*\*\* (Update|Add|Delete) File:\s*(.+?)\s*$", header)
            if not match:
                i += 1
                continue
            operation, raw_path = match.groups()
            raw_path = _clean_patch_path(raw_path)
            i += 1
            body: list[str] = []
            while i < len(lines) and not lines[i].startswith("*** "):
                body.append(lines[i])
                i += 1
            if operation == "Delete":
                changes[raw_path] = None
                continue
            if operation == "Add":
                content_lines = [line[1:] if line.startswith("+") else line for line in body]
                changes[raw_path] = _join_patch_lines(content_lines)
                continue
            existing_path = self.resolve_path(raw_path)
            original = existing_path.read_text(encoding="utf-8", errors="replace") if existing_path.exists() else ""
            changes[raw_path] = _apply_xi_update(original, body)
        if not changes:
            raise ValueError("补丁中没有可识别的文件变更")
        return changes

    def _parse_unified_patch(self, lines: list[str]) -> dict[str, str | None]:
        changes: dict[str, str | None] = {}
        i = 0
        while i < len(lines):
            if not lines[i].startswith("--- "):
                i += 1
                continue
            old_name = _clean_patch_path(lines[i][4:].split("\t", 1)[0].strip())
            i += 1
            if i >= len(lines) or not lines[i].startswith("+++ "):
                raise ValueError("统一 diff 缺少 +++ 文件头")
            new_name = _clean_patch_path(lines[i][4:].split("\t", 1)[0].strip())
            i += 1
            target_name = new_name if new_name != "/dev/null" else old_name
            if target_name == "/dev/null":
                raise ValueError("无法确定补丁目标文件")
            target_path = self.resolve_path(target_name)
            old_content = "" if old_name == "/dev/null" else (
                target_path.read_text(encoding="utf-8", errors="replace") if target_path.exists() else ""
            )
            current = old_content.splitlines()
            while i < len(lines) and not lines[i].startswith("--- "):
                if not lines[i].startswith("@@"):
                    i += 1
                    continue
                header = lines[i]
                i += 1
                hunk: list[str] = []
                while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("--- "):
                    if lines[i].startswith((" ", "+", "-", "\\")):
                        hunk.append(lines[i])
                    i += 1
                current = _apply_hunk(current, hunk, header)
            if new_name == "/dev/null":
                changes[old_name] = None
            else:
                changes[new_name] = _join_patch_lines(current)
        if not changes:
            raise ValueError("补丁中没有可识别的统一 diff")
        return changes

    def _run_command(self, arguments: Mapping[str, Any]) -> ToolResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(output="command 不能为空", success=False)
        cwd = self.resolve_path(str(arguments.get("cwd", ".")), allow_root=True)
        if not cwd.is_dir():
            return ToolResult(output=f"cwd 不是目录: {self.relative_path(cwd)}", success=False)
        timeout = _command_timeout(arguments.get("timeout_seconds"), self.limits)
        env = _safe_environment()
        executable = "cmd.exe" if os.name == "nt" else "/bin/sh"
        if os.name == "nt":
            executable = env.get("COMSPEC") or shutil.which("cmd.exe") or executable
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                executable=executable,
            )
        except subprocess.TimeoutExpired as exc:
            partial_stdout = exc.stdout or ""
            partial_stderr = exc.stderr or ""
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode("utf-8", errors="replace")
            if isinstance(partial_stderr, bytes):
                partial_stderr = partial_stderr.decode("utf-8", errors="replace")
            partial = str(partial_stdout) + str(partial_stderr)
            output, truncated = _limit_text(partial, self.limits.max_output_chars)
            return ToolResult(
                output=f"命令超时（{timeout:g}s）\n{output}",
                success=False,
                metadata={"timeout": True, "truncated": truncated},
                error="command timeout",
            )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        combined = stdout
        if stderr:
            combined = f"{combined}\n[stderr]\n{stderr}" if combined else f"[stderr]\n{stderr}"
        output, truncated = _limit_text(combined, self.limits.max_output_chars)
        return ToolResult(
            output=output or "(命令无输出)",
            success=completed.returncode == 0,
            metadata={
                "exit_code": completed.returncode,
                "cwd": self.relative_path(cwd),
                "truncated": truncated,
            },
            error=None if completed.returncode == 0 else f"exit code {completed.returncode}",
        )


class DryRunExecutor:
    """Read normally, but record mutations and commands without executing them."""

    name = "dry-run"

    def __init__(self, workspace: str | Path, *, limits: ExecutionLimits | None = None) -> None:
        self._reader = RestrictedLocalExecutor(workspace, limits=limits)
        self.workspace = self._reader.workspace
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> ToolResult:
        copied = dict(arguments)
        self.calls.append((tool_name, copied))
        if tool_name in {"read_file", "search_code"}:
            return self._reader.execute(tool_name, copied)
        if tool_name in {"apply_patch", "run_command"}:
            return ToolResult(
                output=f"[dry-run] 已记录 {tool_name}",
                metadata={"dry_run": True, "arguments": copied},
            )
        return ToolResult(output=f"未知工具: {tool_name}", success=False)


LocalExecutor = RestrictedLocalExecutor


def _positive_int(value: Any, *, default: int | None) -> int | None:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _limit_text(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    marker = f"\n… [输出已截断，限制 {limit} 字符]"
    return text[: max(limit - len(marker), 0)] + marker, True


def _safe_environment() -> dict[str, str]:
    blocked_fragments = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    allowed_names = {
        "path",
        "pathext",
        "comspec",
        "systemroot",
        "systemdrive",
        "windir",
        "temp",
        "tmp",
        "home",
        "userprofile",
        "homedrive",
        "homepath",
        "pythonunbuffered",
    }
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(fragment in upper for fragment in blocked_fragments):
            continue
        if key.lower() in allowed_names:
            result[key] = value
    result.setdefault("PYTHONUNBUFFERED", "1")
    return result


def _docker_cli_environment() -> dict[str, str]:
    """Return the narrow host environment needed by the Docker CLI.

    Docker connection selectors are intentionally kept out of the shared
    local-command environment.  They are host-side CLI configuration, not
    variables that should change the default local executor's child process.
    """

    result = _safe_environment()
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
    ):
        value = os.environ.get(name)
        if value:
            result[name] = value
    return result


def _clean_patch_path(raw: str) -> str:
    value = raw.strip().strip('"')
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    return value


def _join_patch_lines(lines: list[str]) -> str:
    return "\n".join(lines) + ("\n" if lines else "")


def _apply_xi_update(original: str, body: list[str]) -> str:
    current = original.splitlines()
    hunk_groups: list[tuple[str | None, list[str]]] = []
    header: str | None = None
    hunk: list[str] = []
    saw_hunk_header = False
    for line in body:
        if line.startswith("@@"):
            if hunk or header is not None:
                hunk_groups.append((header, hunk))
            header = line
            hunk = []
            saw_hunk_header = True
        elif line.startswith(("+", "-", " ")):
            hunk.append(line)
        elif line and not saw_hunk_header:
            hunk.append(" " + line)
    if hunk or header is not None:
        hunk_groups.append((header, hunk))
    if not hunk_groups:
        return _join_patch_lines(body)
    for header, hunk in hunk_groups:
        current = _apply_hunk(current, hunk, header)
    return _join_patch_lines(current)


def _apply_hunk(current: list[str], hunk: list[str], header: str | None) -> list[str]:
    old_lines: list[str] = []
    new_lines: list[str] = []
    for line in hunk:
        if line.startswith("\\"):
            continue
        if not line:
            marker, content = " ", ""
        else:
            marker, content = line[0], line[1:]
        if marker in {" ", "-"}:
            old_lines.append(content)
        if marker in {" ", "+"}:
            new_lines.append(content)
    if old_lines:
        index = _find_subsequence(current, old_lines)
        if index < 0:
            # Be a little forgiving about files with a final newline.
            normalized_current = [line.rstrip("\r") for line in current]
            normalized_old = [line.rstrip("\r") for line in old_lines]
            index = _find_subsequence(normalized_current, normalized_old)
        if index < 0:
            raise ValueError("补丁上下文与当前文件不匹配")
    else:
        index = _header_new_index(header, len(current))
    return current[:index] + new_lines + current[index + len(old_lines) :]


def _find_subsequence(haystack: list[str], needle: list[str]) -> int:
    if not needle:
        return -1
    width = len(needle)
    for index in range(0, len(haystack) - width + 1):
        if haystack[index : index + width] == needle:
            return index
    return -1


def _header_new_index(header: str | None, length: int) -> int:
    if header:
        match = re.search(r"\+(\d+)", header)
        if match:
            return min(max(int(match.group(1)) - 1, 0), length)
    return length


def _command_timeout(value: Any, limits: ExecutionLimits) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return limits.command_timeout_seconds
    if not math.isfinite(timeout):
        return limits.command_timeout_seconds
    return min(max(timeout, 0.1), limits.command_timeout_seconds)


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    stdout = _as_text(exc.stdout)
    stderr = _as_text(exc.stderr)
    if stderr:
        return f"{stdout}\n[stderr]\n{stderr}" if stdout else f"[stderr]\n{stderr}"
    return stdout


class DockerExecutor(RestrictedLocalExecutor):
    """Run shell commands in an isolated Docker container.

    The executor deliberately keeps the same small ``ToolExecutor`` interface
    as :class:`RestrictedLocalExecutor`.  File-oriented tools still use the
    inherited workspace-checked implementation, while ``run_command`` is
    executed by a short-lived Linux container with only the workspace mounted
    read/write.  This is a v1 process-isolation seam; it does not claim to be
    a complete host security boundary for a compromised Docker daemon.
    """

    name = "docker"
    pids_limit = 256
    memory_limit = "512m"
    memory_swap_limit = "512m"
    cpu_limit = "1.0"

    def __init__(
        self,
        workspace: str | Path,
        *,
        image: str | None = None,
        docker_binary: str = "docker",
        limits: ExecutionLimits | None = None,
        max_output_chars: int | None = None,
        max_file_bytes: int | None = None,
        command_timeout_seconds: float | None = None,
        network_disabled: bool = True,
        read_only_root: bool = True,
        tmpfs_size: str = "64m",
    ) -> None:
        super().__init__(
            workspace,
            limits=limits,
            max_output_chars=max_output_chars,
            max_file_bytes=max_file_bytes,
            command_timeout_seconds=command_timeout_seconds,
        )
        normalized_image = str(
            image or os.getenv("XI_DOCKER_IMAGE") or "python:3.11-slim"
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", normalized_image):
            raise ValueError("Docker image 名称包含不受支持的字符")
        normalized_binary = str(docker_binary).strip()
        if not normalized_binary:
            raise ValueError("Docker CLI 路径不能为空")
        normalized_tmpfs = str(tmpfs_size).strip().lower()
        if not re.fullmatch(r"[0-9]+[kmgt]?", normalized_tmpfs):
            raise ValueError("tmpfs 大小必须是数字加可选单位 k/m/g/t")
        self.image = normalized_image
        self.docker_binary = normalized_binary
        self._docker_path = shutil.which(normalized_binary)
        self.network_disabled = bool(network_disabled)
        self.read_only_root = bool(read_only_root)
        self.tmpfs_size = normalized_tmpfs

    @property
    def available(self) -> bool:
        """Whether the configured Docker CLI can be resolved on this host."""

        return self._docker_path is not None

    @property
    def availability_error(self) -> str | None:
        if self.available:
            return None
        return (
            f"找不到 Docker CLI: {self.docker_binary}；"
            "请安装 Docker Desktop/Engine，或改用 --executor local"
        )

    def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> ToolResult:
        result = super().execute(tool_name, arguments)
        if tool_name == "run_command":
            audit = self._audit_metadata()
            audit.update(result.metadata)
            audit["executor"] = self.name
            audit["docker_image"] = self.image
            audit["fallback"] = False
            result.metadata = audit
        else:
            result.metadata.setdefault("executor", self.name)
            result.metadata.setdefault("execution_location", "host-workspace")
        return result

    def _run_command(self, arguments: Mapping[str, Any]) -> ToolResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(
                output="command 不能为空",
                success=False,
                error="command 不能为空",
                metadata=self._audit_metadata(),
            )
        if not self.available:
            return ToolResult(
                output=self.availability_error or "Docker CLI 不可用",
                success=False,
                error="docker unavailable",
                metadata=self._audit_metadata(),
            )

        try:
            cwd = self.resolve_path(str(arguments.get("cwd", ".")), allow_root=True)
        except WorkspaceViolation as exc:
            return ToolResult(
                output=str(exc),
                success=False,
                error=str(exc),
                metadata=self._audit_metadata(),
            )
        if not cwd.is_dir():
            relative = self.relative_path(cwd)
            return ToolResult(
                output=f"cwd 不是目录: {relative}",
                success=False,
                error="cwd is not a directory",
                metadata=self._audit_metadata(cwd=relative),
            )

        timeout = _command_timeout(arguments.get("timeout_seconds"), self.limits)
        relative_cwd = self.relative_path(cwd)
        container_cwd = "/workspace" if relative_cwd == "." else f"/workspace/{relative_cwd}"
        container_name = f"xi-exec-{uuid4().hex[:12]}"
        docker_command = self._build_docker_command(
            command,
            container_cwd=container_cwd,
            container_name=container_name,
        )
        environment = _docker_cli_environment()
        try:
            completed = subprocess.run(
                docker_command,
                cwd=str(self.workspace),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            cleaned = self._remove_container(container_name, environment)
            partial = _timeout_output(exc)
            output, truncated = _limit_text(partial, self.limits.max_output_chars)
            return ToolResult(
                output=f"Docker 命令超时（{timeout:g}s）\n{output}" if output else f"Docker 命令超时（{timeout:g}s）",
                success=False,
                error="docker command timeout",
                metadata=self._audit_metadata(
                    container=container_name,
                    cwd=relative_cwd,
                    timeout=True,
                    container_cleanup=cleaned,
                    truncated=truncated,
                ),
            )
        except KeyboardInterrupt:
            self._remove_container(container_name, environment)
            raise
        except FileNotFoundError:
            self._docker_path = None
            return ToolResult(
                output=self.availability_error or "Docker CLI 不可用",
                success=False,
                error="docker unavailable",
                metadata=self._audit_metadata(),
            )
        except OSError as exc:
            return ToolResult(
                output=f"Docker 执行失败: {exc}",
                success=False,
                error=str(exc),
                metadata=self._audit_metadata(
                    container=container_name,
                    cwd=relative_cwd,
                ),
            )

        stdout = _as_text(completed.stdout)
        stderr = _as_text(completed.stderr)
        combined = stdout
        if stderr:
            combined = f"{combined}\n[stderr]\n{stderr}" if combined else f"[stderr]\n{stderr}"
        output, truncated = _limit_text(combined, self.limits.max_output_chars)
        return ToolResult(
            output=output or "(命令无输出)",
            success=completed.returncode == 0,
            error=None if completed.returncode == 0 else f"docker exit code {completed.returncode}",
            metadata=self._audit_metadata(
                container=container_name,
                cwd=relative_cwd,
                exit_code=completed.returncode,
                truncated=truncated,
            ),
        )

    def _audit_metadata(self, **extra: Any) -> dict[str, Any]:
        """Describe the selected isolation policy on every Docker outcome."""

        metadata: dict[str, Any] = {
            "executor": self.name,
            "execution_location": "docker-container",
            "docker_image": self.image,
            "entrypoint": "/bin/sh",
            "network": "none" if self.network_disabled else "default",
            "read_only_root": self.read_only_root,
            "workspace_mount": "/workspace:rw",
            "tmpfs": f"/tmp:rw,nosuid,nodev,size={self.tmpfs_size}",
            "cap_drop": "ALL",
            "no_new_privileges": True,
            "pids_limit": self.pids_limit,
            "memory_limit": self.memory_limit,
            "memory_swap_limit": self.memory_swap_limit,
            "cpu_limit": self.cpu_limit,
            "command_timeout_limit_seconds": self.limits.command_timeout_seconds,
            "fallback": False,
        }
        metadata.update(extra)
        return metadata

    def _build_docker_command(
        self,
        command: str,
        *,
        container_cwd: str,
        container_name: str,
    ) -> list[str]:
        assert self._docker_path is not None
        args = [
            self._docker_path,
            "run",
            "--rm",
            "--name",
            container_name,
        ]
        if self.network_disabled:
            args.extend(["--network", "none"])
        if self.read_only_root:
            args.append("--read-only")
        mount_source = self.workspace.as_posix() if os.name == "nt" else str(self.workspace)
        args.extend(
            [
                "--mount",
                f"type=bind,source={mount_source},target=/workspace",
                "--tmpfs",
                f"/tmp:rw,nosuid,nodev,size={self.tmpfs_size}",
                "--workdir",
                container_cwd,
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit",
                str(self.pids_limit),
                "--memory",
                self.memory_limit,
                "--memory-swap",
                self.memory_swap_limit,
                "--cpus",
                self.cpu_limit,
                "--env",
                "PYTHONUNBUFFERED=1",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint",
                "/bin/sh",
            ]
        )
        if os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "getgid"):
            args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        args.extend([self.image, "-lc", command])
        return args

    def _remove_container(self, container_name: str, environment: Mapping[str, str]) -> bool:
        if self._docker_path is None:
            return False
        try:
            completed = subprocess.run(
                [self._docker_path, "rm", "-f", container_name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5.0,
                env=dict(environment),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0


__all__ = [
    "ExecutionLimits",
    "WorkspaceViolation",
    "RestrictedLocalExecutor",
    "DockerExecutor",
    "LocalExecutor",
    "DryRunExecutor",
]
