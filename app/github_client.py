"""GitHub API client — post review comments, fetch PR details.

Uses httpx (async HTTP) with GitHub REST API.
Falls back to ``gh`` CLI when a token is not configured.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


async def post_review_comments(
    owner: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    comments: list[dict[str, Any]],
    summary: str = "",
) -> bool:
    """Post review comments to a GitHub PR as a single review.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: PR number.
        commit_id: The SHA of the latest commit on the PR (required for review API).
        comments: List of review comment dicts with keys:
            path, line (int), side ("RIGHT"), body (str)
        summary: Optional summary body for the review.

    Returns:
        True if at least one comment is posted (or summary-only review succeeds).
    """
    if not settings.github_token:
        logger.warning("GITHUB_TOKEN not set, skipping comment post")
        return False

    if not comments and not summary:
        logger.info("No comments or summary to post")
        return False

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

    body: dict[str, Any] = {
        "commit_id": commit_id,
        "event": "COMMENT",  # 不 approve 也不 request changes
        "comments": comments,
    }
    if summary:
        body["body"] = summary

    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body, headers=headers)

    if resp.is_success:
        logger.info("Review posted successfully to PR #%d", pr_number)
        return True
    else:
        logger.error(
            "Failed to post review: %d %s — %s",
            resp.status_code,
            resp.reason_phrase,
            resp.text[:500],
        )
        return False


async def get_pr_details(
    owner: str, repo: str, pr_number: int
) -> dict[str, Any]:
    """Fetch PR metadata: title, head SHA, base branch, etc."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    } if settings.github_token else {}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def get_pr_diff_via_api(
    owner: str, repo: str, pr_number: int
) -> str:
    """Fetch the raw diff for a PR via GitHub REST API (no gh CLI needed)."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github.v3.diff",
    } if settings.github_token else {
        "Accept": "application/vnd.github.v3.diff",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text
