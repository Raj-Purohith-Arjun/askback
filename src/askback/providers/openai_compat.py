"""Model providers: any OpenAI-compatible server, OpenAI itself, Azure OpenAI, Anthropic.

The first and most common choice is a server you run yourself (vLLM, Ollama, LiteLLM, a
company gateway). Judging with one keeps every graded reply inside your own network, which
is the point of a test tool that reads internal conversations:

    provider: openai
    base_url: http://localhost:11434/v1     # or JUDGE_OPENAI_BASE_URL
    model: llama3                           # or JUDGE_OPENAI_MODEL

A server at a custom `base_url` needs no API key: none is demanded and no `Authorization`
header is sent unless you set one. OpenAI's own endpoint does need `OPENAI_API_KEY`. Azure
and Anthropic follow.

    AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
    AZURE_OPENAI_API_KEY=...
    AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini

Whose credentials. Every environment variable read here is read as
`{env_prefix}{NAME}`. An agent is built with no prefix and reads the names
above; the judge is built with `JUDGE_` and reads `JUDGE_AZURE_OPENAI_ENDPOINT`
and so on. A setting given explicitly wins over either. Nothing falls back
across the prefix: a judge that quietly graded on the agent's own resource
would be the subject marking its own work.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from ..core.contracts import Message, RetrievedChunk, Role
from ..core.errors import ProviderError
from ..core.logging import get_logger
from .base import LLM_PROVIDERS, LLMProvider, LLMResponse, render_context

log = get_logger(__name__)


def _setting(explicit: str, env_prefix: str, variable: str) -> str:
    """An explicit setting, else `{env_prefix}{variable}` from the environment."""
    return explicit or os.getenv(f"{env_prefix}{variable}", "")


class _ChatCompletionsProvider(LLMProvider):
    """Shared request/response handling for the /chat/completions wire format."""

    remote = True

    def __init__(
        self,
        *,
        model: str = "",
        api_key: str = "",
        temperature: float = 0.0,
        max_tokens: int = 800,
        timeout: float = 60.0,
        env_prefix: str = "",
        **_: Any,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.env_prefix = env_prefix

    # Subclasses supply transport details. ----------------------------------- #

    def _endpoint(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    # ----------------------------------------------------------------------- #

    def generate(
        self,
        messages: Sequence[Message],
        *,
        context: Sequence[RetrievedChunk] = (),
        **options: Any,
    ) -> LLMResponse:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError("Remote providers need httpx. Run: pip install httpx") from exc

        if not self.is_available():
            raise ProviderError(
                f"{self.name}: not configured; missing {sorted(self.missing_settings())}."
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _to_wire(messages, context),
            "temperature": options.get("temperature", self.temperature),
            "max_tokens": options.get("max_tokens", self.max_tokens),
        }
        if options.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}

        try:
            response = httpx.post(
                self._endpoint(), headers=self._headers(), json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - normalise every transport failure
            raise ProviderError(f"{self.name} request failed: {exc}") from exc

        try:
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: unexpected response shape: {body}") from exc

        usage = body.get("usage") or {}
        return LLMResponse(
            text=text.strip(),
            model=body.get("model", self.model),
            finish_reason=choice.get("finish_reason", "stop"),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            raw=body,
        )


@LLM_PROVIDERS.register("azure_openai")
class AzureOpenAIProvider(_ChatCompletionsProvider):
    """Azure OpenAI: deployment-scoped URL, `api-key` header."""

    name = "azure_openai"

    def __init__(
        self,
        *,
        endpoint: str = "",
        deployment: str = "",
        api_version: str = "2024-10-21",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        prefix = self.env_prefix
        self.api_key = _setting(self.api_key, prefix, "AZURE_OPENAI_API_KEY")
        self.endpoint = _setting(endpoint, prefix, "AZURE_OPENAI_ENDPOINT").rstrip("/")
        self.deployment = _setting(deployment, prefix, "AZURE_OPENAI_DEPLOYMENT")
        self.api_version = api_version
        self.model = self.model or self.deployment

    def missing_settings(self) -> dict[str, str]:
        wanted = {
            "endpoint": (self.endpoint, "AZURE_OPENAI_ENDPOINT"),
            "deployment": (self.deployment, "AZURE_OPENAI_DEPLOYMENT"),
            "api_key": (self.api_key, "AZURE_OPENAI_API_KEY"),
        }
        return {
            name: f"{self.env_prefix}{variable}"
            for name, (value, variable) in wanted.items()
            if not value
        }

    def _endpoint(self) -> str:
        return (
            f"{self.endpoint}/openai/deployments/{self.deployment}"
            f"/chat/completions?api-version={self.api_version}"
        )

    def _headers(self) -> dict[str, str]:
        return {"api-key": self.api_key, "content-type": "application/json"}


#: OpenAI's own endpoint and its default model. Anything else is "self-hosted" (see below).
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


@LLM_PROVIDERS.register("openai")
class OpenAIProvider(_ChatCompletionsProvider):
    """OpenAI, or any gateway that speaks the same wire format (vLLM, Ollama, LiteLLM)."""

    name = "openai"

    def __init__(self, *, base_url: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        prefix = self.env_prefix
        self.api_key = _setting(self.api_key, prefix, "OPENAI_API_KEY")
        url = _setting(base_url, prefix, "OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL
        self.base_url = url.rstrip("/")
        # A server at your own URL is not OpenAI: it has no key to demand and no model we can
        # name for you. OpenAI's endpoint keeps its default model and needs its key.
        self.self_hosted = self.base_url != DEFAULT_OPENAI_BASE_URL
        self.model = _setting(self.model, prefix, "OPENAI_MODEL") or (
            "" if self.self_hosted else DEFAULT_OPENAI_MODEL
        )

    def missing_settings(self) -> dict[str, str]:
        missing: dict[str, str] = {}
        if self.self_hosted and not self.model:
            missing["model"] = f"{self.env_prefix}OPENAI_MODEL"
        if not self.self_hosted and not self.api_key:
            missing["api_key"] = f"{self.env_prefix}OPENAI_API_KEY"
        return missing

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


@LLM_PROVIDERS.register("anthropic")
class AnthropicProvider(LLMProvider):
    """Claude via the Messages API. Separate class: different wire format."""

    name = "anthropic"
    remote = True

    def __init__(
        self,
        *,
        model: str = "",
        api_key: str = "",
        base_url: str = "https://api.anthropic.com/v1",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        env_prefix: str = "",
        **_: Any,
    ) -> None:
        # No default model: an id shipped here goes stale, and one that does not exist fails on
        # the first call. Name the model you mean.
        self.model = _setting(model, env_prefix, "ANTHROPIC_MODEL")
        self.env_prefix = env_prefix
        self.api_key = _setting(api_key, env_prefix, "ANTHROPIC_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    def missing_settings(self) -> dict[str, str]:
        missing: dict[str, str] = {}
        if not self.api_key:
            missing["api_key"] = f"{self.env_prefix}ANTHROPIC_API_KEY"
        if not self.model:
            missing["model"] = f"{self.env_prefix}ANTHROPIC_MODEL"
        return missing

    def generate(
        self,
        messages: Sequence[Message],
        *,
        context: Sequence[RetrievedChunk] = (),
        **options: Any,
    ) -> LLMResponse:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError("Remote providers need httpx. Run: pip install httpx") from exc

        if not self.is_available():
            raise ProviderError(
                f"anthropic: not configured; missing {sorted(self.missing_settings())}."
            )

        wire = _to_wire(messages, context)
        # The Messages API takes the system prompt as a top-level field.
        system = "\n\n".join(m["content"] for m in wire if m["role"] == "system")
        turns = [m for m in wire if m["role"] != "system"] or [
            {"role": "user", "content": ""}
        ]

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": options.get("max_tokens", self.max_tokens),
            "temperature": options.get("temperature", self.temperature),
            "messages": turns,
        }
        if system:
            payload["system"] = system

        try:
            response = httpx.post(
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - normalise every transport failure
            raise ProviderError(f"anthropic request failed: {exc}") from exc

        text = "".join(
            block.get("text", "")
            for block in body.get("content", [])
            if block.get("type") == "text"
        )
        usage = body.get("usage") or {}
        return LLMResponse(
            text=text.strip(),
            model=body.get("model", self.model),
            finish_reason=body.get("stop_reason", "stop"),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            raw=body,
        )


def _to_wire(
    messages: Sequence[Message], context: Sequence[RetrievedChunk]
) -> list[dict[str, str]]:
    """Convert contract messages to the vendor list, injecting the CONTEXT block.

    Context is appended to the system message so retrieved text is never mistaken
    for user instructions: that separation is the prompt-injection defence.
    """
    wire = [
        {"role": m.role.value, "content": m.content}
        for m in messages
        if m.role in (Role.SYSTEM, Role.USER, Role.ASSISTANT)
    ]
    if not context:
        return wire

    block = render_context(context)
    for entry in wire:
        if entry["role"] == "system":
            entry["content"] = f"{entry['content']}\n\n{block}"
            return wire
    return [{"role": "system", "content": block}, *wire]
