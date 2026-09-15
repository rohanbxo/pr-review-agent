"""The ONE place a review model is constructed. The graph runner (``get_llm``) and the eval
adapter (``resolve_llm_config`` + ``build_llm``) both come through here.

Providers:

* ``openai_compatible`` (default) -- ``ChatOpenAI`` pointed at ``LLM_BASE_URL`` (default
  OpenRouter) with ``LLM_API_KEY``. Model ids are the gateway's, e.g. OpenRouter's
  ``anthropic/<model>``.
* ``anthropic`` -- ``ChatAnthropic`` direct, with ``LLM_API_KEY`` (or ``ANTHROPIC_API_KEY``), so a
  direct-Anthropic run stays possible for comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from app.config import get_settings

Provider = Literal["anthropic", "openai_compatible"]
PROVIDERS: tuple[str, ...] = ("anthropic", "openai_compatible")
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5"

_MAX_TOKENS = 8192
_TIMEOUT_S = 120
_MAX_RETRIES = 2


class LLMConfigError(ValueError):
    """The configured provider/model/key cannot build a working client."""


@dataclass(frozen=True)
class LLMConfig:
    provider: Provider
    model: str
    base_url: str | None
    api_key: str

    def describe(self) -> dict[str, Any]:
        """Safe to log and to write into reports: never includes the key."""
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url,
                "api_key_set": bool(self.api_key)}


def resolve_llm_config(
    *, provider: str | None = None, model: str | None = None, require_key: bool = True,
) -> LLMConfig:
    """Settings, optionally overridden per call (eval ``--provider`` / ``--model``)."""
    s = get_settings()
    prov = provider or s.llm_provider
    if prov not in PROVIDERS:
        raise LLMConfigError(f"unknown LLM provider {prov!r}; expected one of {', '.join(PROVIDERS)}")
    chosen = model or s.agent_model
    if prov == "anthropic":
        chosen = chosen or ANTHROPIC_DEFAULT_MODEL
        key = s.llm_api_key or s.anthropic_api_key
        base_url = None
    else:
        if not chosen:
            raise LLMConfigError("AGENT_MODEL is not set. openai_compatible has no default model because ids "
                                 "are gateway-specific (OpenRouter: 'anthropic/<model>').")
        key = s.llm_api_key
        base_url = s.llm_base_url
        if not base_url:
            raise LLMConfigError("LLM_BASE_URL is not set for provider openai_compatible")
    if require_key and not key:
        env = "LLM_API_KEY" + (" (or ANTHROPIC_API_KEY)" if prov == "anthropic" else "")
        raise LLMConfigError(f"{env} is not set for provider {prov}")
    return LLMConfig(provider=prov, model=chosen, base_url=base_url, api_key=key)  # type: ignore[arg-type]


def configured_model_label() -> str:
    """What to record on a queued run before the model is built: never raises, needs no key."""
    try:
        return resolve_llm_config(require_key=False).model
    except LLMConfigError:
        return f"{get_settings().llm_provider}:unconfigured"


def default_http_async_client() -> httpx.AsyncClient | None:
    """Hook for offline verification: tests replace this to serve recorded/stub responses.
    ``None`` means the SDK builds its own client."""
    return None


def build_llm(cfg: LLMConfig) -> BaseChatModel:
    if cfg.provider == "openai_compatible":
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {}
        http_client = default_http_async_client()
        if http_client is not None:
            kwargs["http_async_client"] = http_client
        return ChatOpenAI(
            model=cfg.model,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            max_tokens=_MAX_TOKENS,
            max_retries=_MAX_RETRIES,
            timeout=_TIMEOUT_S,
            **kwargs,
        )

    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=cfg.model,
        api_key=cfg.api_key or None,
        max_tokens=_MAX_TOKENS,
        max_retries=_MAX_RETRIES,
        default_request_timeout=_TIMEOUT_S,
    )


def get_llm() -> BaseChatModel:
    """The review model as configured in settings (used by the background runner)."""
    return build_llm(resolve_llm_config())


def structured_output(llm: BaseChatModel, schema: type) -> Runnable:
    """``with_structured_output(schema, include_raw=True)`` with the same method on every provider.

    ChatOpenAI defaults to ``json_schema`` (response_format), which OpenAI-compatible gateways
    support unevenly across models. Forced tool calling (``function_calling``) is what
    ChatAnthropic uses by default and what OpenRouter supports broadly, so both providers go
    through the same mechanism and parse failures are comparable across runs."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:  # pragma: no cover
        ChatOpenAI = None  # type: ignore[assignment]
    if ChatOpenAI is not None and isinstance(llm, ChatOpenAI):
        return llm.with_structured_output(schema, include_raw=True, method="function_calling")
    return llm.with_structured_output(schema, include_raw=True)
