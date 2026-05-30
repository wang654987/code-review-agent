"""Team Rules DSL Engine — YAML-based custom review rules.

Teams define a ``.code-review-rules.yaml`` in their repo root.
The engine loads it and checks changed files against the rules.

Supported rule types:
    - forbid_pattern:    block code matching a regex
    - require_pattern:   code must match a regex
    - forbid_import:     block specific imports
    - max_function_lines: limit function length
    - naming_convention: enforce naming patterns for functions/classes
    - max_new_deps:      limit new dependencies per PR
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.diff_parser import FileDiff
from app.models import ReviewComment, ReviewConfidence, ReviewSeverity, ReviewSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule data structures
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    name: str
    rule_type: str  # forbid_pattern, require_pattern, forbid_import, ...
    severity: ReviewSeverity = ReviewSeverity.WARNING
    description: str = ""
    # Rule-specific parameters
    pattern: str = ""  # for forbid_pattern / require_pattern / naming_convention
    message: str = ""  # custom message when rule triggers
    files: list[str] = field(default_factory=list)  # glob patterns to apply to
    max_lines: int = 0  # for max_function_lines
    max_count: int = 0  # for max_new_deps
    # Naming convention
    kind: str = ""  # "function" or "class"


@dataclass
class RulesConfig:
    """Parsed rules configuration."""

    repo: str = ""
    rules: list[Rule] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_rules(repo_path: str) -> RulesConfig:
    """Load and parse ``.code-review-rules.yaml`` from a repo.

    Returns empty config if file doesn't exist.
    """
    rules_path = os.path.join(repo_path, ".code-review-rules.yaml")
    if not os.path.isfile(rules_path):
        return RulesConfig(repo=repo_path)

    try:
        with open(rules_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except Exception:
        logger.exception("Failed to parse %s", rules_path)
        return RulesConfig(repo=repo_path)

    if not raw or not isinstance(raw, dict):
        return RulesConfig(repo=repo_path)

    config = RulesConfig(
        repo=raw.get("repo", repo_path),
    )

    for item in raw.get("rules", []):
        try:
            config.rules.append(Rule(
                name=item.get("name", "unnamed"),
                rule_type=item.get("type", "forbid_pattern"),
                severity=ReviewSeverity(item.get("severity", "warning")),
                description=item.get("description", ""),
                pattern=item.get("pattern", ""),
                message=item.get("message", ""),
                files=item.get("files", []),
                max_lines=item.get("max_lines", 0),
                max_count=item.get("max_count", 0),
                kind=item.get("kind", ""),
            ))
        except Exception:
            logger.warning("Skipping malformed rule: %s", item)

    return config


def check_rules(
    files: list[FileDiff],
    rules_config: RulesConfig,
    repo_path: str = ".",
) -> list[ReviewComment]:
    """Check changed files against loaded rules, return review comments.

    Skips deleted files and non-code files.
    """
    if not rules_config.rules:
        return []

    comments: list[ReviewComment] = []

    for rule in rules_config.rules:
        for fd in files:
            if fd.is_deleted:
                continue
            path = fd.effective_path

            # File filter
            if rule.files and not _matches_any_glob(path, rule.files):
                continue

            # Try disk first, fall back to diff content
            source = ""
            try:
                source = _read_file(repo_path, path)
            except Exception:
                source = _extract_source_from_diff(fd)
                if not source:
                    continue

            if rule.rule_type == "forbid_pattern":
                comments.extend(_check_forbid_pattern(rule, fd, source))
            elif rule.rule_type == "require_pattern":
                comments.extend(_check_require_pattern(rule, fd, source))
            elif rule.rule_type == "forbid_import":
                comments.extend(_check_forbid_import(rule, fd, source))
            elif rule.rule_type == "max_function_lines":
                comments.extend(_check_max_function_lines(rule, fd, source))
            elif rule.rule_type == "naming_convention":
                comments.extend(_check_naming_convention(rule, fd, source))
            elif rule.rule_type == "max_new_deps":
                comments.extend(_check_max_new_deps(rule, [fd]))

    return comments


# ---------------------------------------------------------------------------
# Rule checkers
# ---------------------------------------------------------------------------


def _check_forbid_pattern(rule: Rule, fd: FileDiff, source: str) -> list[ReviewComment]:
    """Block lines matching a forbidden regex."""
    if not rule.pattern:
        return []
    comments: list[ReviewComment] = []
    try:
        pat = re.compile(rule.pattern)
    except re.error:
        logger.warning("Invalid regex in rule '%s': %s", rule.name, rule.pattern)
        return []

    for line_no, line in enumerate(source.split("\n"), 1):
        if pat.search(line):
            comments.append(ReviewComment(
                file_path=fd.effective_path,
                line_start=line_no,
                severity=rule.severity,
                category="maintainability",
                title=f"[rule] {rule.name}",
                body=rule.message or f"禁止的模式匹配: `{rule.pattern}`",
                confidence=ReviewConfidence.LOW,
                source=ReviewSource.SEMGREP,
            ))
    return comments


def _check_require_pattern(rule: Rule, fd: FileDiff, source: str) -> list[ReviewComment]:
    """Require a pattern to exist (e.g., type hints)."""
    if not rule.pattern:
        return []
    try:
        pat = re.compile(rule.pattern)
    except re.error:
        return []

    if not pat.search(source):
        return [ReviewComment(
            file_path=fd.effective_path,
            line_start=1,
            severity=rule.severity,
            category="maintainability",
            title=f"[rule] {rule.name}",
            body=rule.message or f"缺少必要模式: `{rule.pattern}`",
            confidence=ReviewConfidence.LOW,
            source=ReviewSource.SEMGREP,
        )]
    return []


def _check_forbid_import(rule: Rule, fd: FileDiff, source: str) -> list[ReviewComment]:
    """Block forbidden imports."""
    if not rule.pattern:
        return []
    try:
        pat = re.compile(rule.pattern)
    except re.error:
        return []

    comments: list[ReviewComment] = []
    for line_no, line in enumerate(source.split("\n"), 1):
        # Match Python import and TS import
        if ("import " in line or "require(" in line or "from " in line) and pat.search(line):
            comments.append(ReviewComment(
                file_path=fd.effective_path,
                line_start=line_no,
                severity=rule.severity,
                category="maintainability",
                title=f"[rule] {rule.name}",
                body=rule.message or f"禁止引入的依赖: `{rule.pattern}`",
                confidence=ReviewConfidence.LOW,
                source=ReviewSource.SEMGREP,
            ))
    return comments


def _check_max_function_lines(rule: Rule, fd: FileDiff, source: str) -> list[ReviewComment]:
    """Check function length."""
    if not rule.max_lines:
        return []

    comments: list[ReviewComment] = []
    # Find function boundaries
    func_pattern = re.compile(r"^\s*(?:def |function |func |async function )", re.MULTILINE)
    matches = list(func_pattern.finditer(source))
    lines = source.split("\n")

    for i, m in enumerate(matches):
        start = m.start()
        start_line = source[:start].count("\n") + 1

        # End is next function or EOF
        if i + 1 < len(matches):
            end_line = source[:matches[i + 1].start()].count("\n") + 1
        else:
            end_line = len(lines)

        func_len = end_line - start_line
        if func_len > rule.max_lines:
            comments.append(ReviewComment(
                file_path=fd.effective_path,
                line_start=start_line,
                severity=rule.severity,
                category="maintainability",
                title=f"[rule] {rule.name}",
                body=rule.message or f"函数过长 ({func_len} 行, 限制 {rule.max_lines} 行)",
                suggestion=f"建议拆分为多个小函数，每个不超过 {rule.max_lines} 行。",
                confidence=ReviewConfidence.LOW,
                source=ReviewSource.SEMGREP,
            ))
    return comments


def _check_naming_convention(rule: Rule, fd: FileDiff, source: str) -> list[ReviewComment]:
    """Check naming conventions (snake_case, camelCase, etc.)."""
    if not rule.pattern or not rule.kind:
        return []

    try:
        pat = re.compile(rule.pattern)
    except re.error:
        return []

    comments: list[ReviewComment] = []
    if rule.kind == "function":
        def_pat = re.compile(
            r"^\s*(?:def |function |func )(\w+)", re.MULTILINE,
        )
    elif rule.kind == "class":
        def_pat = re.compile(r"^\s*class (\w+)", re.MULTILINE)
    else:
        def_pat = re.compile(r"^\s*(?:def |function |func |class )(\w+)", re.MULTILINE)

    for m in def_pat.finditer(source):
        name = m.group(1)
        if not pat.match(name):
            line_no = source[: m.start()].count("\n") + 1
            comments.append(ReviewComment(
                file_path=fd.effective_path,
                line_start=line_no,
                severity=rule.severity,
                category="style",
                title=f"[rule] {rule.name}",
                body=rule.message or f"命名不符合规范: `{name}` (要求: `{rule.pattern}`)",
                confidence=ReviewConfidence.LOW,
                source=ReviewSource.SEMGREP,
            ))
    return comments


def _check_max_new_deps(rule: Rule, files: list[FileDiff]) -> list[ReviewComment]:
    """Count new dependency additions across changed files."""
    if not rule.max_count:
        return []

    dep_patterns = [
        # Python
        re.compile(r"^\s*(?:import |from )(\w+)"),
        # TypeScript / JavaScript
        re.compile(r"""^\s*(?:import .+ from ["']|require\(["'])"""),
        # Go
        re.compile(r"^\s*\"(.+)\"$"),
    ]

    new_deps: set[str] = set()
    for fd in files:
        if fd.is_deleted:
            continue
        try:
            source = _read_file(".", fd.effective_path)
        except Exception:
            continue
        for pat in dep_patterns:
            for m in pat.finditer(source):
                new_deps.add(m.group(1) if m.lastindex else m.group(0)[:40])

    if len(new_deps) > rule.max_count:
        return [ReviewComment(
            file_path="",
            line_start=None,
            severity=rule.severity,
            category="maintainability",
            title=f"[rule] {rule.name}",
            body=(
                f"本次 PR 新增 {len(new_deps)} 个依赖 "
                f"(限制 {rule.max_count}): "
                + ", ".join(sorted(new_deps)[:10])
            ),
            confidence=ReviewConfidence.LOW,
            source=ReviewSource.SEMGREP,
        )]
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_file(repo_path: str, file_path: str) -> str:
    full = os.path.join(repo_path, file_path)
    with open(full, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _matches_any_glob(file_path: str, globs: list[str]) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(file_path, g) for g in globs)


def _extract_source_from_diff(fd: FileDiff) -> str:
    """Reconstruct source text from a diff's added lines.

    Used as fallback when the file doesn't exist on disk (e.g., new files in synthetic diffs).
    """
    lines: list[str] = []
    for hunk in fd.hunks:
        for ln in hunk.lines:
            if ln.change_type in ("+", " "):
                lines.append(ln.content)
    return "\n".join(lines)
