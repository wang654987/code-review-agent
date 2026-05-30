"""Pydantic models for request/response and domain objects."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReviewSeverity(str, Enum):
    """审查意见的严重程度。"""

    BLOCKER = "blocker"  # 必须修改才能合并
    WARNING = "warning"  # 强烈建议修改
    SUGGESTION = "suggestion"  # 可选优化
    PRAISE = "praise"  # 写得好的地方


class ReviewComment(BaseModel):
    """单条审查意见。"""

    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    severity: ReviewSeverity
    category: str  # e.g. "bug", "performance", "security", "style", "logic"
    title: str  # 简短标题
    body: str  # 详细说明
    suggestion: str | None = None  # 建议的修改方案（代码）


class ReviewResult(BaseModel):
    """一次完整审查的结果。"""

    pr_url: str
    summary: str  # 审查总结
    comments: list[ReviewComment]
    stats: dict[str, int]  # {"blocker": 0, "warning": 2, "suggestion": 5, "praise": 1}


class WebhookPayload(BaseModel):
    """GitHub webhook 推送的 PR 事件（我们只关心 PR）。"""

    action: str
    pull_request: dict[str, Any] | None = None
    repository: dict[str, Any] = Field(default_factory=dict)
    installation: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
