"""Git worktree lifecycle adapter for isolated Xi workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4


class WorktreeError(RuntimeError):
    """Raised when a detached worktree cannot be created or inspected."""


@dataclass(slots=True)
class WorktreeRecord:
    source_repo: Path
    worktree_path: Path
    base_revision: str
    cleanup_policy: str = "remove"
    state: str = "created"
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "workspace_mode": "worktree",
            "source_repo": str(self.source_repo),
            "worktree_path": str(self.worktree_path),
            "base_revision": self.base_revision,
            "cleanup_policy": self.cleanup_policy,
            "state": self.state,
            "error": self.error,
        }


@dataclass(slots=True)
class WorktreeCleanup:
    removed: bool
    retained: bool
    state: str
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "removed": self.removed,
            "retained": self.retained,
            "state": self.state,
            "error": self.error,
        }


class WorktreeManager:
    """Own Git commands and the safe lifecycle of one temporary worktree."""

    def __init__(
        self,
        source_repo: str | Path,
        *,
        root: str | Path | None = None,
        keep: bool = False,
        git_binary: str = "git",
    ) -> None:
        self.source_repo = Path(source_repo).expanduser().resolve()
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else self.source_repo.parent / ".xi-worktrees"
        )
        self.keep = bool(keep)
        self.git_binary = str(git_binary).strip() or "git"
        self.record: WorktreeRecord | None = None

    def create(self) -> WorktreeRecord:
        if self.record is not None:
            raise WorktreeError("一个 WorktreeManager 只能创建一个 worktree")
        repo = self._repo_root()
        base_revision = self._git(repo, "rev-parse", "HEAD").strip()
        if not base_revision:
            raise WorktreeError("源仓库没有可用 HEAD")
        self.root.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = (root / f"xi-{stamp}-{uuid4().hex[:8]}").resolve()
        self._ensure_under_root(path)
        if path.exists():
            raise WorktreeError(f"worktree 目标路径已存在: {path}")
        try:
            self._git(repo, "worktree", "add", "--detach", str(path), base_revision)
        except WorktreeError:
            if path.exists() and self._is_under_root(path):
                self._git_best_effort(repo, "worktree", "remove", "--force", str(path))
            raise
        self.record = WorktreeRecord(
            source_repo=repo,
            worktree_path=path,
            base_revision=base_revision,
            cleanup_policy="keep" if self.keep else "remove",
        )
        return self.record

    def summary(self) -> dict[str, Any]:
        if self.record is None:
            return {
                "workspace_mode": "worktree",
                "source_repo": str(self.source_repo),
                "worktree_root": str(self.root),
                "state": "not_created",
            }
        return self.record.summary()

    def remove(self) -> WorktreeCleanup:
        if self.record is None:
            raise WorktreeError("尚未创建 worktree")
        record = self.record
        if record.cleanup_policy == "keep":
            record.state = "retained"
            return WorktreeCleanup(removed=False, retained=True, state=record.state)
        if not record.worktree_path.exists():
            record.state = "removed"
            return WorktreeCleanup(removed=True, retained=False, state=record.state)
        try:
            self._git(
                record.source_repo,
                "worktree",
                "remove",
                "--force",
                str(record.worktree_path),
            )
        except WorktreeError as exc:
            if not record.worktree_path.exists():
                record.state = "removed"
                return WorktreeCleanup(removed=True, retained=False, state=record.state)
            record.state = "cleanup_failed"
            record.error = str(exc)
            return WorktreeCleanup(
                removed=False,
                retained=False,
                state=record.state,
                error=str(exc),
            )
        record.state = "removed"
        return WorktreeCleanup(removed=True, retained=False, state=record.state)

    def _repo_root(self) -> Path:
        if not self.source_repo.exists() or not self.source_repo.is_dir():
            raise WorktreeError(f"源仓库不是目录: {self.source_repo}")
        try:
            raw = self._git(self.source_repo, "rev-parse", "--show-toplevel")
        except WorktreeError as exc:
            raise WorktreeError(f"不是可用的 Git 仓库: {self.source_repo}; {exc}") from exc
        return Path(raw.strip()).resolve()

    def _ensure_under_root(self, path: Path) -> None:
        if not self._is_under_root(path):
            raise WorktreeError(f"worktree 路径超出显式 worktree root: {path}")

    def _is_under_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return False
        return True

    def _git(self, cwd: Path, *arguments: str) -> str:
        command = [self.git_binary, "-C", str(cwd), *arguments]
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except OSError as exc:
            raise WorktreeError(f"Git 命令无法启动: {type(exc).__name__}: {exc}") from exc
        output = (completed.stdout or "").strip()
        error = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = error or output or f"exit_code={completed.returncode}"
            raise WorktreeError(f"Git 命令失败 ({' '.join(arguments)}): {detail}")
        return output

    def _git_best_effort(self, cwd: Path, *arguments: str) -> None:
        try:
            self._git(cwd, *arguments)
        except WorktreeError:
            return


__all__ = ["WorktreeCleanup", "WorktreeError", "WorktreeManager", "WorktreeRecord"]
