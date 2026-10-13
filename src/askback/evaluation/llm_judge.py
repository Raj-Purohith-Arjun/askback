"""LLM-as-a-judge: semantic scoring against a reference answer.

Two implementations behind one evaluator:

  ModelJudge      asks a real model for per-dimension scores. Used only when
                  `evaluation.judge.llm` names one.
  HeuristicJudge  deterministic lexical proxy, used offline. The default.

The judge's model is its own, built here from its own config block. It is never
the agent's: a judge sharing the subject's model is a conflict of interest, and
the agent may not be in this process at all. Which judge a run gets is decided
once, from config, and recorded in the run context - a configured judge that
cannot run is an error, never a quiet switch to the heuristic.

The same goes for a judge that fails mid-run. A call that errors or returns
something unparseable is answered by the heuristic *for that case only*, and the
result says so (`EvalResult.degraded`): the run counts them, `max_degraded` gates
on them, and nothing averages the two regimes together unmarked.

A model judge is metered. Each result carries what it cost (`EvalResult.judge`),
a verdict served from the on-disk cache costs nothing, and the runner stops a run
that outspends `budget_usd`.

The heuristic judge is honestly a *proxy*, not semantic judgement: it measures
word overlap with the reference, so it rewards a correct answer phrased
differently less than a model would. It exists so the suite runs and regresses
meaningfully today; expect scores to shift when a real model is switched on,
and re-baseline at that point.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from ..core.config import judge_number, resolve_path
from ..core.contracts import ChatResponse, Message, ResponseStatus, Role, Severity
from ..core.errors import ConfigError
from ..core.logging import get_logger
from ..core.registry import EVALUATORS
from ..core.text import coverage, tokenize
from ..datasets.schema import TestCase, Turn
from ..providers.base import BUILTIN_PROVIDER_MODULES, LLM_PROVIDERS, LLMProvider, load_provider
from .base import Check, EvalResult, Evaluator, JudgeUsage
from .judge_cache import VerdictCache
from .rag_eval import groundedness

if TYPE_CHECKING:
    from .clarification import Round

log = get_logger(__name__)

DIMENSIONS = ("correctness", "completeness", "groundedness", "reasoning", "clarity")

#: The only prefix the judge reads credentials under. Never the agent's.
JUDGE_ENV_PREFIX = "JUDGE_"

#: Part of every cache key. Bump it when anything that shapes a verdict changes
#: outside the prompt text - the scale, the parsing, the dimensions.
JUDGE_PROMPT_VERSION = "1"

#: A dimension passes at this score unless `evaluator_options.llm_judge.threshold`
#: says otherwise. Calibration cuts at the same value, so it validates the cut that gates.
DEFAULT_PASS_THRESHOLD = 0.6

#: Where verdicts are cached unless `evaluation.judge.cache_path` says otherwise.
DEFAULT_CACHE_PATH = ".cache/judge"

#: Keys of the `evaluation.judge` block. The run-level ones (`max_degraded`,
#: `budget_usd`, `min_kappa`) are read by the gates, the runner and `calibrate`;
#: they are listed here so the block is validated in one place.
JUDGE_KEYS = frozenset(
    {
        "llm",
        "cache_path",
        "price_per_1k_prompt",
        "price_per_1k_completion",
        "max_degraded",
        "budget_usd",
        "min_kappa",
    }
)

JUDGE_SYSTEM_PROMPT = """You are a strict evaluation judge for an AI assistant.

Score the ANSWER against the QUESTION and the REFERENCE on each dimension,
from 0.0 (worst) to 1.0 (best):

- correctness: factually agrees with the reference
- completeness: covers every part the question asks for
- groundedness: makes no claim absent from the reference (hallucination check)
- reasoning: the steps implied by the answer are sound
- clarity: unambiguous and readable

Reply with JSON only, no prose:
{"correctness": 0.0, "completeness": 0.0, "groundedness": 0.0,
 "reasoning": 0.0, "clarity": 0.0, "rationale": "one sentence"}"""
_NUMBER = re.compile(r"\d[\d,.]*")

#: What a cached verdict says in place of the rationale the cache does not keep.
RATIONALE_NOT_CACHED = "Rationale unavailable: cached verdict, scores only."


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """A judge's scores for one response, and what getting them took."""

    scores: dict[str, float]
    rationale: str
    #: None for a judge that costs nothing and can say nothing about it.
    usage: JudgeUsage | None = None
    #: Set when these scores did not come from the judge that was asked for.
    degraded: str | None = None


class Judge(ABC):
    """Produces a score in [0, 1] for each dimension, plus a one-line rationale."""

    #: Recorded in the run context. Two runs graded by different judges are not
    #: comparable, and this is what the regression comparison reads to say so.
    identity: str = "unknown"

    @abstractmethod
    def score(
        self, turn: Turn, response: ChatResponse, rounds: Sequence[Round] = ()
    ) -> JudgeVerdict:
        """Score `response`. `rounds` is the clarification exchange that preceded it, if any."""
        ...

    def provenance(self) -> dict[str, str]:
        return {"judge": self.identity}


class HeuristicJudge(Judge):
    """Deterministic lexical scoring. No network, no variance, no cost."""

    identity = "heuristic"

    def score(
        self, turn: Turn, response: ChatResponse, rounds: Sequence[Round] = ()
    ) -> JudgeVerdict:
        reply_tokens = tokenize(response.reply)
        reference_tokens = tokenize(turn.expect.reference_answer)

        correctness = _f1(reply_tokens, reference_tokens)
        completeness = coverage(reference_tokens, reply_tokens)
        # Same rule as the RAG evaluator: only a real answer can be ungrounded.
        grounded = (
            groundedness(response.reply, response.retrieved, 0.55)[0]
            if response.retrieved and response.status is ResponseStatus.ANSWERED
            else 1.0
        )
        return JudgeVerdict(
            {
                "correctness": correctness,
                "completeness": completeness,
                "groundedness": grounded,
                # Numbers are where a wrong answer usually shows up in this domain,
                # so agreement on figures stands in for sound reasoning.
                "reasoning": _numeric_agreement(
                    response.reply, turn.expect.reference_answer, correctness
                ),
                "clarity": _clarity(response.reply),
            },
            "Lexical proxy scoring (no model available).",
        )


class ModelJudge(Judge):
    """Asks a model to grade the answer, and says so when it could not.

    A failed call (transport error, refusal, unparseable reply) is graded by the
    heuristic for that case and returned as degraded. It is never cached and never
    presented as the model's verdict.
    """

    def __init__(
        self,
        llm: LLMProvider,
        *,
        fallback: Judge | None = None,
        cache: VerdictCache | None = None,
        price_per_1k_prompt: float = 0.0,
        price_per_1k_completion: float = 0.0,
        prompt_version: str = JUDGE_PROMPT_VERSION,
    ) -> None:
        self.llm = llm
        self.fallback = fallback or HeuristicJudge()
        self.cache = cache
        self.price_per_1k_prompt = price_per_1k_prompt
        self.price_per_1k_completion = price_per_1k_completion
        self.prompt_version = prompt_version
        self.model = str(getattr(llm, "model", ""))
        self.identity = f"model:{llm.name}" + (f"/{self.model}" if self.model else "")

    def provenance(self) -> dict[str, str]:
        return {**super().provenance(), "judge_prompt_version": self.prompt_version}

    def score(
        self, turn: Turn, response: ChatResponse, rounds: Sequence[Round] = ()
    ) -> JudgeVerdict:
        # Only a turn that had an exchange gets the preamble, so every prompt, and
        # with it every cache key, for a turn without one is what it always was.
        exchange = "".join(f"AGENT ASKED:\n{r.question}\nUSER ANSWERED:\n{r.answer}\n\n" for r in rounds)
        prompt = (
            (f"CLARIFICATION EXCHANGE (the agent asked before it answered):\n{exchange}" if rounds else "")
            + f"QUESTION:\n{turn.user}\n\n"
            f"REFERENCE:\n{turn.expect.reference_answer}\n\n"
            f"ANSWER:\n{response.reply}"
        )
        key = VerdictCache.key(JUDGE_SYSTEM_PROMPT, prompt, self.model, self.prompt_version)
        if self.cache is not None and (hit := self.cache.get(key)) is not None:
            # Free by construction: the budget figure counts money spent.
            return JudgeVerdict(hit, RATIONALE_NOT_CACHED, usage=JudgeUsage(cache_hits=1))

        try:
            raw = self.llm.generate(
                [Message(Role.SYSTEM, JUDGE_SYSTEM_PROMPT), Message(Role.USER, prompt)],
                json_mode=True,
                max_tokens=300,
            )
            start, end = raw.text.find("{"), raw.text.rfind("}")
            data = json.loads(raw.text[start : end + 1])
            scores = {d: _clamp(data.get(d, 0.0)) for d in DIMENSIONS}
            rationale = str(data.get("rationale", ""))[:300]
        except Exception as exc:  # noqa: BLE001 - documented judge boundary: degrade, mark, count
            reason = f"{type(exc).__name__}: {exc}"
            log.warning(
                "Model judge %s failed on %r (%s); heuristic scores, marked degraded.",
                self.identity, turn.user[:60], reason,
            )
            fallback = self.fallback.score(turn, response)
            # A failed call has no token counts to bill; it is counted as a call.
            return JudgeVerdict(
                fallback.scores,
                f"DEGRADED, model judge failed ({reason}); lexical proxy scores.",
                usage=JudgeUsage(calls=1),
                degraded=reason,
            )

        if self.cache is not None:
            self.cache.put(key, scores)
        cost = (
            raw.prompt_tokens * self.price_per_1k_prompt
            + raw.completion_tokens * self.price_per_1k_completion
        ) / 1000
        usage = JudgeUsage(
            calls=1,
            prompt_tokens=raw.prompt_tokens,
            completion_tokens=raw.completion_tokens,
            cost_usd=cost,
        )
        return JudgeVerdict(scores, rationale, usage=usage)


@EVALUATORS.register("llm_judge")
class LLMJudgeEvaluator(Evaluator):
    """Semantic correctness, completeness, hallucination, reasoning and clarity."""

    name: ClassVar[str] = "llm_judge"
    description: ClassVar[str] = "Reference-based semantic scoring across five dimensions."

    def __init__(
        self,
        *,
        judge: Judge | None = None,
        threshold: float = DEFAULT_PASS_THRESHOLD,
        weights: dict[str, float] | None = None,
        **options: Any,
    ) -> None:
        super().__init__(**options)
        self.threshold = threshold
        self.weights = weights or {d: 1.0 for d in DIMENSIONS}
        self.judge: Judge = judge or HeuristicJudge()

    def provenance(self) -> dict[str, str]:
        return self.judge.provenance()

    def evaluate(self, case: TestCase, turn: Turn, response: ChatResponse) -> EvalResult:
        return self._grade(turn, response, ())

    def evaluate_exchange(
        self, case: TestCase, turn: Turn, response: ChatResponse, rounds: Sequence[Round]
    ) -> EvalResult:
        return self._grade(turn, response, rounds)

    def _grade(self, turn: Turn, response: ChatResponse, rounds: Sequence[Round]) -> EvalResult:
        # Without a reference answer there is nothing to judge against.
        if not turn.expect.reference_answer.strip():
            return EvalResult(evaluator=self.name, score=1.0, passed=True, skipped=True)

        # A judge written before rounds existed takes (turn, response) only.
        verdict = self.judge.score(turn, response, rounds) if rounds else self.judge.score(turn, response)
        scores, rationale = verdict.scores, verdict.rationale
        checks: list[Check] = [
            self.check(
                dimension,
                scores.get(dimension, 0.0) >= self.threshold,
                score=scores.get(dimension, 0.0),
                detail=f"{dimension} {scores.get(dimension, 0.0):.2f} "
                f"(pass >= {self.threshold:.2f}). {rationale}".strip(),
                # Hallucination is the one dimension worth failing a build over.
                severity=Severity.CRITICAL if dimension == "groundedness" else Severity.WARNING,
                expected=f">= {self.threshold}",
                actual=round(scores.get(dimension, 0.0), 3),
            )
            for dimension in DIMENSIONS
        ]

        result = self.build(checks)
        result.degraded = verdict.degraded
        result.judge = verdict.usage
        total_weight = sum(self.weights.get(d, 1.0) for d in DIMENSIONS) or 1.0
        result.score = sum(
            scores.get(d, 0.0) * self.weights.get(d, 1.0) for d in DIMENSIONS
        ) / total_weight
        return result


# --------------------------------------------------------------------------- #
# Building the judge from config
# --------------------------------------------------------------------------- #


def build_judge(config: Mapping[str, Any] | None = None) -> Judge:
    """Build the judge named by the `evaluation.judge` block.

    No block, or no `llm` in it, is the heuristic judge. An `llm` that is
    present is a request for a model judge and is honoured or refused: an
    unknown provider, one that is not a remote model, or one with no
    credentials raises rather than falling back, because a run that asked for a
    model and was graded lexically has changed scoring regime without saying.

    The judge's credentials are its own. The provider is built to read
    `JUDGE_`-prefixed environment variables only, so it can never pick up the
    agent's endpoint or key by accident; what is missing is named, by setting
    and by variable. The provider's module is imported here, by name, rather
    than relying on something else having imported it first.
    """
    if config is None:
        return HeuristicJudge()
    if not isinstance(config, Mapping):
        raise ConfigError(f"evaluation.judge must be a mapping, got {type(config).__name__}")
    stray = sorted(set(config) - JUDGE_KEYS)
    if stray:
        raise ConfigError(
            f"unknown evaluation.judge key(s): {stray}; known: {sorted(JUDGE_KEYS)}"
        )

    llm = config.get("llm")
    if llm is None:
        return HeuristicJudge()
    if not isinstance(llm, Mapping):
        raise ConfigError(f"evaluation.judge.llm must be a mapping, got {type(llm).__name__}")
    settings = dict(llm)
    name = settings.pop("provider", None)
    if not isinstance(name, str) or not name:
        raise ConfigError("evaluation.judge.llm.provider is required when the block is present")
    if "env_prefix" in settings:
        raise ConfigError(
            "evaluation.judge.llm.env_prefix cannot be set: the judge always reads "
            f"{JUDGE_ENV_PREFIX}-prefixed variables, never the agent's"
        )
    if name not in LLM_PROVIDERS and name.strip().lower() not in BUILTIN_PROVIDER_MODULES:
        raise ConfigError(
            f"unknown evaluation.judge.llm.provider {name!r}; known: "
            f"{sorted({*LLM_PROVIDERS.names(), *BUILTIN_PROVIDER_MODULES})}"
        )

    provider_cls = load_provider(name)
    # Only a remote model adds judgement the heuristic does not already have.
    if not getattr(provider_cls, "remote", False):
        raise ConfigError(
            f"evaluation.judge.llm.provider {name!r} is not a model that can judge; "
            "remove the block to use the heuristic judge"
        )
    # A Protocol says nothing about constructors; `env_prefix` is the documented
    # keyword a provider must take to be a judge, and a TypeError below is how
    # one that does not is refused.
    construct: Callable[..., LLMProvider] = provider_cls
    try:
        provider = construct(env_prefix=JUDGE_ENV_PREFIX, **settings)
    except TypeError as exc:
        raise ConfigError(f"evaluation.judge.llm does not fit provider {name!r}: {exc}") from exc
    missing = provider.missing_settings()
    if missing:
        raise ConfigError(
            f"evaluation.judge.llm ({name}) is missing: "
            + "; ".join(
                f"{setting} (set evaluation.judge.llm.{setting} or {variable})"
                for setting, variable in missing.items()
            )
            + ". The judge does not read the agent's credentials."
        )
    if not provider.is_available():
        raise ConfigError(
            f"evaluation.judge.llm.provider {name!r} is configured but not available "
            "(missing credentials?). Refusing to grade with the heuristic judge instead: "
            "set the credentials, or remove the block to choose the heuristic explicitly."
        )
    cache_path = config.get("cache_path", DEFAULT_CACHE_PATH)
    if not isinstance(cache_path, str) or not cache_path.strip():
        raise ConfigError(
            f"evaluation.judge.cache_path must be a non-empty path, got {cache_path!r}"
        )
    return ModelJudge(
        provider,
        cache=VerdictCache(resolve_path(cache_path), DIMENSIONS),
        price_per_1k_prompt=judge_number(config, "price_per_1k_prompt") or 0.0,
        price_per_1k_completion=judge_number(config, "price_per_1k_completion") or 0.0,
    )


# --------------------------------------------------------------------------- #
# Heuristic helpers
# --------------------------------------------------------------------------- #


def _f1(predicted: list[str], reference: list[str]) -> float:
    """Token-level F1: the standard lexical answer-overlap measure."""
    if not predicted or not reference:
        return 0.0
    common = set(predicted) & set(reference)
    if not common:
        return 0.0
    precision = len(common) / len(set(predicted))
    recall = len(common) / len(set(reference))
    return 2 * precision * recall / (precision + recall)


def _numeric_agreement(reply: str, reference: str, default: float) -> float:
    """Share of the reference's figures that also appear in the reply."""
    wanted = {n.rstrip(".") for n in _NUMBER.findall(reference)}
    if not wanted:
        return default
    found = {n.rstrip(".") for n in _NUMBER.findall(reply)}
    return len(wanted & found) / len(wanted)


def _clarity(reply: str) -> float:
    """Penalise empty, truncated or run-on answers. A shape check, not a style one."""
    text = reply.strip()
    if not text:
        return 0.0
    words = text.split()
    if len(words) < 4:
        return 0.4
    if text[-1] not in ".!?\"')]":
        return 0.7  # looks truncated
    # Very long single sentences are hard to read; 45 words is a generous ceiling.
    longest = max((len(s.split()) for s in re.split(r"(?<=[.!?])\s+", text)), default=0)
    return 1.0 if longest <= 45 else 0.8


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
