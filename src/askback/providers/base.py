"""The model seam: what a text-generating model looks like to everything else.

Framework-side on purpose. The judge needs a model and so may an agent, but the
judge must be buildable with no agent installed, so the contract, the registry
and the remote providers live here and import nothing from the simulator.

`LLMProvider` is a Protocol: a provider implements a shape. The built-in ones
subclass it to inherit the defaults, which a Protocol allows; a team wrapping
their own client need not.

To add a provider:

    from askback.providers import LLM_PROVIDERS, LLMProvider, LLMResponse

    @LLM_PROVIDERS.register("my-model")
    class MyProvider(LLMProvider):
        name = "my-model"
        remote = True

        def __init__(self, *, env_prefix: str = "", **settings) -> None: ...
        def generate(self, messages, *, context=(), **options) -> LLMResponse: ...

Registration happens when the defining module is imported, and nothing imports
a provider module in passing: `load_provider` imports the one that was asked
for, by name. A registry filled as a side effect of importing some other package
is a registry whose contents depend on who happened to run first.
"""

from __future__ import annotations

import importlib
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from ..core.contracts import Message, RetrievedChunk
from ..core.registry import Registry


@dataclass(slots=True)
class LLMResponse:
    """Normalised model output. Providers translate their vendor payload into this."""

    text: str
    model: str = ""
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Everything the caller might want that the contract does not model.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMProvider(Protocol):
    """Contract for text generation.

    `context` carries the passages the answer must be grounded in. Grounded
    providers read it directly; prompt-based providers render it into the prompt
    (see `render_context`).

    A provider that reads credentials from the environment takes an
    `env_prefix` keyword and reads `{env_prefix}{VARIABLE}`. That is what keeps
    two users of one provider class - an agent and the judge grading it - on
    separate credentials: the judge is built with `JUDGE_`, and a provider that
    cannot take the keyword cannot be a judge.
    """

    name: ClassVar[str] = "base"
    #: Whether this provider needs network access. Used to skip it offline.
    remote: ClassVar[bool] = False

    @abstractmethod
    def generate(
        self,
        messages: Sequence[Message],
        *,
        context: Sequence[RetrievedChunk] = (),
        **options: Any,
    ) -> LLMResponse:
        """Produce a reply for `messages`, grounded in `context` when supplied."""

    def missing_settings(self) -> dict[str, str]:
        """Settings this provider cannot run without and does not have.

        Setting name -> the environment variable that would also supply it, so a
        caller can say exactly what to set. Empty when the provider is ready.
        Names only, never values.
        """
        return {}

    def is_available(self) -> bool:
        """False when the provider cannot run (missing key, no network)."""
        return not self.missing_settings()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"


# `Any`, not `type[LLMProvider]`: `register` is used as a class decorator, and a
# registry typed to the Protocol would hand every decorated class back as the
# bare Protocol, losing its constructor. `load_provider` restores the type.
LLM_PROVIDERS: Registry[Any] = Registry("llm provider")

#: Built-in provider name -> the module, in this package, that defines it.
BUILTIN_PROVIDER_MODULES: Mapping[str, str] = {
    "azure_openai": "openai_compat",
    "openai": "openai_compat",
    "anthropic": "openai_compat",
}


def load_provider(name: str) -> type[LLMProvider]:
    """The provider class registered as `name`, importing its module if need be.

    A name already registered - by whoever defined it - is returned as is. A
    built-in name has its module imported here, explicitly. Anything else raises
    `RegistryError` listing what is registered.
    """
    key = name.strip().lower()
    if key not in LLM_PROVIDERS and key in BUILTIN_PROVIDER_MODULES:
        importlib.import_module(f"{__package__}.{BUILTIN_PROVIDER_MODULES[key]}")
    provider_cls: type[LLMProvider] = LLM_PROVIDERS.get(key)
    return provider_cls


def render_context(chunks: Sequence[RetrievedChunk], *, max_chars: int = 6000) -> str:
    """Format retrieved passages as a CONTEXT block for prompt-based providers.

    The `doc_id` prefix is what the model cites and what the RAG evaluator
    checks, so the two stay in lockstep.
    """
    if not chunks:
        return "CONTEXT:\n(no documents retrieved)"

    parts: list[str] = ["CONTEXT:"]
    budget = max_chars
    for chunk in chunks:
        entry = f"[{chunk.doc_id}] {chunk.title}\n{chunk.text}"
        if len(entry) > budget:
            break
        parts.append(entry)
        budget -= len(entry)
    return "\n\n".join(parts)


__all__ = [
    "BUILTIN_PROVIDER_MODULES",
    "LLM_PROVIDERS",
    "LLMProvider",
    "LLMResponse",
    "load_provider",
    "render_context",
]
