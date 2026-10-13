"""The simulator's model: the offline provider, and how the agent picks one.

Importing this package registers the offline `extractive` provider, which is the
simulator's own. The remote providers are framework-side (`askback.providers`)
and are imported by name when config asks for one, not here in passing.
"""

from __future__ import annotations

from typing import Any

from askback.core.logging import get_logger
from askback.providers.base import load_provider
from askback.providers.openai_compat import AnthropicProvider, AzureOpenAIProvider, OpenAIProvider

from .base import LLMProvider, LLMResponse
from .extractive import ExtractiveProvider
from .prompts import PROMPT_VERSION, build_system_prompt, render_context

log = get_logger(__name__)


def build_provider(config: dict[str, Any] | None = None) -> LLMProvider:
    """Instantiate the provider named in config, falling back when unavailable.

    Falling back rather than raising is deliberate: a missing API key should
    degrade the answer quality, never take the service down mid-run.
    """
    settings = dict(config or {})
    requested = str(settings.pop("provider", "extractive"))
    fallback = str(settings.pop("fallback", "extractive"))

    provider: LLMProvider = load_provider(requested)(**settings)

    if provider.is_available():
        return provider

    log.warning(
        "LLM provider %r is not available (missing credentials?); using %r instead.",
        requested,
        fallback,
    )
    return load_provider(fallback)()


__all__ = [
    "PROMPT_VERSION",
    "AnthropicProvider",
    "AzureOpenAIProvider",
    "ExtractiveProvider",
    "OpenAIProvider",
    "LLMProvider",
    "LLMResponse",
    "build_provider",
    "build_system_prompt",
    "render_context",
]
