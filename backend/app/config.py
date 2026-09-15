from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Env lists are CSV (or JSON); NoDecode stops pydantic-settings JSON-parsing them first.
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    redis_url: str = "redis://localhost:6379/0"

    # Backend-issued session JWT. Separate from the token encryption key on purpose.
    jwt_secret: str = "dev-only-change-me-dev-only-change-me"
    jwt_ttl_seconds: int = 8 * 60 * 60
    jwt_issuer: str = "pr-review-agent"

    # Fernet key for users.github_token_enc. Must NOT equal jwt_secret.
    token_encryption_key: str = ""

    # Shared secret between the Next.js server and FastAPI for /auth/github/exchange.
    auth_bridge_secret: str = "dev-bridge-secret"

    # Sign-in policy (fail closed). Role is derived from these at FIRST sign-in only.
    # Numeric GitHub user ids, never logins: logins can be renamed and re-registered by someone else.
    github_admin_ids: CsvList = Field(default_factory=list)
    github_reviewer_orgs: CsvList = Field(default_factory=list)
    github_viewer_orgs: CsvList = Field(default_factory=list)

    # GitHub App (read-only permissions) used by the agent.
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_app_installation_id: str = ""
    # Dev fallback: a fine-grained read-only token. The transport allowlist applies regardless.
    github_readonly_token: str = ""
    github_api_url: str = "https://api.github.com"
    github_fetch_max_bytes: int = 200_000

    # Proxies whose X-Forwarded-For we believe. IPs or CIDR blocks.
    trusted_proxies: CsvList = Field(default_factory=lambda: ["127.0.0.1/32"])

    # App-layer rate limits (Redis, keyed on user id).
    rate_limit_general_per_minute: int = 600
    review_quota_per_hour_reviewer: int = 20
    review_quota_per_hour_admin: int = 100

    # LLM. One construction point for every caller: app/agent/llm.py.
    #   openai_compatible -> ChatOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)  (default: OpenRouter)
    #   anthropic         -> ChatAnthropic(api_key=LLM_API_KEY or ANTHROPIC_API_KEY)  (direct, for comparison)
    llm_provider: Literal["anthropic", "openai_compatible"] = "openai_compatible"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    anthropic_api_key: str = ""
    # AGENT_MODEL is the name; LLM_MODEL is still accepted. Model ids are provider-specific
    # (OpenRouter: "anthropic/<model>"), so openai_compatible has no default -- see app/agent/llm.py.
    agent_model: str = Field(default="", validation_alias=AliasChoices("AGENT_MODEL", "LLM_MODEL", "agent_model"))
    llm_max_tool_rounds: int = 12
    # Prompt-cache breakpoints on the brief and the newest trimmed stub (app/agent/context.py).
    # Changes cost, never what the model sees.
    llm_prompt_cache: bool = True
    # Context hygiene (OFF by default): when set to N, the N most recent tool results are sent in
    # full and older ones become a stub (tool, args, size); the brief with every patch is never
    # trimmed. It changes what the model sees, so eval reports record it (meta.keep_tool_results)
    # and it is part of the comparison rule. Unset = no trimming + rolling top-level prompt cache.
    llm_keep_tool_results: int | None = Field(default=None, ge=0)
    # OpenRouter provider routing object, sent as the request's "provider" field (JSON), e.g.
    # {"only": ["novita"], "allow_fallbacks": false}. Open-weight models are served by many hosts
    # with different quantization and context limits; pin one for any number you intend to compare.
    llm_provider_routing: dict | None = None
    # 0 by default: at non-zero temperature case-level differences between runs are sampling
    # noise and no A/B is interpretable. Recorded in every eval report (meta.temperature).
    llm_temperature: float = 0.0
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://langfuse-web:3000"

    @field_validator(
        "github_admin_ids", "github_reviewer_orgs", "github_viewer_orgs", "trusted_proxies",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json

                return json.loads(s)
            return [p.strip() for p in s.split(",") if p.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
