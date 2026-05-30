"""Core review logic: send diff to LLM and parse structured review comments."""

from __future__ import annotations

import json
import logging

import litellm

from app.config import settings
from app.diff_parser import FileDiff, format_diff_for_review
from app.models import ReviewComment, ReviewResult, ReviewSeverity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM prompt template
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一位资深的高级软件工程师，正在进行代码审查（Code Review）。

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

USER_PROMPT_TEMPLATE = """请审查以下 Pull Request 的代码变更。

使用 {language} 撰写审查意见。

{diff}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def review_pr(
    files: list[FileDiff],
    pr_url: str = "",
    *,
    language: str | None = None,
) -> ReviewResult:
    """Run a full review on a parsed diff.

    Args:
        files: Parsed diff files from ``diff_parser.parse_diff()``.
        pr_url: PR URL for logging / result metadata.
        language: Language for review comments (default: settings.review_language).

    Returns:
        Structured review result with comments and statistics.
    """
    lang = language or settings.review_language

    # 1. Format diff
    diff_text = format_diff_for_review(files)
    if not diff_text.strip():
        return ReviewResult(
            pr_url=pr_url,
            summary="无需审查：diff 为空。",
            comments=[],
            stats={},
        )

    # 2. Check limits
    lines = diff_text.count("\n")
    if lines > settings.max_diff_lines:
        logger.warning("Diff too large: %d lines (max %d)", lines, settings.max_diff_lines)
        return ReviewResult(
            pr_url=pr_url,
            summary=f"Diff 过大（{lines} 行），跳过自动审查。建议人工 review。",
            comments=[],
            stats={},
        )

    # 3. Call LLM
    try:
        raw = await _call_llm(diff_text, lang)
        result = _parse_llm_response(raw, pr_url)
        logger.info(
            "Review complete: %d comments (%s)",
            len(result.comments),
            result.stats,
        )
        return result
    except Exception:
        logger.exception("LLM review failed")
        return ReviewResult(
            pr_url=pr_url,
            summary="审查失败：LLM 调用出错。请检查 API key 和网络连接。",
            comments=[],
            stats={},
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _call_llm(diff_text: str, language: str) -> str:
    """Send diff to LLM and get raw response text."""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        language=language,
        diff=diff_text,
    )

    response = await litellm.acompletion(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=settings.max_review_tokens,
    )

    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("LLM returned empty response")
    return content


def _parse_llm_response(raw: str, pr_url: str) -> ReviewResult:
    """Parse LLM JSON response into a ReviewResult, with error handling."""
    # LLM 有时会在 JSON 外面包 markdown code fences — 剥掉
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # 移除 ```json ... ``` 包裹
        cleaned = cleaned.split("\n", 1)[-1]  # 去掉第一行 ```
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM JSON: %s", raw[:500])
        return ReviewResult(
            pr_url=pr_url,
            summary="审查结果解析失败，原始输出见日志。",
            comments=[],
            stats={},
        )

    # Parse comments
    comments: list[ReviewComment] = []
    for item in data.get("comments", []):
        try:
            comments.append(ReviewComment(**item))
        except Exception:
            logger.warning("Skipping malformed comment: %s", item)

    # Compute stats
    stats: dict[str, int] = {}
    for c in comments:
        key = c.severity.value if c.severity else "unknown"
        stats[key] = stats.get(key, 0) + 1

    return ReviewResult(
        pr_url=pr_url,
        summary=data.get("summary", "审查完成。"),
        comments=comments,
        stats=stats,
    )
