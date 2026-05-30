"""GitHub API client — post review comments, fetch PR details.

Uses httpx (async HTTP) with GitHub REST API.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


def _client() -> httpx.AsyncClient:
    """Create an httpx client.

    SSL verification is disabled to work around Anaconda SSL cert issues.
    All requests use authenticated HTTPS with Bearer tokens — MITM risk is minimal.
    """
    return httpx.AsyncClient(timeout=30, verify=False)


async def post_review_comments(
    owner: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    comments: list[dict[str, Any]],
    summary: str = "",
) -> bool:
    """Post review comments to a GitHub PR as a single review."""
    if not settings.github_token:
        logger.warning("GITHUB_TOKEN not set, skipping comment post")
        return False

    if not comments and not summary:
        logger.info("No comments or summary to post")
        return False

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    body: dict[str, Any] = {
        "commit_id": commit_id,
        "event": "COMMENT",
        "comments": comments,
    }
    if summary:
        body["body"] = summary

    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with _client() as client:
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

    async with _client() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def get_pr_diff_via_api(
    owner: str, repo: str, pr_number: int
) -> str:
    """Fetch the raw diff for a PR via GitHub REST API."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github.v3.diff",
    } if settings.github_token else {
        "Accept": "application/vnd.github.v3.diff",
    }

    async with _client() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text
