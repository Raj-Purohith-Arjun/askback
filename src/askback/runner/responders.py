"""Who answers when the agent asks a question: a script first, then a persona.

The scripted list is free and deterministic, so it goes first and is the way a
suite should be written whenever the questions can be foreseen. The persona is for
the ones that cannot: a simulated user, backed by a model, that answers from a
CLOSED sheet of facts.

Closed is the whole point. A persona free to say "I'm in Group A" when asked for a
grade turns every downstream assertion into a measurement of the persona. So the
guarantee is structural, not a request made nicely to the model: it must return
the keys of the facts it used, and an answer that cites none, cites one that is
not on the sheet, or states a number that is not in the cited facts is replaced by
the case's `unknown_response`. The model can decline; it cannot make something up
and have it sent.

The persona's tokens are its own line in the report (`PersonaUsage`), never the
judge's, so a run's metered spend can be read as two different bills.

The core imports no provider SDK: the persona takes any `LLMProvider`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.contracts import Message, Role
from ..core.errors import ConfigError, ProviderError
from ..core.logging import get_logger
from ..datasets.schema import ClarificationSpec, Persona
from ..evaluation.clarification import PersonaUsage, QuestionDetector, Round
from ..providers.base import BUILTIN_PROVIDER_MODULES, LLM_PROVIDERS, LLMProvider, load_provider

log = get_logger(__name__)

#: The only prefix the persona reads credentials under: not the agent's, not the judge's.
PERSONA_ENV_PREFIX = "PERSONA_"

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

PERSONA_SYSTEM_PROMPT = (
    "You play a user talking to an assistant. Your goal: {goal}\n{style}"
    "You may state ONLY facts from the FACT SHEET below, using its values exactly. "
    "Anything the assistant asks that the sheet does not cover, you do not know. "
    'Reply with JSON only: {{"answer": "<one short message>", "fact_keys": [<the key of '
    'every fact you used>]}}. If the sheet does not cover the question, reply '
    '{{"answer": null, "fact_keys": []}}. Never guess, infer or invent.\n\nFACT SHEET:\n{facts}'
)


@dataclass(frozen=True, slots=True)
class Answer:
    """What we say back to a question, and where it came from."""

    text: str
    #: scripted | persona | declined
    source: str
    usage: PersonaUsage | None = None


class PersonaResponder:
    """A model-backed user that can say only what its fact sheet says."""

    def __init__(
        self,
        llm: LLMProvider,
        persona: Persona,
        *,
        price_per_1k_prompt: float = 0.0,
        price_per_1k_completion: float = 0.0,
    ) -> None:
        self.llm = llm
        self.persona = persona
        self.price_per_1k_prompt = price_per_1k_prompt
        self.price_per_1k_completion = price_per_1k_completion

    def answer(self, question: str, rounds: Sequence[Round]) -> Answer:
        persona = self.persona
        system = PERSONA_SYSTEM_PROMPT.format(
            goal=persona.goal,
            style=f"Your style: {persona.style}\n" if persona.style else "",
            facts="\n".join(f"{key}: {value}" for key, value in persona.facts.items()),
        )
        transcript = "".join(f"ASSISTANT: {r.question}\nYOU: {r.answer}\n" for r in rounds)
        # A transport failure raises: it is the persona's infrastructure failing, and
        # answering "I don't know" for it would pass off an outage as a measurement.
        raw = self.llm.generate(
            [Message(Role.SYSTEM, system), Message(Role.USER, f"{transcript}ASSISTANT: {question}")],
            json_mode=True,
            max_tokens=200,
        )
        usage = PersonaUsage(
            calls=1,
            prompt_tokens=raw.prompt_tokens,
            completion_tokens=raw.completion_tokens,
            cost_usd=(
                raw.prompt_tokens * self.price_per_1k_prompt
                + raw.completion_tokens * self.price_per_1k_completion
            )
            / 1000,
        )
        text = self._accepted(raw.text)
        if text is None:
            return Answer(persona.unknown_response, "declined", usage)
        return Answer(text, "persona", usage)

    def _accepted(self, raw_text: str) -> str | None:
        """The model's answer if it stayed on the sheet, else None (decline)."""
        start, end = raw_text.find("{"), raw_text.rfind("}")
        try:
            data = json.loads(raw_text[start : end + 1])
        except ValueError as exc:
            raise ProviderError("persona reply was not JSON; it cannot be trusted to stay on its sheet") from exc
        answer, keys = data.get("answer"), data.get("fact_keys")
        if not isinstance(answer, str) or not answer.strip():
            return None
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            log.warning("Persona answered without citing its facts; declined: %r", answer[:80])
            return None
        facts = self.persona.facts
        if any(key not in facts for key in keys):
            log.warning("Persona cited a fact that is not on the sheet (%s); declined.", keys)
            return None
        # A cited key is not a licence for a figure it does not contain.
        allowed = " ".join(facts[key] for key in keys)
        if any(number not in allowed for number in _NUMBER.findall(answer)):
            log.warning("Persona stated a number outside its cited facts; declined: %r", answer[:80])
            return None
        return answer.strip()


class TurnResponder:
    """The answers for one turn of one sample: scripted ones in order, then the persona.

    Built per turn, so the cursor is never shared between samples or workers.
    """

    def __init__(
        self,
        spec: ClarificationSpec | None,
        persona: PersonaResponder | None,
        *,
        persona_declared: bool = False,
    ) -> None:
        self._answers = list(spec.answers) if spec else []
        self._next = 0
        self._persona = persona
        self._persona_declared = persona_declared
        self._use_persona = spec is None or spec.on_exhausted == "persona"

    def next(self, question: str, rounds: Sequence[Round]) -> Answer | None:
        """The next answer, or None when there is nothing left to say."""
        if self._next < len(self._answers):
            text = self._answers[self._next]
            self._next += 1
            return Answer(text, "scripted")
        if self._use_persona and self._persona is not None:
            return self._persona.answer(question, rounds)
        return None

    def nothing_to_say(self) -> str:
        """Why `next` returned None, as the fix for whoever owns the suite."""
        if self._answers and not self._use_persona:
            return (
                "its scripted answers are used up and `on_exhausted: gap` stops here. Add more "
                "to this turn's `clarification.answers`, or use `on_exhausted: persona`"
            )
        if self._persona_declared and self._persona is None:
            return (
                "the case has a `persona` but no model is configured to play it. Set "
                "`evaluation.clarification.persona.llm`, or script this turn's answers"
            )
        if self._answers:
            return (
                "its scripted answers are used up and the case has no `persona`. Add more to "
                "this turn's `clarification.answers`, or give the case a `persona`"
            )
        return (
            "this turn has no scripted answer and the case has no `persona`. Add the answer to "
            "this turn's `clarification.answers` (schema_version: 3), or give the case a `persona`"
        )


@dataclass(frozen=True, slots=True)
class ClarificationSettings:
    """What `evaluation.clarification` configures: the detector and the persona's model."""

    detector: QuestionDetector
    persona_llm: LLMProvider | None = None
    price_per_1k_prompt: float = 0.0
    price_per_1k_completion: float = 0.0


def build_clarification(config: Mapping[str, Any] | None) -> ClarificationSettings:
    """Read `evaluation.clarification`. No block is the stock detector and no persona model.

    A persona `llm` that is present is a request for a model and is honoured or
    refused, never quietly dropped: a suite that relies on a persona would
    otherwise turn into gaps with no hint why.
    """
    if config is None:
        return ClarificationSettings(QuestionDetector())
    if not isinstance(config, Mapping):
        raise ConfigError(f"evaluation.clarification must be a mapping, got {type(config).__name__}")
    stray = sorted(set(config) - {"detector", "persona"})
    if stray:
        raise ConfigError(f"unknown evaluation.clarification key(s): {stray}; known: detector, persona")
    detector = QuestionDetector.from_config(config.get("detector"))
    persona = config.get("persona")
    if persona is None:
        return ClarificationSettings(detector)
    if not isinstance(persona, Mapping):
        raise ConfigError(f"evaluation.clarification.persona must be a mapping, got {persona!r}")
    stray = sorted(set(persona) - {"llm", "price_per_1k_prompt", "price_per_1k_completion"})
    if stray:
        raise ConfigError(f"unknown evaluation.clarification.persona key(s): {stray}")
    prices = [_price(persona, key) for key in ("price_per_1k_prompt", "price_per_1k_completion")]
    llm = persona.get("llm")
    if llm is None:
        return ClarificationSettings(detector, None, *prices)
    if not isinstance(llm, Mapping):
        raise ConfigError(f"evaluation.clarification.persona.llm must be a mapping, got {llm!r}")
    settings = dict(llm)
    name = settings.pop("provider", None)
    if not isinstance(name, str) or not name:
        raise ConfigError("evaluation.clarification.persona.llm.provider is required")
    if "env_prefix" in settings:
        raise ConfigError(
            "evaluation.clarification.persona.llm.env_prefix cannot be set: the persona always "
            f"reads {PERSONA_ENV_PREFIX}-prefixed variables, never the agent's or the judge's"
        )
    if name not in LLM_PROVIDERS and name.strip().lower() not in BUILTIN_PROVIDER_MODULES:
        raise ConfigError(
            f"unknown evaluation.clarification.persona.llm.provider {name!r}; known: "
            f"{sorted({*LLM_PROVIDERS.names(), *BUILTIN_PROVIDER_MODULES})}"
        )
    provider_cls = load_provider(name)
    if not getattr(provider_cls, "remote", False):
        raise ConfigError(
            f"evaluation.clarification.persona.llm.provider {name!r} is not a model that can "
            "play a persona"
        )
    # A Protocol says nothing about constructors; a TypeError refuses a provider
    # that cannot take `env_prefix`, which is what keeps its credentials its own.
    construct: Callable[..., LLMProvider] = provider_cls
    try:
        provider = construct(env_prefix=PERSONA_ENV_PREFIX, **settings)
    except TypeError as exc:
        raise ConfigError(f"evaluation.clarification.persona.llm does not fit {name!r}: {exc}") from exc
    missing = provider.missing_settings()
    if missing:
        raise ConfigError(
            f"evaluation.clarification.persona.llm ({name}) is missing: "
            + "; ".join(f"{setting} (or {variable})" for setting, variable in missing.items())
        )
    if not provider.is_available():
        raise ConfigError(
            f"evaluation.clarification.persona.llm.provider {name!r} is configured but not available"
        )
    return ClarificationSettings(detector, provider, *prices)


def _price(block: Mapping[str, Any], key: str) -> float:
    value = block.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"evaluation.clarification.persona.{key} must be a number >= 0, got {value!r}")
    return float(value)


__all__ = [
    "Answer",
    "ClarificationSettings",
    "PersonaResponder",
    "TurnResponder",
    "build_clarification",
]
