"""Feedback Loop — learn from human reviewer decisions.

Tracks which AI review comments were accepted or rejected by human reviewers.
Uses this data to:
    1. Show accuracy stats in review summaries
    2. Adjust review confidence over time (future)

Stores feedback in a local SQLite database at ``.review_feedback.db``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models import ReviewComment

logger = logging.getLogger(__name__)

_DB_PATH = ".review_feedback.db"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class FeedbackEntry:
    comment_hash: str
    accepted: bool
    category: str
    severity: str
    timestamp: float


@dataclass
class FeedbackStats:
    total_reviews: int = 0
    total_comments: int = 0
    accepted: int = 0
    rejected: int = 0
    accuracy: float = 0.0  # accepted / total
    by_category: dict[str, dict[str, int]] = None  # noqa: RUF009

    def __post_init__(self):
        if self.by_category is None:
            self.by_category = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create the feedback database if it doesn't exist."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_hash TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0,
                category TEXT NOT NULL DEFAULT '',
                severity TEXT NOT NULL DEFAULT '',
                timestamp REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_hash
            ON feedback(comment_hash)
        """)
        conn.commit()


def record_feedback(comment: ReviewComment, accepted: bool) -> None:
    """Record a human reviewer's decision on a review comment."""
    h = _hash_comment(comment)
    init_db()

    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO feedback (comment_hash, accepted, category, severity, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (h, int(accepted), comment.category, comment.severity.value, time.time()),
        )
        conn.commit()

    logger.info(
        "Feedback recorded: hash=%s accepted=%s category=%s",
        h[:8], accepted, comment.category,
    )


def get_stats() -> FeedbackStats:
    """Get aggregate feedback statistics."""
    init_db()
    stats = FeedbackStats()

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN accepted=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN accepted=0 THEN 1 ELSE 0 END) "
            "FROM feedback"
        ).fetchone()

        if row and row[0]:
            stats.total_comments = row[0]
            stats.accepted = row[1] or 0
            stats.rejected = row[2] or 0
            stats.accuracy = stats.accepted / stats.total_comments if stats.total_comments > 0 else 0.0

        # By category
        cat_rows = conn.execute(
            "SELECT category, "
            "SUM(CASE WHEN accepted=1 THEN 1 ELSE 0 END) as acc, "
            "SUM(CASE WHEN accepted=0 THEN 1 ELSE 0 END) as rej "
            "FROM feedback GROUP BY category"
        ).fetchall()
        for cat, acc, rej in cat_rows:
            stats.by_category[cat] = {"accepted": acc or 0, "rejected": rej or 0}

    return stats


def format_feedback_summary(stats: FeedbackStats) -> str:
    """Format feedback stats as a human-readable string for review summaries."""
    if stats.total_comments == 0:
        return ""

    parts = [
        "\n### 历史反馈统计",
        f"- 累计审查意见: {stats.total_comments} 条",
        f"- 采纳率: {stats.accuracy:.0%} ({stats.accepted}/{stats.total_comments})",
    ]
    if stats.by_category:
        parts.append("\n按分类:")
        for cat, counts in sorted(stats.by_category.items()):
            total = counts["accepted"] + counts["rejected"]
            rate = counts["accepted"] / total if total > 0 else 0
            parts.append(f"  - {cat}: {rate:.0%} ({counts['accepted']}/{total})")

    return "\n".join(parts)


def get_comment_score(comment: ReviewComment) -> float:
    """Get a quality score (0–1) for a comment based on past feedback.

    Used to filter low-quality comment types that are frequently rejected.
    """
    init_db()
    h = _hash_comment(comment)

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT accepted FROM feedback WHERE category=?",
            (comment.category,),
        ).fetchall()

    if not rows:
        return 0.5  # No feedback yet → neutral

    accepted = sum(1 for (a,) in rows if a)
    return accepted / len(rows)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _hash_comment(comment: ReviewComment) -> str:
    """Create a stable hash for a review comment based on its key fields."""
    key = json.dumps({
        "file_path": comment.file_path,
        "line_start": comment.line_start,
        "category": comment.category,
        "title": comment.title,
    }, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection to the feedback database."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
