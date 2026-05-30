"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration for the Code Review Agent."""

    # GitHub
    github_token: str = ""
    webhook_secret: str = ""

    # LLM — primary model
    llm_model: str = "deepseek/deepseek-chat"
    llm_api_key: str = ""

    # LLM — secondary model (for cross-validation; optional)
    llm_model_secondary: str = ""
    llm_api_key_secondary: str = ""
    llm_api_base_secondary: str = ""  # for custom OpenAI-compatible endpoints

    # Behaviour
    review_language: str = "zh"
    port: int = 8000
    log_level: str = "INFO"

    # Pipeline stages (can be toggled on/off)
    enable_semgrep: bool = True
    enable_context_builder: bool = True
    enable_dual_model: bool = True  # requires llm_model_secondary

    # Limits
    max_diff_lines: int = 2000
    max_review_tokens: int = 4096

    # Semgrep
    semgrep_config: str = "auto"  # "auto" = semgrep built-in rules, or path to rules dir
    semgrep_timeout: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
