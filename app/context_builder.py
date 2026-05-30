"""Context Builder — AST-based analysis of changed code.

Uses tree-sitter to parse changed files, extract function/class definitions,
and trace call chains.  The resulting context graph is injected into the LLM
prompt so the model sees more than just the raw diff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.diff_parser import FileDiff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ChangedSymbol:
    """A function, class, or variable touched by the PR."""

    name: str
    kind: str  # "function", "class", "method", "variable"
    file_path: str
    line_start: int
    line_end: int


@dataclass
class CallSite:
    """A place where a changed symbol is referenced in the codebase."""

    caller_name: str  # the function/method making the call
    file_path: str
    line: int
    snippet: str  # the line of code


@dataclass
class ContextGraph:
    """The complete context built for a PR."""

    changed_symbols: list[ChangedSymbol] = field(default_factory=list)
    callers: dict[str, list[CallSite]] = field(
        default_factory=dict
    )  # symbol_name → call sites
    related_files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tree-sitter extraction (pure regex fallback when tree-sitter unavailable)
# ---------------------------------------------------------------------------


def _extract_symbols_regex(source: str, file_path: str) -> list[ChangedSymbol]:
    """Extract function/class definitions using regex.

    This is the lightweight fallback when tree-sitter is not installed.
    Supports Python only.
    """
    import re

    symbols: list[ChangedSymbol] = []
    lines = source.split("\n")

    # class ClassName(...):
    for m in re.finditer(r"^\s*class\s+(\w+)\s*[(:]", source, re.MULTILINE):
        line_no = source[: m.start()].count("\n") + 1
        symbols.append(
            ChangedSymbol(
                name=m.group(1), kind="class", file_path=file_path,
                line_start=line_no, line_end=line_no,
            )
        )

    # def function_name(...):
    for m in re.finditer(r"^\s*def\s+(\w+)\s*\(", source, re.MULTILINE):
        line_no = source[: m.start()].count("\n") + 1
        indent = len(m.group(0)) - len(m.group(0).lstrip())
        kind = "method" if indent > 0 else "function"
        symbols.append(
            ChangedSymbol(
                name=m.group(1), kind=kind, file_path=file_path,
                line_start=line_no, line_end=line_no,
            )
        )

    return symbols


def _find_callers_regex(source: str, symbol_name: str, file_path: str) -> list[CallSite]:
    """Find call sites for a symbol using regex."""
    import re

    callers: list[CallSite] = []
    lines = source.split("\n")

    # Matches: symbol_name(...) but not def symbol_name(...)
    pattern = rf"(?<!def\s)\b{re.escape(symbol_name)}\s*\("
    for m in re.finditer(pattern, source):
        line_no = source[: m.start()].count("\n") + 1

        # Find the enclosing function
        caller_name = "<module>"
        for i in range(line_no - 2, -1, -1):
            if i < len(lines):
                fm = re.match(r"^\s*def\s+(\w+)\s*\(", lines[i])
                if fm:
                    caller_name = fm.group(1)
                    break

        callers.append(
            CallSite(
                caller_name=caller_name, file_path=file_path,
                line=line_no, snippet=lines[line_no - 1].strip()[:120],
            )
        )

    return callers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_context(
    files: list[FileDiff],
    repo_path: str,
    *,
    source_loader: SourceLoader | None = None,
) -> ContextGraph:
    """Build a context graph for a set of changed files.

    Args:
        files: Parsed diff files.
        repo_path: Path to the repository root (for reading source files).
        source_loader: Optional callback to load source content.
                       Default reads from disk at ``repo_path``.

    Returns:
        ContextGraph with changed symbols and their call sites.
    """
    loader = source_loader or _default_loader
    graph = ContextGraph()

    for fd in files:
        if fd.is_deleted:
            continue

        path = fd.effective_path
        if not path.endswith(".py"):
            continue  # Phase 2: Python only

        try:
            source = loader(repo_path, path)
        except Exception:
            logger.debug("Cannot read %s for context building, skipping", path)
            continue

        # Extract changed symbols
        symbols = _extract_symbols_regex(source, path)
        graph.changed_symbols.extend(symbols)

        # Find callers for each symbol in this file
        for sym in symbols:
            graph.callers.setdefault(sym.name, [])
            graph.callers[sym.name].extend(
                _find_callers_regex(source, sym.name, path)
            )

    # Collect related files
    seen = {fd.effective_path for fd in files}
    for cs_list in graph.callers.values():
        for cs in cs_list:
            if cs.file_path not in seen:
                graph.related_files.append(cs.file_path)
                seen.add(cs.file_path)

    return graph


def format_context_for_prompt(graph: ContextGraph) -> str:
    """Format a ContextGraph into a human-readable string for the LLM prompt."""
    if not graph.changed_symbols:
        return ""

    parts: list[str] = [
        "\n## 代码上下文分析（自动提取）\n",
        "### 变更影响的符号",
    ]

    for sym in graph.changed_symbols:
        parts.append(f"- `{sym.name}` ({sym.kind}) 在 `{sym.file_path}:{sym.line_start}`")

    if graph.callers:
        parts.append("\n### 调用关系")
        for name, sites in graph.callers.items():
            if not sites:
                continue
            parts.append(f"\n**`{name}`** 被以下位置调用:")
            for cs in sites[:5]:  # 最多显示 5 个调用点
                parts.append(f"- `{cs.caller_name}` 在 `{cs.file_path}:{cs.line}`")
                parts.append(f"  ```{cs.snippet}```")
            if len(sites) > 5:
                parts.append(f"  ... 以及另外 {len(sites) - 5} 个调用点")

    if graph.related_files:
        parts.append("\n### 关联文件（可能受影响）")
        for f in graph.related_files[:10]:
            parts.append(f"- {f}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


SourceLoader = callable  # (repo_path: str, file_path: str) -> str


def _default_loader(repo_path: str, file_path: str) -> str:
    """Load source file from disk."""
    import os

    full = os.path.join(repo_path, file_path)
    with open(full, encoding="utf-8") as fh:
        return fh.read()
