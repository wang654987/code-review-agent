"""Pipeline-based code review — multi-stage with dual-model cross-validation.

Stage 1: Semgrep static analysis (optional)
Stage 2: Context Builder — AST-based call-chain analysis (optional)
Stage 3: Dual-model semantic review with cross-validation (optional)
Stage 4: Aggregation & deduplication
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import litellm

from app.config import settings
from app.context_builder import ContextGraph, build_context, format_context_for_prompt
from app.diff_parser import FileDiff, format_diff_for_review
from app.models import (
    ReviewComment,
    ReviewConfidence,
    ReviewResult,
    ReviewSeverity,
    ReviewSource,
)
from app.semgrep_runner import run_semgrep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_V2 = """你是一位资深的高级软件工程师，正在进行代码审查（Code Review）。

你的审查风格：
- **精准**：只指出真正有问题的地方，不要吹毛求疵
- **建设性**：每条意见都附带具体的修改建议（代码示例）
- **结构化**：严格按照 JSON 格式输出

审查优先级：
1. **BUG** — 逻辑错误、空指针、边界条件、并发问题
2. **安全** — SQL 注入、XSS、密钥泄露、权限问题
3. **性能** — N+1 查询、不必要的循环、内存泄漏
4. **可维护性** — 命名、重复代码、过度耦合
5. **风格** — 只在严重偏离惯例时提及

输出格式（严格 JSON，不要输出其他内容）：
{
  "summary": "一句话总结本次 PR 的质量和主要问题",
  "comments": [
    {
      "file_path": "app/main.py",
      "line_start": 42,
      "line_end": 42,
      "severity": "warning",
      "category": "bug",
      "title": "简短标题",
      "body": "详细说明问题所在",
      "suggestion": "建议的代码修改方案"
    }
  ]
}

severity 可选值: blocker, warning, suggestion, praise
category 可选值: bug, security, performance, maintainability, style

规则：
- 如果没有发现问题，comments 返回空数组，summary 写"代码质量良好"
- 每个 comment 的 body 要具体，不要泛泛而谈
- suggestion 要给出可直接使用的代码
"""

USER_PROMPT_V2 = """请审查以下 Pull Request 的代码变更。

使用 {language} 撰写审查意见。

{context}

{diff}
"""


# ---------------------------------------------------------------------------
# Public API — Pipeline orchestrator
# ---------------------------------------------------------------------------


async def pipeline_review(
    files: list[FileDiff],
    pr_url: str = "",
    repo_path: str = ".",
    *,
    language: str | None = None,
) -> ReviewResult:
    """Run the full multi-stage review pipeline.

    Stages:
        1. Semgrep static analysis
        2. Context building (AST)
        3. Dual-model LLM review with cross-validation
        4. Aggregation

    Returns:
        Aggregated ReviewResult from all stages.
    """
    lang = language or settings.review_language
    pipeline_info: dict[str, Any] = {"stages": {}}

    # --- Early exit: empty diff ---
    diff_text = format_diff_for_review(files)
    if not diff_text.strip():
        return ReviewResult(
            pr_url=pr_url, summary="无需审查：diff 为空。",
            comments=[], stats={}, pipeline_info=pipeline_info,
        )

    # --- Stage 1: Semgrep ---
    semgrep_comments: list[ReviewComment] = []
    if settings.enable_semgrep:
        try:
            semgrep_comments = run_semgrep(files, repo_path)
            pipeline_info["stages"]["semgrep"] = {
                "status": "ok", "findings": len(semgrep_comments),
            }
        except Exception:
            logger.exception("Semgrep stage failed")
            pipeline_info["stages"]["semgrep"] = {"status": "error"}

    # --- Stage 2: Context building ---
    context = ContextGraph()
    if settings.enable_context_builder:
        try:
            context = build_context(files, repo_path)
            pipeline_info["stages"]["context"] = {
                "status": "ok",
                "symbols": len(context.changed_symbols),
                "call_relations": sum(len(v) for v in context.callers.values()),
            }
        except Exception:
            logger.exception("Context builder stage failed")
            pipeline_info["stages"]["context"] = {"status": "error"}

    context_text = format_context_for_prompt(context)

    # --- Stage 3: LLM review ---
    llm_comments: list[ReviewComment] = []
    if settings.enable_dual_model and settings.llm_model_secondary:
        try:
            llm_comments = await _dual_model_review(diff_text, context_text, lang)
            pipeline_info["stages"]["dual_model"] = {
                "status": "ok", "findings": len(llm_comments),
            }
        except Exception:
            logger.exception("Dual-model review failed, falling back to single")
            try:
                llm_comments = await _single_model_review(diff_text, context_text, lang)
                pipeline_info["stages"]["dual_model"] = {"status": "fallback_single", "findings": len(llm_comments)}
            except Exception:
                logger.exception("Single-model review also failed")
                pipeline_info["stages"]["dual_model"] = {"status": "error"}
    else:
        try:
            llm_comments = await _single_model_review(diff_text, context_text, lang)
            pipeline_info["stages"]["single_model"] = {
                "status": "ok", "findings": len(llm_comments),
            }
        except Exception:
            logger.exception("LLM review failed")
            pipeline_info["stages"]["single_model"] = {"status": "error"}

    # --- Stage 4: Aggregation ---
    all_comments = _deduplicate(semgrep_comments + llm_comments)
    stats = _compute_stats(all_comments)
    summary = _build_pipeline_summary(all_comments, pipeline_info)

    return ReviewResult(
        pr_url=pr_url,
        summary=summary,
        comments=all_comments,
        stats=stats,
        pipeline_info=pipeline_info,
    )


# ---- Backward-compatible single-review entry point ----
async def review_pr(
    files: list[FileDiff],
    pr_url: str = "",
    *,
    language: str | None = None,
) -> ReviewResult:
    """Simple single-model review (Phase 1 API, kept for backward compat)."""
    return await pipeline_review(files, pr_url=pr_url, language=language)


# ---------------------------------------------------------------------------
# Dual-model cross-validation
# ---------------------------------------------------------------------------


async def _dual_model_review(
    diff_text: str, context_text: str, language: str,
) -> list[ReviewComment]:
    """Run two LLMs in parallel, cross-validate, return merged results."""
    primary_prompt = USER_PROMPT_V2.format(
        language=language, diff=diff_text, context=context_text,
    )
    secondary_prompt = USER_PROMPT_V2.format(
        language=language, diff=diff_text, context=context_text,
    )

    # Fire both models concurrently
    primary_task = _call_llm_async(settings.llm_model, settings.llm_api_key, primary_prompt)
    secondary_task = _call_llm_async(
        settings.llm_model_secondary, settings.llm_api_key_secondary, secondary_prompt,
        api_base=settings.llm_api_base_secondary,
    )

    raw_a, raw_b = await asyncio.gather(primary_task, secondary_task, return_exceptions=True)

    # If one fails, use the other
    if isinstance(raw_a, Exception):
        logger.warning("Primary model failed: %s", raw_a)
        if isinstance(raw_b, Exception):
            raise RuntimeError(f"Both models failed: A={raw_a}, B={raw_b}")
        return _parse_and_tag(raw_b, ReviewConfidence.MEDIUM)

    if isinstance(raw_b, Exception):
        logger.warning("Secondary model failed: %s", raw_b)
        return _parse_and_tag(raw_a, ReviewConfidence.MEDIUM)

    # Parse both
    comments_a = _parse_raw_comments(raw_a)
    comments_b = _parse_raw_comments(raw_b)

    logger.info(
        "Cross-validating: model A found %d issues, model B found %d",
        len(comments_a), len(comments_b),
    )

    # Cross-validate: match by (file_path, line_start)
    merged = _cross_validate(comments_a, comments_b)
    logger.info("After cross-validation: %d issues (merged)", len(merged))
    return merged


def _cross_validate(
    primary: list[ReviewComment],
    secondary: list[ReviewComment],
) -> list[ReviewComment]:
    """Merge two review lists with confidence tagging.

    Matching strategy (tried in order):
        1. Exact: same (file_path, line_start) → HIGH
        2. Fuzzy: same file + same category + similar title + nearby line (±5) → HIGH
        3. Unmatched → MEDIUM
    """
    from collections import defaultdict

    merged: list[ReviewComment] = []
    secondary_matched: set[int] = set()  # indices of matched secondary comments

    # --- Pass 1: exact line match ---
    secondary_by_loc: dict[tuple[str, int], int] = {}
    for i, c in enumerate(secondary):
        if c.file_path and c.line_start:
            secondary_by_loc[(c.file_path, c.line_start)] = i

    for c in primary:
        loc = (c.file_path, c.line_start) if c.file_path and c.line_start else None
        if loc and loc in secondary_by_loc:
            c.confidence = ReviewConfidence.HIGH
            secondary_matched.add(secondary_by_loc[loc])

    # --- Pass 2: fuzzy match for unmatched primary comments ---
    # Build index of unmatched secondary: by (file_path, category)
    unmatched_sec: dict[tuple[str, str], list[tuple[int, ReviewComment]]] = defaultdict(list)
    for i, c in enumerate(secondary):
        if i not in secondary_matched and c.file_path:
            unmatched_sec[(c.file_path, c.category)].append((i, c))

    for c in primary:
        if c.confidence == ReviewConfidence.HIGH:
            continue  # already matched
        if not c.file_path or not c.category:
            continue

        candidates = unmatched_sec.get((c.file_path, c.category), [])
        best_idx, best_score = _best_fuzzy_match(c, candidates)
        if best_idx is not None and best_score >= 0.3:
            c.confidence = ReviewConfidence.HIGH
            secondary_matched.add(best_idx)
            # Remove from candidates so it won't be reused
            unmatched_sec[(c.file_path, c.category)] = [
                (i, sc) for i, sc in candidates if i != best_idx
            ]

    # --- Collect results ---
    # All primary (with confidence already set)
    merged.extend(primary)

    # Unmatched secondary
    for i, c in enumerate(secondary):
        if i not in secondary_matched:
            c.confidence = ReviewConfidence.MEDIUM
            merged.append(c)

    return merged


def _best_fuzzy_match(
    primary: ReviewComment,
    candidates: list[tuple[int, ReviewComment]],
) -> tuple[int | None, float]:
    """Find the best fuzzy match among candidates.

    Score = 0.5 × line_proximity + 0.5 × title_similarity
    """
    best_idx: int | None = None
    best_score = 0.0

    for idx, cand in candidates:
        score = 0.0

        # Line proximity: 1 if same line, 0 if > 5 lines apart
        if primary.line_start and cand.line_start:
            line_diff = abs(primary.line_start - cand.line_start)
            line_score = max(0, 1.0 - line_diff / 5.0)
        else:
            line_score = 0.5  # no line info → neutral

        # Title similarity: word overlap
        title_score = _title_similarity(primary.title, cand.title)

        score = 0.5 * line_score + 0.5 * title_score
        if score > best_score:
            best_score = score
            best_idx = idx

    return best_idx, best_score


def _title_similarity(a: str, b: str) -> float:
    """Simple word-level Jaccard similarity between two titles."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Single-model review (fallback / Phase 1 compat)
# ---------------------------------------------------------------------------


async def _single_model_review(
    diff_text: str, context_text: str, language: str,
) -> list[ReviewComment]:
    """Call a single LLM for code review."""
    prompt = USER_PROMPT_V2.format(
        language=language, diff=diff_text, context=context_text,
    )
    raw = await _call_llm_async(settings.llm_model, settings.llm_api_key, prompt)
    return _parse_and_tag(raw, ReviewConfidence.MEDIUM)


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------


async def _call_llm_async(
    model: str, api_key: str, user_prompt: str,
    *, api_base: str = "",
) -> str:
    """Send a prompt to an LLM and return the raw response text."""
    kwargs: dict = {
        "model": model,
        "api_key": api_key,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_V2},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": settings.max_review_tokens,
    }
    if api_base:
        kwargs["api_base"] = api_base

    response = await litellm.acompletion(**kwargs)
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("LLM returned empty response")
    return content


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_raw_comments(raw: str) -> list[ReviewComment]:
    """Parse raw LLM JSON output into ReviewComment list (without confidence tag)."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM JSON: %s", raw[:500])
        return []

    comments: list[ReviewComment] = []
    for item in data.get("comments", []):
        try:
            comments.append(ReviewComment(**item))
        except Exception:
            logger.warning("Skipping malformed comment: %s", item)
    return comments


def _parse_and_tag(raw: str, confidence: ReviewConfidence) -> list[ReviewComment]:
    """Parse raw LLM output and tag all comments with given confidence."""
    comments = _parse_raw_comments(raw)
    for c in comments:
        c.confidence = confidence
    return comments


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _deduplicate(comments: list[ReviewComment]) -> list[ReviewComment]:
    """Remove duplicate comments by (file_path, line_start, category).

    Prefers HIGH confidence over MEDIUM over LOW.
    """
    by_key: dict[tuple[str, int, str], ReviewComment] = {}
    confidence_order = {ReviewConfidence.HIGH: 3, ReviewConfidence.MEDIUM: 2, ReviewConfidence.LOW: 1}

    for c in comments:
        key = (c.file_path, c.line_start or 0, c.category)
        if key in by_key:
            existing = by_key[key]
            if confidence_order.get(c.confidence, 0) > confidence_order.get(existing.confidence, 0):
                by_key[key] = c
        else:
            by_key[key] = c

    return list(by_key.values())


def _compute_stats(comments: list[ReviewComment]) -> dict[str, int]:
    """Compute severity statistics."""
    stats: dict[str, int] = {}
    for c in comments:
        key = c.severity.value if c.severity else "unknown"
        stats[key] = stats.get(key, 0) + 1
    return stats


def _build_pipeline_summary(
    comments: list[ReviewComment], pipeline_info: dict,
) -> str:
    """Build a human-readable pipeline summary."""
    if not comments:
        stages_ok = sum(
            1 for s in pipeline_info.get("stages", {}).values()
            if isinstance(s, dict) and s.get("status") == "ok"
        )
        return f"审查完成：未发现问题（{stages_ok} 个分析阶段通过）。"

    high = sum(1 for c in comments if c.confidence == ReviewConfidence.HIGH)
    medium = sum(1 for c in comments if c.confidence == ReviewConfidence.MEDIUM)
    low = sum(1 for c in comments if c.confidence == ReviewConfidence.LOW)

    parts = [f"审查完成：共发现 {len(comments)} 个问题"]
    if high:
        parts.append(f"（其中 {high} 个经双模型交叉验证确认）")
    parts.append(f"。置信度分布：高={high} / 中={medium} / 低={low}。")
    return "".join(parts)
