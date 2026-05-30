"""Semgrep static analysis integration.

Runs semgrep against changed files and converts findings into
ReviewComment format.  Falls back gracefully when semgrep is not installed.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from app.config import settings
from app.diff_parser import FileDiff
from app.models import ReviewComment, ReviewConfidence, ReviewSeverity, ReviewSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity mapping: semgrep → our model
# ---------------------------------------------------------------------------

_SEMGREP_SEVERITY_MAP: dict[str, ReviewSeverity] = {
    "ERROR": ReviewSeverity.BLOCKER,
    "WARNING": ReviewSeverity.WARNING,
    "INFO": ReviewSeverity.SUGGESTION,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_semgrep(
    files: list[FileDiff],
    repo_path: str = ".",
) -> list[ReviewComment]:
    """Run semgrep on changed files and return review comments.

    Args:
        files: Parsed diff files.
        repo_path: Path to repository root.

    Returns:
        List of ReviewComment from semgrep findings.
    """
    if not settings.enable_semgrep:
        return []

    if not _semgrep_available():
        logger.info("semgrep not installed — skipping static analysis stage")
        return []

    # Collect changed Python files
    changed_paths = [
        fd.effective_path for fd in files
        if not fd.is_deleted and fd.effective_path.endswith(".py")
    ]
    if not changed_paths:
        return []

    try:
        raw = _invoke_semgrep(changed_paths, repo_path)
        return _parse_semgrep_output(raw)
    except FileNotFoundError:
        logger.info("semgrep not found — skipping")
        return []
    except Exception:
        logger.exception("semgrep run failed")
        return []


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _semgrep_available() -> bool:
    """Check if semgrep is installed and usable."""
    try:
        subprocess.run(
            ["semgrep", "--version"],
            capture_output=True, timeout=5,
        )
        return True
    except Exception:
        return False


def _invoke_semgrep(paths: list[str], repo_path: str) -> str:
    """Run semgrep and return JSON output."""
    config = settings.semgrep_config
    cmd = [
        "semgrep",
        "--config", config,
        "--json",
        "--no-git-ignore",
        "--quiet",
        *paths,
    ]
    logger.debug("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True, text=True,
        timeout=settings.semgrep_timeout,
        cwd=repo_path,
    )
    # semgrep exits non-zero when findings exist — that's expected
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def _parse_semgrep_output(raw: str) -> list[ReviewComment]:
    """Parse semgrep JSON output into ReviewComment list."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse semgrep JSON output")
        return []

    results = data.get("results", [])
    comments: list[ReviewComment] = []

    for finding in results:
        severity = _SEMGREP_SEVERITY_MAP.get(
            finding.get("extra", {}).get("severity", "WARNING").upper(),
            ReviewSeverity.WARNING,
        )
        rule_id = finding.get("check_id", "unknown")
        message = finding.get("extra", {}).get("message", "")
        path = finding.get("path", "")
        line = finding.get("start", {}).get("line", None)

        # Category from rule prefix
        category = "maintainability"
        if "security" in rule_id.lower():
            category = "security"
        elif "bug" in rule_id.lower() or "correctness" in rule_id.lower():
            category = "bug"
        elif "performance" in rule_id.lower():
            category = "performance"
        elif "style" in rule_id.lower():
            category = "style"

        if not path or line is None:
            continue

        comments.append(
            ReviewComment(
                file_path=path,
                line_start=line,
                line_end=finding.get("end", {}).get("line"),
                severity=severity,
                category=category,
                title=f"[semgrep] {rule_id}",
                body=_clean_semgrep_message(message),
                suggestion=None,
                confidence=ReviewConfidence.LOW,
                source=ReviewSource.SEMGREP,
            )
        )

    logger.info("Semgrep found %d issues", len(comments))
    return comments


def _clean_semgrep_message(msg: str) -> str:
    """Clean up semgrep message — strip trailing whitespace, dedent."""
    import textwrap
    return textwrap.dedent(msg).strip()
