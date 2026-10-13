"""Clarifying questions: telling an answer from a question, and judging the habit.

An agent often meets a vague question with a question of its own. A scripted
conversation written without that in mind derails at once: turn 2 is sent as the
reply to something it was never written for, and every later assertion measures
the derailment. This module is the part that does not need a network:

  QuestionDetector   classifies one reply, free and deterministic, from weighted
                     signals. It runs on every turn of every sample, so it cannot
                     cost anything, and it records the score and the signals that
                     fired so a misfire is visible in the report instead of
                     silently reshaping the conversation.
  Round              one question the agent asked and the answer we gave it. The
                     rounds ride on the turn result, and on to the judge.
  policy_result      the `allow | forbid | require` assertion on the first reply.
  gap_result         what a turn reports when the agent asked and we had nothing
                     to say.

Who is being answered matters as much as what is said. A reply that invites the user
to ask ("Please share your specific query...") is clarification only if the user's
message left something to clarify. When that message made no request - a greeting, a
thanks, "are you there" - there is nothing to clarify and the reply is a welcome, whatever
it says. That is decided from the user's message, not from phrases in the reply, so it
holds for any agent's wording. It never applies when the user typed anything else, so a
vague topic ("dental") still gets its clarifying question recognised.

The detector scores signals rather than matching one pattern because no single
cue survives contact with real text. A question mark is absent from "Please share
your specific question..."; a closing offer ("Let me know if you'd like...") is
present in plain answers; a citation is the strongest evidence of an answer and
absent from many of them. Weights are configurable, and a weight of 0 switches a
signal off.

Two ways a turn ends still being a question are kept apart, because they blame
different parties. We answered `max_rounds` times and it is STILL asking: the agent
failed to converge, a hard error. We had nothing to say: a gap in OUR suite, a
skipped result that names the fix and cannot pass. See `runner/test_runner.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.contracts import ChatResponse
from ..core.errors import ConfigError
from ..core.text import tolerate_line_breaks
from ..datasets.schema import ClarificationSpec
from .base import EvalResult, Evaluator

#: The `evaluator` name on clarification results; the engine reads it to veto a failed policy.
CLARIFICATION = "clarification"

DEFAULT_THRESHOLD = 0.5

_FLAGS = re.IGNORECASE | re.MULTILINE

# What makes a message "a request for nothing", by CATEGORY.
#
# A user message asks for nothing when every word in it belongs to one of the categories
# below: social ritual whose only content is "I am here" or "I am leaving". Each category is
# a kind of ritual, listed once, from what people do when they open, keep up and close a
# conversation, not from what any test happened to send. To extend it, ask which kind of
# ritual a missing word belongs to and add it to THAT category; a word that fits none is
# probably content, and content is exactly what must keep a message a request.
#
# The list errs one way on purpose. A message with ANY word outside it is a request, so a
# greeting we did not list is merely read as a question (the reply is classified as before:
# a visible, harmless miss), while a request wrongly read as a greeting would hide a real
# clarification. Never add a word that carries a topic, an object or a verb of asking
# ("help", "need", "tell", "what" and "how" alone, "new").
#
# If this has to grow a third time to keep a baseline green, the mechanism is wrong, not the
# list (CLAUDE.md, invariant 9). Escalate; do not extend.
_GREETINGS = frozenset({  # opening a conversation
    "hi", "hello", "hey", "hiya", "heya", "hullo", "howdy", "yo", "sup", "wassup", "greetings",
    "hola", "bonjour", "ciao", "hallo", "namaste", "salaam", "salam", "shalom", "aloha",
})  # fmt: skip
_TIMES_OF_DAY = frozenset({"good", "morning", "afternoon", "evening", "night", "day"})
_THANKS_AND_ACKNOWLEDGEMENT = frozenset({  # keeping it up: gratitude, assent with no content
    "thanks", "thank", "thx", "ty", "cheers", "ok", "okay", "k", "cool", "great", "fine",
    "nice", "awesome", "perfect", "alright",
})  # fmt: skip
_INTENSIFIERS = frozenset({"so", "very", "much", "many", "lot", "a", "again", "really"})
_CLOSINGS = frozenset({"bye", "goodbye", "cya", "later", "see", "take", "care"})
_PRESENCE_CHECKS = frozenset({  # "are you there?": asks whether the agent is, not about anything
    "are", "is", "there", "here", "anyone", "anybody", "someone", "available", "online",
    "awake", "listening", "around", "you", "u", "r",
})  # fmt: skip
_POLITENESS_MARKERS = frozenset({"please", "pls", "plz", "kindly", "sorry", "pardon", "excuse", "me"})
_SMALL_TALK_ABOUT_THE_AGENT = frozenset({  # "how are you doing today?": asks after the agent
    "how", "hows", "how's", "doing", "going", "today", "up", "what's", "whats", "it",
})  # fmt: skip

GREETING_WORDS: frozenset[str] = frozenset[str]().union(
    _GREETINGS,
    _TIMES_OF_DAY,
    _THANKS_AND_ACKNOWLEDGEMENT,
    _INTENSIFIERS,
    _CLOSINGS,
    _PRESENCE_CHECKS,
    _POLITENESS_MARKERS,
    _SMALL_TALK_ABOUT_THE_AGENT,
)

#: How a reply introduces itself: the name it gives is the one a greeting addresses.
_SELF_NAME = re.compile(
    tolerate_line_breaks(r"\b(?:I am|I'm|I\u2019m|my name is|this is)\s+([A-Z][\w-]*)")
)
_REQUEST_VERBS = (
    "share|specify|provide|tell|let me know|confirm|clarify|indicate|state|describe|"
    "mention|enter|give|select|choose"
)

#: name -> (weight, pattern). Positive weights say "this is a question", negative
#: "this is an answer". The structural signals (`trailing_question`,
#: `examples_list`, `has_citations`) are computed in code and have weights only.
#:
#: Written with ordinary spaces and compiled through `tolerate_line_breaks`, so every
#: phrase matches across a hard wrap. A pattern added in config gets the same treatment.
#: A phrase must therefore not rely on a space meaning exactly one space.
_PATTERNS: dict[str, tuple[float, str]] = {
    "request_imperative": (0.45, rf"\b(?:please|kindly)\s+(?:{_REQUEST_VERBS})\b"),
    "polite_request": (0.35, rf"\b(?:could|can|would|will)\s+you\s+(?:please\s+)?(?:{_REQUEST_VERBS})\b"),
    "request_detail": (
        0.2, r"\b(?:also\s+)?(?:share|provide|specify|tell me|send)\s+(?:the|your|me|which|what|any)\b"
    ),
    "purpose_clause": (0.25, r"\bso (?:that )?I (?:can|could|may)\b"),
    # Stops at sentence punctuation, not at a line end: a wrapped question is one question.
    "second_person_wh": (0.25, r"\b(?:which|what)\b[^.?!]{0,80}\byou(?:r)?\b"),
    "source_reference": (
        -0.5, r"\b(?:according to|as per|chapter\s+\d+|pages?\s+\d+|section\s+\d+|policy manual)\b"
    ),
    "closing_offer": (
        -0.3,
        r"\b(?:let me know if you(?:'d| would)?\s*(?:like|want|need)|feel free to ask|"
        r"happy to help|if you(?:'d| would) like (?:me )?to)\b",
    ),
    "states_figures": (
        -0.3,
        r"\b\d+(?:\.\d+)?\s*(?:days?|weeks?|months?|years?|hours?|%|percent|instal+ments?)\b|"
        r"\b(?:one|two|three|four|five|six|ten|twelve)\s+(?:equal\s+)?"
        r"(?:days?|weeks?|months?|years?|instal+ments?)\b",
    ),
}
_STRUCTURAL: dict[str, float] = {"trailing_question": 0.4, "examples_list": 0.25, "has_citations": -0.6}

# The structural evidence is the list under the lead-in, not where the lead-in sits: a
# wrap can push "for example:" off the start of a line, and anchoring there lost the signal.
_EXAMPLES_LIST = re.compile(
    tolerate_line_breaks(
        r"\b(?:examples?|for example|for instance|e\.g\.|such as|options|choices)\s*:?\s*\n\s*[-*•\d]"
    ),
    _FLAGS,
)


@dataclass(frozen=True, slots=True)
class Detection:
    """Whether one reply is a question, and why the detector thinks so."""

    is_question: bool
    #: Sum of the matched signals' weights. Not clamped: a strongly negative score
    #: is information, and so is one that only just missed the threshold.
    score: float
    #: The signals that fired, in a fixed order.
    signals: tuple[str, ...]
    threshold: float = DEFAULT_THRESHOLD
    #: Scored as a question but ruled out because the user's message made no request. Not
    #: the same as scoring low: the report shows it, so the override is never silent.
    suppressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "is_question": self.is_question,
            "score": round(self.score, 3),
            "signals": list(self.signals),
        }
        if self.suppressed:
            data["suppressed"] = True
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Detection:
        return cls(
            is_question=bool(data["is_question"]),
            score=float(data["score"]),
            signals=tuple(str(s) for s in data.get("signals", [])),
            suppressed=bool(data.get("suppressed", False)),
        )


class QuestionDetector:
    """Classifies a reply as a question or an answer from weighted signals.

    Pure and stateless, so one instance is shared by every worker thread.
    """

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        weights: Mapping[str, float] | None = None,
        patterns: Mapping[str, tuple[float, str]] | None = None,
        greeting_words: Sequence[str] = (),
    ) -> None:
        known = {**{n: w for n, (w, _) in _PATTERNS.items()}, **_STRUCTURAL}
        for name in weights or {}:
            if name not in known and name not in (patterns or {}):
                raise ConfigError(
                    f"unknown clarification detector signal {name!r}; known: {sorted(known)}"
                )
        self.threshold = float(threshold)
        self._greeting_words = GREETING_WORDS | {w.strip().lower() for w in greeting_words}
        self._weights = {**known, **{n: float(w) for n, (w, _) in (patterns or {}).items()}}
        self._weights.update({n: float(w) for n, w in (weights or {}).items()})
        self._compiled: dict[str, re.Pattern[str]] = {}
        for name, (_, pattern) in {**_PATTERNS, **(patterns or {})}.items():
            try:
                self._compiled[name] = re.compile(tolerate_line_breaks(pattern), _FLAGS)
            except re.error as exc:
                raise ConfigError(f"clarification detector signal {name!r}: bad pattern ({exc})") from exc
        self._identity = json.dumps(
            [
                self.threshold,
                sorted(self._weights.items()),
                sorted((patterns or {}).items()),
                sorted(self._greeting_words),
            ],
            sort_keys=True,
        )

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> QuestionDetector:
        """Build from `evaluation.clarification.detector`: threshold, weights, patterns."""
        if config is None:
            return cls()
        if not isinstance(config, Mapping):
            raise ConfigError(
                f"evaluation.clarification.detector must be a mapping, got {type(config).__name__}"
            )
        stray = sorted(set(config) - {"threshold", "weights", "patterns", "greeting_words"})
        if stray:
            raise ConfigError(
                f"unknown evaluation.clarification.detector key(s): {stray}; "
                "known: greeting_words, patterns, threshold, weights"
            )
        words = config.get("greeting_words") or []
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            raise ConfigError(
                f"evaluation.clarification.detector.greeting_words must be a list of words, got {words!r}"
            )
        extra: dict[str, tuple[float, str]] = {}
        for name, spec in (config.get("patterns") or {}).items():
            if not isinstance(spec, Mapping) or not isinstance(spec.get("pattern"), str):
                raise ConfigError(
                    f"evaluation.clarification.detector.patterns.{name} needs `pattern` and `weight`"
                )
            extra[str(name)] = (_number(spec.get("weight"), f"patterns.{name}.weight"), spec["pattern"])
        weights = {
            str(n): _number(w, f"weights.{n}") for n, w in (config.get("weights") or {}).items()
        }
        return cls(
            threshold=_number(config.get("threshold", DEFAULT_THRESHOLD), "threshold"),
            weights=weights,
            patterns=extra,
            greeting_words=words,
        )

    def provenance(self) -> dict[str, str]:
        """Empty for the stock detector. A tuned one makes runs incomparable, so it says so."""
        if self._identity == _DEFAULT_IDENTITY:
            return {}
        return {"clarification_detector": hashlib.sha256(self._identity.encode()).hexdigest()[:12]}

    def detect(self, response: ChatResponse, *, user_message: str | None = None) -> Detection:
        """Classify `response`. `user_message` is what it replies to, when known.

        Without it nothing is ever ruled out on the user's side: an unknown message is not
        evidence that the user asked for nothing.
        """
        text = response.reply or ""
        fired: list[str] = []
        for name, pattern in self._compiled.items():
            if pattern.search(text):
                fired.append(name)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines and lines[-1].endswith("?"):
            fired.append("trailing_question")
        if _EXAMPLES_LIST.search(text):
            fired.append("examples_list")
        if response.citations:
            fired.append("has_citations")
        score = sum(self._weights.get(name, 0.0) for name in fired)
        # A signal weighted to 0 is off: it neither scores nor appears in the report.
        shown = tuple(n for n in fired if self._weights.get(n, 0.0) != 0.0)
        scored = score >= self.threshold
        if scored and user_message is not None and self._made_no_request(user_message, text):
            return Detection(False, score, (*shown, "user_made_no_request"), self.threshold, True)
        return Detection(scored, score, shown, self.threshold)

    def _made_no_request(self, user_message: str, reply: str) -> bool:
        """True when every word of the user's message is a greeting word or the reply's own name."""
        words = re.findall(r"[a-z0-9']+", user_message.lower().replace("\u2019", "'"))
        if not words:
            return False
        names = {name.lower() for name in _SELF_NAME.findall(reply)}
        return all(w in self._greeting_words or w in names for w in words)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"evaluation.clarification.detector.{where} must be a number, got {value!r}")
    return float(value)


_DEFAULT_IDENTITY = QuestionDetector()._identity


@dataclass(slots=True)
class PersonaUsage:
    """What the simulated user cost. Kept apart from the judge's spend on purpose."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: PersonaUsage) -> PersonaUsage:
        return PersonaUsage(
            self.calls + other.calls,
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cost_usd + other.cost_usd,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PersonaUsage:
        return cls(
            int(data.get("calls", 0)),
            int(data.get("prompt_tokens", 0)),
            int(data.get("completion_tokens", 0)),
            float(data.get("cost_usd", 0.0)),
        )


@dataclass(slots=True)
class Round:
    """One question the agent asked, and what we said back."""

    #: The agent's reply that was a question.
    question: str
    detection: Detection
    answer: str
    #: scripted | persona | declined (the persona had no such fact and said so).
    source: str
    status: str = ""
    latency_ms: float = 0.0
    usage: PersonaUsage | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "question": self.question,
            "detection": self.detection.to_dict(),
            "answer": self.answer,
            "source": self.source,
            "status": self.status,
            "latency_ms": self.latency_ms,
        }
        if self.usage is not None:
            data["usage"] = self.usage.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Round:
        usage = data.get("usage")
        return cls(
            question=str(data["question"]),
            detection=Detection.from_dict(data["detection"]),
            answer=str(data["answer"]),
            source=str(data["source"]),
            status=str(data.get("status", "")),
            latency_ms=float(data.get("latency_ms", 0.0)),
            usage=PersonaUsage.from_dict(usage) if usage is not None else None,
        )


def policy_result(spec: ClarificationSpec, *, asked_first: bool, rounds: int) -> EvalResult:
    """The `allow | forbid | require` assertion, made on the agent's FIRST reply.

    `allow` always passes but scores 1 - rounds/(max_rounds + 1): every round it
    took is a little lost, so creeping clarification moves the score and shows in
    the regression diff instead of hiding behind a pass.
    """
    name = "clarification_policy"
    if spec.policy == "forbid":
        check = Evaluator.check(
            name,
            not asked_first,
            detail=(
                "Answered without asking."
                if not asked_first
                else "Asked a question where the policy forbids one. The turn was answerable, "
                "so asking is a usability defect."
            ),
            expected="no clarifying question",
            actual="question" if asked_first else "answer",
        )
    elif spec.policy == "require":
        check = Evaluator.check(
            name,
            asked_first,
            detail=(
                "Asked for clarification, as the policy requires."
                if asked_first
                else "Answered a question the policy marks ambiguous. A confident answer "
                "is wrong for most users without the missing detail."
            ),
            expected="clarifying question",
            actual="question" if asked_first else "answer",
        )
    else:
        score = 1.0 - rounds / (spec.max_rounds + 1)
        check = Evaluator.check(
            name,
            True,
            score=score,
            detail=(
                "Answered without asking."
                if not rounds
                else f"Needed {rounds} clarification round(s); score falls with each."
            ),
            expected="any",
            actual=f"{rounds} round(s)",
        )
    return EvalResult(
        evaluator=CLARIFICATION, score=check.score, passed=check.passed, checks=[check]
    )


def gap_result(message: str) -> EvalResult:
    """The turn ended asking and we had nothing to say: our gap, not the agent's.

    Skipped, because nothing about the agent was measured, and failed, because
    unmeasured never passes. The check name says whose gap it is; the detail says
    how to close it.
    """
    check = Evaluator.check(
        "clarification_unanswered", False, detail=message, expected="an answer to grade"
    )
    return EvalResult(
        evaluator=CLARIFICATION, score=0.0, passed=False, checks=[check], skipped=True
    )


__all__ = [
    "CLARIFICATION",
    "DEFAULT_THRESHOLD",
    "Detection",
    "PersonaUsage",
    "QuestionDetector",
    "Round",
    "gap_result",
    "policy_result",
]
