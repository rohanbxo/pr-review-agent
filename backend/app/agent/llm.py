from langchain_core.language_models import BaseChatModel

from app.config import get_settings


def get_llm() -> BaseChatModel:
    """The review model (Anthropic via langchain-anthropic), configured from settings."""
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    kwargs = {}
    if settings.anthropic_api_key:
        kwargs["api_key"] = settings.anthropic_api_key
    return ChatAnthropic(
        model=settings.llm_model,
        max_tokens=8192,
        max_retries=2,
        default_request_timeout=120,
        **kwargs,
    )
