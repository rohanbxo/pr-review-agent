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
    temperature: float = 0.0

    def describe(self) -> dict[str, Any]:
        """Safe to log and to write into reports: never includes the key."""
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url,
                "temperature": self.temperature, "api_key_set": bool(self.api_key)}


def resolve_llm_config(
    *, provider: str | None = None, model: str | None = None, temperature: float | None = None,
    require_key: bool = True,
) -> LLMConfig:
    """Settings, optionally overridden per call (eval ``--provider`` / ``--model`` / ``--temperature``)."""
    s = get_settings()
    temp = s.llm_temperature if temperature is None else temperature
    if not 0.0 <= temp <= 1.0:
        raise LLMConfigError(f"temperature must be within [0, 1], got {temp}")
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
    return LLMConfig(provider=prov, model=chosen, base_url=base_url, api_key=key,  # type: ignore[arg-type]
                     temperature=temp)


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
            temperature=cfg.temperature,
            max_tokens=_MAX_TOKENS,
            max_retries=_MAX_RETRIES,
            timeout=_TIMEOUT_S,
            **kwargs,
        )

    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=cfg.model,
        api_key=cfg.api_key or None,
        temperature=cfg.temperature,
        max_tokens=_MAX_TOKENS,
        max_retries=_MAX_RETRIES,
        default_request_timeout=_TIMEOUT_S,
    )


def get_llm() -> BaseChatModel:
    """The review model as configured in settings (used by the background runner)."""
    return build_llm(resolve_llm_config())


CACHE_CONTROL = {"type": "ephemeral"}


def with_conversation_cache(runnable: Runnable, llm: BaseChatModel) -> Runnable:
    """Bind top-level ``cache_control`` so the provider caches the WHOLE request prefix up to the
    last cacheable block -- tools, system, brief and every tool result so far. Each tool round then
    reads everything before it from cache and writes only what it added. Nothing the model sees
    changes: no message content is edited, only a request-level field is added.

    Verified on OpenRouter -> anthropic/claude-haiku-4.5 (Amazon Bedrock) by probe, 2026-09-16:
    * top-level ``cache_control`` alone rolls: call 2 read all 23,159 tokens of call 1, wrote 1,866;
    * top-level combined with an explicit block breakpoint does NOT roll (only the explicit block
      was cached), so no explicit breakpoints are used anywhere;
    * a request with a different tool list (synthesize) reads nothing from this cache, so bind
      this to the analyze calls only -- on synthesize it would buy a 1.25x write nobody reads.

    ChatOpenAI sends it via ``extra_body`` (OpenRouter's top-level field). ChatAnthropic accepts a
    ``cache_control`` call kwarg: top-level on the direct API, expanded to the last eligible block
    on other transports. Other chat models (test fakes) are returned unchanged.
    LLM_PROMPT_CACHE=false returns ``runnable`` unchanged, i.e. the exact uncached request."""
    if not get_settings().llm_prompt_cache:
        return runnable
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:  # pragma: no cover
        ChatOpenAI = None  # type: ignore[assignment]
    from langchain_anthropic import ChatAnthropic

    if ChatOpenAI is not None and isinstance(llm, ChatOpenAI):
        return runnable.bind(extra_body={"cache_control": CACHE_CONTROL})
    if isinstance(llm, ChatAnthropic):
        return runnable.bind(cache_control=CACHE_CONTROL)
    return runnable


def message_text(message: Any) -> str:
    """The text of a message whether its content is a string or a list of content blocks."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content or [])


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
