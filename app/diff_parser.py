"""Parse git unified diff output into structured data.

The diff format we parse is the standard unified diff produced by
`git diff` or `diff -u`.  Example::

    diff --git a/app/main.py b/app/main.py
    index abc123..def456 100644
    --- a/app/main.py
    +++ b/app/main.py
    @@ -10,7 +10,9 @@ def hello():
    -    print("hello")
    +    print("hello, world")
    +
    +    return 42
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Regular expressions for parsing
# ---------------------------------------------------------------------------

_FILE_HEADER_RE = re.compile(
    r"^diff --git a/(?P<old_path>.*?) b/(?P<new_path>.*?)$"
)
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_NEW_FILE_RE = re.compile(r"^new file mode")
_DELETED_FILE_RE = re.compile(r"^deleted file mode")
_RENAME_RE = re.compile(
    r"^rename (?:from|to) (?P<path>.*?)$"
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ChangedLine:
    """A single changed line within a hunk."""

    new_line_number: int | None  # None 表示删除行（只有旧行号）
    old_line_number: int | None  # None 表示新增行（只有新行号）
    content: str
    change_type: str  # "+" / "-" / " " (context)
    is_context: bool = False


@dataclass
class Hunk:
    """A contiguous block of changes within a file."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context: str = ""  # @@ 行后面的函数名等注释
    lines: list[ChangedLine] = field(default_factory=list)

    @property
    def added_lines(self) -> list[ChangedLine]:
        return [ln for ln in self.lines if ln.change_type == "+" and not ln.is_context]

    @property
    def removed_lines(self) -> list[ChangedLine]:
        return [ln for ln in self.lines if ln.change_type == "-" and not ln.is_context]


@dataclass
class FileDiff:
    """Changes within a single file."""

    old_path: str
    new_path: str
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def effective_path(self) -> str:
        """The path that matters (new_path unless deleted)."""
        return self.new_path if not self.is_deleted else self.old_path

    @property
    def total_added(self) -> int:
        return sum(len(h.added_lines) for h in self.hunks)

    @property
    def total_removed(self) -> int:
        return sum(len(h.removed_lines) for h in self.hunks)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a complete unified diff string into structured data.

    Returns:
        List of FileDiff, one per changed file.
    """
    files: list[FileDiff] = []
    current_file: FileDiff | None = None
    current_hunk: Hunk | None = None

    for raw_line in diff_text.splitlines():
        line = raw_line.rstrip("\n")

        # --- File header ---
        if m := _FILE_HEADER_RE.match(line):
            current_file = FileDiff(
                old_path=m.group("old_path"),
                new_path=m.group("new_path"),
            )
            current_hunk = None
            files.append(current_file)
            continue

        if current_file is None:
            continue

        # --- File mode markers ---
        if _NEW_FILE_RE.match(line):
            current_file.is_new = True
            continue
        if _DELETED_FILE_RE.match(line):
            current_file.is_deleted = True
            continue
        if m := _RENAME_RE.match(line):
            current_file.is_rename = True
            continue

        # --- Hunk header ---
        if m := _HUNK_HEADER_RE.match(line):
            context = line[m.end() :].strip()
            current_hunk = Hunk(
                old_start=int(m.group("old_start")),
                old_count=int(m.group("old_count") or 1),
                new_start=int(m.group("new_start")),
                new_count=int(m.group("new_count") or 1),
                context=context,
            )
            current_file.hunks.append(current_hunk)
            continue

        # --- Lines inside a hunk ---
        if current_hunk is None:
            continue

        if line.startswith(" "):
            # Context line
            current_hunk.lines.append(
                ChangedLine(
                    new_line_number=current_hunk.new_start + len(current_hunk.lines),
                    old_line_number=current_hunk.old_start + len(current_hunk.lines),
                    content=line[1:],
                    change_type=" ",
                    is_context=True,
                )
            )
        elif line.startswith("+"):
            current_hunk.lines.append(
                ChangedLine(
                    new_line_number=current_hunk.new_start
                    + sum(1 for ln in current_hunk.lines if ln.change_type in ("+", " ")),
                    old_line_number=None,
                    content=line[1:],
                    change_type="+",
                )
            )
        elif line.startswith("-"):
            current_hunk.lines.append(
                ChangedLine(
                    new_line_number=None,
                    old_line_number=current_hunk.old_start
                    + sum(1 for ln in current_hunk.lines if ln.change_type in ("-", " ")),
                    content=line[1:],
                    change_type="-",
                )
            )
        elif line == r"\ No newline at end of file":
            # 忽略这个注释行
            pass

    return files


def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    """Fetch the diff for a PR using the GitHub API via ``gh`` CLI.

    Requires ``gh`` CLI to be installed and authenticated.
    """
    result = subprocess.run(
        [
            "gh", "pr", "diff", str(pr_number),
            "--repo", f"{owner}/{repo}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr diff failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def format_diff_for_review(files: list[FileDiff]) -> str:
    """Format parsed diffs into a compact string for LLM input."""
    parts: list[str] = []
    for fd in files:
        parts.append(f"\n### {fd.effective_path}")
        if fd.is_new:
            parts.append("  [新文件]")
        elif fd.is_deleted:
            parts.append("  [已删除]")
        parts.append(f"  +{fd.total_added} -{fd.total_removed}")

        for h in fd.hunks:
            parts.append(
                f"\n  @@ -{h.old_start},{h.old_count} "
                f"+{h.new_start},{h.new_count} @@ {h.context}"
            )
            for ln in h.lines[:50]:  # 每个 hunk 最多展示 50 行
                prefix = ln.change_type
                parts.append(f"  {prefix} {ln.content}")
            if len(h.lines) > 50:
                parts.append(f"  ... ({len(h.lines) - 50} more lines)")
    return "\n".join(parts)
