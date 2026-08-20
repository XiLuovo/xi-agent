"""Context-building strategies for repository tasks."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
from typing import Protocol


class ContextBuilder(Protocol):
    def build(self, task: str, workspace: Path) -> str:
        ...


class SearchContextBuilder:
    """Build a compact system context and let the agent search on demand."""

    strategy = "search"

    def __init__(self, *, max_instruction_chars: int = 12_000) -> None:
        self.max_instruction_chars = max_instruction_chars

    def build(self, task: str, workspace: Path) -> str:
        instructions = _load_instructions(workspace, self.max_instruction_chars)
        guidance = (
            "你是 Xi，一个在受限工作区内工作的 Python Coding Agent。"
            "先搜索并读取相关代码，再用 apply_patch 修改；修改后运行任务要求的测试。"
            "只通过提供的工具访问工作区，不要臆测文件内容。完成后给出简洁总结。"
        )
        if instructions:
            guidance += f"\n\n工作区规则（AGENTS.md）：\n{instructions}"
        if task:
            guidance += f"\n\n当前任务：{task}"
        return guidance


class RepoMapContextBuilder:
    """Build a bounded, deterministic repository map before model execution.

    The map is deliberately an index rather than a source-code dump. It lists
    safe workspace files and extracts lightweight module symbols from common
    source formats. Symlinks are never followed or read.
    """

    strategy = "repo-map"

    def __init__(
        self,
        *,
        max_instruction_chars: int = 12_000,
        max_map_chars: int = 16_000,
        max_files: int = 240,
        max_depth: int = 8,
        max_symbols_per_file: int = 20,
        max_source_bytes: int = 256_000,
    ) -> None:
        self.max_instruction_chars = max(0, int(max_instruction_chars))
        self.max_map_chars = max(512, int(max_map_chars))
        self.max_files = max(1, int(max_files))
        self.max_depth = max(0, int(max_depth))
        self.max_symbols_per_file = max(1, int(max_symbols_per_file))
        self.max_source_bytes = max(1_024, int(max_source_bytes))

    def build(self, task: str, workspace: Path) -> str:
        workspace = Path(workspace).expanduser().resolve()
        instructions = _load_instructions(workspace, self.max_instruction_chars)
        repository_map = _build_repository_map(
            workspace,
            max_chars=self.max_map_chars,
            max_files=self.max_files,
            max_depth=self.max_depth,
            max_symbols_per_file=self.max_symbols_per_file,
            max_source_bytes=self.max_source_bytes,
        )
        guidance = (
            "你是 Xi，一个在受限工作区内工作的 Python Coding Agent。"
            "下面的仓库地图仅用于定位，可能因预算而截断；修改前仍需用工具读取真实文件。"
            "结合地图理解模块和调用关系，再用 apply_patch 修改并运行任务要求的测试。"
            "只通过提供的工具访问工作区，不要臆测未读取的实现。完成后给出简洁总结。"
        )
        if instructions:
            guidance += f"\n\n工作区规则（AGENTS.md）：\n{instructions}"
        guidance += f"\n\n仓库地图（确定性、受限摘要）：\n{repository_map}"
        if task:
            guidance += f"\n\n当前任务：{task}"
        return guidance


_IGNORED_DIRECTORY_NAMES = {
    ".xi",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    "site-packages",
}

_SENSITIVE_FILE_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets",
    "secrets.json",
}

_SENSITIVE_SUFFIXES = {
    ".der",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
}

_SENSITIVE_NAME_PATTERN = re.compile(
    r"(?:^|[._-])(?:"
    r"secrets?|credentials?|passwords?|passwd|private[_-]?keys?|"
    r"api[_-]?keys?|access[_-]?tokens?|refresh[_-]?tokens?"
    r")(?:[._-]|$)",
    re.IGNORECASE,
)

_GENERIC_SYMBOL_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    ".js": (
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
    ),
    ".jsx": (
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
    ),
    ".ts": (
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
        ("type", re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
    ),
    ".tsx": (
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
        ("type", re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)),
    ),
    ".go": (
        ("func", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.MULTILINE)),
        ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b", re.MULTILINE)),
    ),
    ".rs": (
        ("fn", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)", re.MULTILINE)),
        ("type", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)", re.MULTILINE)),
    ),
    ".java": (
        ("type", re.compile(r"^\s*(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+)*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)", re.MULTILINE)),
    ),
    ".cs": (
        ("type", re.compile(r"^\s*(?:public\s+|protected\s+|private\s+|internal\s+|abstract\s+|sealed\s+|static\s+|partial\s+)*(?:class|interface|enum|record|struct)\s+([A-Za-z_]\w*)", re.MULTILINE)),
    ),
}


def _build_repository_map(
    workspace: Path,
    *,
    max_chars: int,
    max_files: int,
    max_depth: int,
    max_symbols_per_file: int,
    max_source_bytes: int,
) -> str:
    files, files_truncated = _collect_repository_files(
        workspace,
        max_files=max_files,
        max_depth=max_depth,
    )
    tree_lines = _render_file_tree(files)
    if files_truncated:
        tree_lines.append("- …（达到文件数量上限，后续路径已省略）")
    if not tree_lines:
        tree_lines.append("- （未发现可索引文件）")

    symbol_lines: list[str] = []
    for relative in files:
        symbols = _extract_symbols(
            workspace / relative,
            max_symbols=max_symbols_per_file,
            max_source_bytes=max_source_bytes,
        )
        if not symbols:
            continue
        symbol_lines.append(f"- {relative.as_posix()}")
        symbol_lines.extend(f"  - {symbol}" for symbol in symbols)
    if not symbol_lines:
        symbol_lines.append("- （未发现可提取的模块符号）")

    header_chars = len("文件树：\n\n\n模块符号：\n")
    content_budget = max(max_chars - header_chars, 1)
    tree_budget = max(content_budget * 2 // 5, 1)
    symbol_budget = max(content_budget - tree_budget, 1)
    tree = _clip_block("\n".join(tree_lines), tree_budget)
    symbols = _clip_block("\n".join(symbol_lines), symbol_budget)
    return _clip_block(f"文件树：\n{tree}\n\n模块符号：\n{symbols}", max_chars)


def _collect_repository_files(
    workspace: Path,
    *,
    max_files: int,
    max_depth: int,
) -> tuple[list[Path], bool]:
    files: list[Path] = []

    for root, directory_names, file_names in os.walk(
        workspace,
        topdown=True,
        onerror=lambda _error: None,
        followlinks=False,
    ):
        root_path = Path(root)
        try:
            relative_root = root_path.relative_to(workspace)
        except ValueError:
            directory_names[:] = []
            continue
        depth = 0 if relative_root == Path(".") else len(relative_root.parts)
        if depth >= max_depth:
            directory_names[:] = []
        else:
            directory_names[:] = sorted(
                (
                    name
                    for name in directory_names
                    if not _skip_directory(root_path / name)
                ),
                key=_sort_key,
            )

        for name in sorted(file_names, key=_sort_key):
            path = root_path / name
            if _skip_file(path):
                continue
            try:
                relative = path.relative_to(workspace)
            except ValueError:
                continue
            files.append(relative)
            if len(files) > max_files:
                return files[:max_files], True

    files.sort(key=lambda path: _sort_key(path.as_posix()))
    return files, False


def _skip_directory(path: Path) -> bool:
    name = path.name.casefold()
    return (
        name.startswith(".")
        or name in _IGNORED_DIRECTORY_NAMES
        or name in _SENSITIVE_FILE_NAMES
        or _SENSITIVE_NAME_PATTERN.search(name) is not None
        or path.is_symlink()
    )


def _skip_file(path: Path) -> bool:
    name = path.name.casefold()
    return (
        name.startswith(".")
        or path.is_symlink()
        or name in _SENSITIVE_FILE_NAMES
        or path.suffix.casefold() in _SENSITIVE_SUFFIXES
        or _SENSITIVE_NAME_PATTERN.search(name) is not None
    )


def _render_file_tree(files: list[Path]) -> list[str]:
    lines: list[str] = []
    seen_directories: set[tuple[str, ...]] = set()
    for relative in files:
        parts = relative.parts
        for index, directory in enumerate(parts[:-1]):
            key = tuple(parts[: index + 1])
            if key in seen_directories:
                continue
            seen_directories.add(key)
            lines.append(f"{'  ' * index}- {directory}/")
        lines.append(f"{'  ' * (len(parts) - 1)}- {parts[-1]}")
    return lines


def _extract_symbols(
    path: Path,
    *,
    max_symbols: int,
    max_source_bytes: int,
) -> list[str]:
    suffix = path.suffix.casefold()
    if suffix != ".py" and suffix not in _GENERIC_SYMBOL_PATTERNS:
        return []
    source = _read_source(path, max_source_bytes)
    if source is None:
        return []
    if suffix == ".py":
        return _extract_python_symbols(source, max_symbols)
    return _extract_generic_symbols(source, suffix, max_symbols)


def _read_source(path: Path, max_source_bytes: int) -> str | None:
    if path.is_symlink():
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(max_source_bytes + 1)
    except OSError:
        return None
    if len(data) > max_source_bytes or b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")


def _extract_python_symbols(source: str, limit: int) -> list[str]:
    try:
        module = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    imports: list[str] = []
    declarations: list[str] = []
    for node in module.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            imports.append(prefix + (node.module or ""))
        elif isinstance(node, ast.ClassDef):
            bases = [name for base in node.bases if (name := _dotted_name(base))]
            suffix = f"({', '.join(bases)})" if bases else ""
            declarations.append(f"class {node.name}{suffix}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    declarations.append(_python_function_symbol(child, owner=node.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            declarations.append(_python_function_symbol(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            declarations.extend(f"constant {name}" for name in _assigned_constants(node))

    result: list[str] = []
    unique_imports = list(dict.fromkeys(value for value in imports if value))
    if unique_imports:
        shown = unique_imports[:8]
        import_text = ", ".join(shown)
        if len(unique_imports) > len(shown):
            import_text += ", …"
        result.append(f"imports: {import_text}")
    result.extend(declarations)
    if len(result) > limit:
        return [*result[:limit], "…（符号已截断）"]
    return result


def _python_function_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    owner: str | None = None,
) -> str:
    arguments: list[str] = []
    arguments.extend(argument.arg for argument in node.args.posonlyargs)
    arguments.extend(argument.arg for argument in node.args.args)
    if node.args.vararg is not None:
        arguments.append(f"*{node.args.vararg.arg}")
    elif node.args.kwonlyargs:
        arguments.append("*")
    arguments.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg is not None:
        arguments.append(f"**{node.args.kwarg.arg}")
    qualified_name = f"{owner}.{node.name}" if owner else node.name
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {qualified_name}({', '.join(arguments)})"


def _assigned_constants(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    result: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name) and target.id.isupper():
            result.append(target.id)
    return result


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _extract_generic_symbols(source: str, suffix: str, limit: int) -> list[str]:
    matches: list[tuple[int, str]] = []
    for kind, pattern in _GENERIC_SYMBOL_PATTERNS.get(suffix, ()):
        for match in pattern.finditer(source):
            matches.append((match.start(), f"{kind} {match.group(1)}"))
    matches.sort(key=lambda item: (item[0], item[1]))
    symbols = list(dict.fromkeys(value for _position, value in matches))
    if len(symbols) > limit:
        return [*symbols[:limit], "…（符号已截断）"]
    return symbols


def _clip_block(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n…（已按字符预算截断）"
    if limit <= len(marker):
        return value[:limit]
    prefix = value[: limit - len(marker)]
    if "\n" in prefix:
        prefix = prefix.rsplit("\n", 1)[0]
    return prefix + marker


def _sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _load_instructions(workspace: Path, limit: int) -> str:
    levels: list[Path] = []
    current = workspace
    while True:
        levels.append(current)
        if current.parent == current:
            break
        current = current.parent
    chunks: list[str] = []
    remaining = limit
    if remaining <= 0:
        return ""
    for directory in reversed(levels):
        override = directory / "AGENTS.override.md"
        standard = directory / "AGENTS.md"
        if not override.is_symlink() and override.is_file():
            path = override
        elif not standard.is_symlink() and standard.is_file():
            path = standard
        else:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(f"[{path.name}]\n{text[:remaining]}")
        remaining -= len(text)
        if remaining <= 0:
            break
    return "\n\n".join(chunks)


__all__ = ["ContextBuilder", "SearchContextBuilder", "RepoMapContextBuilder"]
