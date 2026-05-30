"""FastAPI application — webhook endpoint and health check."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import settings
from app.diff_parser import parse_diff
from app.github_client import (
    get_pr_details,
    get_pr_diff_via_api,
    post_review_comments,
)
from app.models import HealthResponse, ReviewComment, WebhookPayload
from app.reviewer import pipeline_review

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("code-review-agent")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Code Review Agent",
    version=__version__,
    description="AI-powered multi-stage code review agent",
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Liveness probe — returns ok when the service is running."""
    return HealthResponse(version=__version__)


@app.post("/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    """Receive GitHub webhook events for pull requests.

    When a PR is opened or synchronized (new commits pushed), the agent
    runs a full code review and posts comments back to the PR.

    Supported events: ``pull_request`` with action ``opened`` or ``synchronize``.
    """
    # --- 1. Verify webhook signature ---
    raw_body = await request.body()
    if settings.webhook_secret:
        if not _verify_signature(raw_body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid signature")

    # --- 2. Filter event type ---
    if x_github_event != "pull_request":
        return JSONResponse(
            {"message": f"Ignored event: {x_github_event}"}, status_code=200
        )

    payload = WebhookPayload(**await request.json())
    if payload.action not in ("opened", "synchronize"):
        return JSONResponse(
            {"message": f"Ignored PR action: {payload.action}"}, status_code=200
        )

    pr = payload.pull_request
    if not pr:
        raise HTTPException(status_code=400, detail="Missing pull_request data")

    # --- 3. Extract PR coordinates ---
    pr_number = pr["number"]
    # pr["base"]["repo"]["full_name"] → "owner/repo"
    owner, repo = pr["base"]["repo"]["full_name"].split("/", 1)
    pr_url = pr.get("html_url", "")

    logger.info("Reviewing PR #%d in %s/%s", pr_number, owner, repo)

    # --- 4. Fetch diff ---
    try:
        diff_text = await get_pr_diff_via_api(owner, repo, pr_number)
    except Exception as exc:
        logger.exception("Failed to fetch diff for PR #%d", pr_number)
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch PR diff: {exc}"
        ) from exc

    if not diff_text.strip():
        return JSONResponse(
            {"message": "Empty diff — nothing to review"}, status_code=200
        )

    # --- 5. Parse diff ---
    files = parse_diff(diff_text)
    logger.info(
        "Parsed %d changed files (%d total hunks)", len(files), sum(len(f.hunks) for f in files)
    )

    # --- 6. Run pipeline review ---
    result = await pipeline_review(files, pr_url=pr_url, repo_path=".")

    # --- 7. Post comments ---
    try:
        pr_details = await get_pr_details(owner, repo, pr_number)
        commit_id = pr_details["head"]["sha"]
    except Exception:
        logger.exception("Failed to fetch PR details, trying base sha")
        commit_id = pr["head"]["sha"]

    comments_payload = _build_comment_payload(result.comments)
    summary_text = _build_summary_body(result)
    await post_review_comments(
        owner, repo, pr_number, commit_id,
        comments=comments_payload,
        summary=summary_text,
    )

    return JSONResponse(
        {
            "message": "Review complete",
            "pr_url": pr_url,
            "stats": result.stats,
            "comment_count": len(result.comments),
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verify_signature(body: bytes, signature_header: str) -> bool:
    """Verify the HMAC-SHA256 webhook signature from GitHub."""
    if not signature_header.startswith("sha256="):
        return False
    expected = signature_header[7:]
    computed = hmac.new(
        settings.webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, expected)


def _build_comment_payload(comments: list[ReviewComment]) -> list[dict]:
    """Convert internal ReviewComment list to GitHub review-comment payload."""
    payload: list[dict] = []
    for c in comments:
        if not c.file_path or c.line_start is None:
            continue
        emoji = {"blocker": "🔴", "warning": "🟡", "suggestion": "💡", "praise": "✅"}
        prefix = emoji.get(c.severity.value if c.severity else "", "")
        body_parts = [f"{prefix} **{c.title}**"]
        if c.category:
            body_parts.insert(0, f"[{c.category}]")
        body_parts.append(f"\n{c.body}")
        if c.suggestion:
            body_parts.append(f"\n\n```suggestion\n{c.suggestion}\n```")

        payload.append({
            "path": c.file_path,
            "line": c.line_start,
            "side": "RIGHT",
            "body": "\n".join(body_parts),
        })
    return payload


def _build_summary_body(result) -> str:
    """Build the review summary text with confidence markers."""
    if not result.comments:
        return f"\U0001f916 **AI Code Review (Pipeline v2)**\n\n{result.summary}"

    parts = [f"\U0001f916 **AI Code Review (Pipeline v2)**\n\n{result.summary}\n"]
    if result.stats:
        stats_line = " | ".join(f"{k}: {v}" for k, v in result.stats.items())
        parts.append(f"\n\U0001f4ca **统计**: {stats_line}")

    # Add confidence distribution
    high = sum(1 for c in result.comments if getattr(c, 'confidence', None) == 'high')
    medium = sum(1 for c in result.comments if getattr(c, 'confidence', None) == 'medium')
    if high > 0:
        parts.append(f"\n\U0001f7e2 双模型交叉验证确认: {high} 条")
    if medium > 0:
        parts.append(f"\n\U0001f7e1 单模型提出: {medium} 条")
    return "\n".join(parts)
