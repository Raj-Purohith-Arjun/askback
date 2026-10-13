"""FrontLine agent: intent classification and specialized-agent selection.

Two classifiers ship here:

  KeywordIntentClassifier - deterministic, offline, config-driven. The default.
  LLMIntentClassifier     - asks the model to pick an intent. Switch to it in
                            config once a model is wired up; it falls back to
                            the keyword classifier if the model call fails.

Both return the same `Intent`, so routing logic is written once.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from askback.core.contracts import Intent, Message, Role
from askback.core.logging import get_logger
from askback.core.text import cosine, fold_plural, tokenize

from ..llm.base import LLMProvider

log = get_logger(__name__)


@dataclass(slots=True)
class IntentSpec:
    """One routable intent, as declared in the intents file (examples/hr/intents.yaml)."""

    name: str
    route: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    #: Multiplies the final score. Raise it to break ties in a known direction.
    priority: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentSpec:
        return cls(
            name=str(data["name"]),
            route=str(data.get("route", "fallback_agent")),
            description=str(data.get("description", "")),
            keywords=[str(k).lower() for k in data.get("keywords", [])],
            examples=[str(e) for e in data.get("examples", [])],
            priority=float(data.get("priority", 1.0)),
        )


class KeywordIntentClassifier:
    """Scores each intent by keyword overlap and similarity to its examples.

    Deterministic and inspectable: `Intent.scores` shows exactly why a route was
    chosen, which is what you want when a routing test fails at 2am.
    """

    def __init__(self, intents: Sequence[IntentSpec], *, threshold: float = 0.18) -> None:
        self.intents = list(intents)
        # Below this score nothing is confident enough to route; we fall back.
        self.threshold = threshold
        self._example_vectors: dict[str, list[Counter[str]]] = {
            spec.name: [Counter(tokenize(e)) for e in spec.examples] for spec in self.intents
        }
        # Single-word keywords are matched against folded tokens, so fold them
        # here too. Multi-word keywords are matched as raw phrases and are left
        # exactly as the config author wrote them.
        self._keywords: dict[str, list[str]] = {
            spec.name: [k if " " in k else fold_plural(k) for k in spec.keywords]
            for spec in self.intents
        }

    def classify(self, text: str, history: Sequence[Message] = ()) -> Intent:
        tokens = set(tokenize(text))
        query_vector = Counter(tokenize(text))
        lowered = text.lower()
        scores: dict[str, float] = {}

        for spec in self.intents:
            keywords = self._keywords[spec.name]
            matched = [k for k in keywords if _keyword_hit(k, lowered, tokens)]
            keyword_score = (
                min(1.0, len(matched) / min(2.0, float(len(keywords)) or 1.0))
                if keywords
                else 0.0
            )
            example_score = max(
                (cosine(query_vector, vec) for vec in self._example_vectors[spec.name]),
                default=0.0,
            )
            scores[spec.name] = round(
                (0.6 * keyword_score + 0.4 * example_score) * spec.priority, 4
            )

        if not scores:
            return Intent(name="unknown", confidence=0.0, rationale="No intents configured.")

        best = max(scores, key=lambda k: scores[k])
        confidence = scores[best]
        if confidence < self.threshold:
            return Intent(
                name="unknown",
                confidence=confidence,
                rationale=f"Best candidate {best!r} scored {confidence} (< {self.threshold}).",
                scores=scores,
            )
        return Intent(
            name=best,
            confidence=confidence,
            rationale=f"Keyword and example match for {best!r}.",
            scores=scores,
        )


def _keyword_hit(keyword: str, lowered_text: str, tokens: set[str]) -> bool:
    """Plain words match whole tokens; anything else matches as a phrase.

    The distinction matters for hyphenated keywords: the tokenizer splits
    "sign-in" into two tokens, so a token comparison would never match it.
    """
    return keyword in tokens if keyword.isalnum() else keyword in lowered_text


class LLMIntentClassifier:
    """Asks the model to choose an intent, with the keyword classifier as backstop."""

    def __init__(
        self,
        intents: Sequence[IntentSpec],
        llm: LLMProvider,
        *,
        fallback: KeywordIntentClassifier | None = None,
    ) -> None:
        self.intents = list(intents)
        self.llm = llm
        self.fallback = fallback or KeywordIntentClassifier(intents)

    def classify(self, text: str, history: Sequence[Message] = ()) -> Intent:
        catalogue = "\n".join(f"- {s.name}: {s.description}" for s in self.intents)
        prompt = (
            "Classify the user message into exactly one intent from this list, "
            'or "unknown" if none fit.\n\n'
            f"{catalogue}\n\n"
            'Reply with JSON only: {"intent": "<name>", "confidence": 0.0}'
        )
        try:
            response = self.llm.generate(
                [Message(Role.SYSTEM, prompt), Message(Role.USER, text)],
                json_mode=True,
                max_tokens=100,
            )
            data = json.loads(_extract_json(response.text))
            name = str(data.get("intent", "unknown"))
            known = {s.name for s in self.intents}
            if name not in known:
                return self.fallback.classify(text, history)
            return Intent(
                name=name,
                confidence=float(data.get("confidence", 0.8)),
                rationale="Classified by LLM.",
            )
        except Exception as exc:  # noqa: BLE001 - never let routing take the turn down
            log.warning("LLM intent classification failed (%s); using keywords.", exc)
            return self.fallback.classify(text, history)


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of a model reply that may wrap it in prose."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


class FrontLineAgent:
    """Classifies the message, then names the specialized agent that should answer.

    This is the router in the architecture diagram. It does not answer anything
    itself, which keeps routing decisions independently testable.
    """

    name = "frontline"

    def __init__(
        self,
        classifier: KeywordIntentClassifier | LLMIntentClassifier,
        routes: dict[str, str],
        *,
        default_route: str = "fallback_agent",
    ) -> None:
        self.classifier = classifier
        self.routes = routes
        self.default_route = default_route

    def route(self, text: str, history: Sequence[Message] = ()) -> tuple[Intent, str]:
        intent = self.classifier.classify(text, history)
        return intent, self.routes.get(intent.name, self.default_route)

    @classmethod
    def from_config(
        cls, intent_specs: Sequence[IntentSpec], config: dict[str, Any], llm: LLMProvider | None = None
    ) -> FrontLineAgent:
        keyword = KeywordIntentClassifier(
            intent_specs, threshold=float(config.get("threshold", 0.18))
        )
        classifier: KeywordIntentClassifier | LLMIntentClassifier = keyword
        if config.get("classifier", "keyword") == "llm" and llm is not None:
            classifier = LLMIntentClassifier(intent_specs, llm, fallback=keyword)
        return cls(
            classifier,
            {spec.name: spec.route for spec in intent_specs},
            default_route=str(config.get("default_route", "fallback_agent")),
        )
