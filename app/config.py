"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration for the Code Review Agent."""

    # GitHub
    github_token: str = ""
    webhook_secret: str = ""

    # LLM
    llm_model: str = "anthropic/claude-sonnet-4-20250514"
    llm_api_key: str = ""

    # Behaviour
    review_language: str = "zh"
    port: int = 8000
    log_level: str = "INFO"

    # Limits
    max_diff_lines: int = 2000  # 超过此行数的 diff 拒绝审查
    max_review_tokens: int = 4096

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
