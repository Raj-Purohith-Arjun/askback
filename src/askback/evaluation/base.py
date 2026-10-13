"""The evaluator seam, plus the `Check` / `EvalResult` vocabulary.

An evaluator turns one turn's reply into a list of named checks. It is handed
the case for who is asking, and the turn for what this reply must satisfy. Keep
checks small and independently meaningful: a report that says
`retrieval_recall 0.5` is actionable, one that says `rag: fail` is not.

To add an evaluator:

    from askback.core.registry import EVALUATORS
    from askback.evaluation.base import Evaluator

    @EVALUATORS.register("tone")
    class ToneEvaluator(Evaluator):
        name = "tone"
        def evaluate(self, case, turn, response):
            return self.build([...checks...])
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from ..core.contracts import ChatResponse, Severity
from ..datasets.schema import TestCase, Turn

if TYPE_CHECKING:
    from .clarification import Round


@dataclass(slots=True)
class Check:
    """One assertion. `score` carries partial credit; `passed` is the verdict."""

    name: str
    passed: bool
    score: float = 0.0
    detail: str = ""
    severity: Severity = Severity.WARNING
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Check:
        # The verdict and its severity are read, never defaulted: a check that came
        # back as a passing warning because a key was missing would stop gating.
        return cls(
            name=str(data["name"]),
            passed=bool(data["passed"]),
            score=float(data.get("score", 0.0)),
            detail=str(data.get("detail", "")),
            severity=Severity(data["severity"]),
            expected=data.get("expected"),
            actual=data.get("actual"),
        )


@dataclass(frozen=True, slots=True)
class JudgeUsage:
    """What grading one response asked of a metered judge.

    A cache hit is `cache_hits=1` with every other field zero, so a sum of these
    is the money actually spent and never the money a cold run would have cost.
    """

    calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: JudgeUsage) -> JudgeUsage:
        return JudgeUsage(
            self.calls + other.calls,
            self.cache_hits + other.cache_hits,
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cost_usd + other.cost_usd,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "cost_usd": round(self.cost_usd, 6)}


@dataclass(slots=True)
class EvalResult:
    """Everything one evaluator concluded about one response."""

    evaluator: str
    score: float = 0.0
    passed: bool = True
    checks: list[Check] = field(default_factory=list)
    #: True when the case declared nothing for this evaluator to assert.
    skipped: bool = False
    #: Why this result was graded by something other than the judge that was
    #: asked for. Its scores are a different regime from the rest of the run.
    degraded: str | None = None
    #: Set only when a metered judge was involved, so a free run says nothing.
    judge: JudgeUsage | None = None

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def critical_failures(self) -> list[Check]:
        return [c for c in self.failures if c.severity is Severity.CRITICAL]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "evaluator": self.evaluator,
            "score": round(self.score, 4),
            "passed": self.passed,
            "skipped": self.skipped,
            "checks": [c.to_dict() for c in self.checks],
        }
        if self.degraded is not None:
            data["degraded"] = self.degraded
        if self.judge is not None:
            data["judge"] = self.judge.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvalResult:
        judge = data.get("judge")
        return cls(
            evaluator=str(data["evaluator"]),
            score=float(data.get("score", 0.0)),
            passed=bool(data["passed"]),
            checks=[Check.from_dict(c) for c in data.get("checks", [])],
            skipped=bool(data.get("skipped", False)),
            degraded=data.get("degraded"),
            judge=JudgeUsage(**judge) if judge is not None else None,
        )


class Evaluator(ABC):
    """Base class for every evaluation module."""

    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""

    def __init__(self, **options: Any) -> None:
        self.options = options

    @abstractmethod
    def evaluate(self, case: TestCase, turn: Turn, response: ChatResponse) -> EvalResult:
        """Assess `response`, the reply to `turn` of `case`, against `turn.expect`."""

    def evaluate_exchange(
        self, case: TestCase, turn: Turn, response: ChatResponse, rounds: Sequence[Round]
    ) -> EvalResult:
        """Assess `response` when the agent asked questions on the way to it.

        `rounds` is every question it asked and the answer it was given, oldest
        first. Only called when there are some, so an evaluator that does not
        override this is graded exactly as before: most of them assert on the
        final reply alone and have no use for the exchange.
        """
        return self.evaluate(case, turn, response)

    def provenance(self) -> dict[str, str]:
        """What, beyond config, would make two runs graded by this differ.

        Lands in the run context. Most evaluators are pure functions of their
        options and have nothing to add.
        """
        return {}

    # ---------------------------------------------------------------- helpers #

    def build(self, checks: Sequence[Check]) -> EvalResult:
        """Assemble a result: mean score, and pass only when every check passed."""
        if not checks:
            return EvalResult(evaluator=self.name, score=1.0, passed=True, skipped=True)
        return EvalResult(
            evaluator=self.name,
            score=sum(c.score for c in checks) / len(checks),
            passed=all(c.passed for c in checks),
            checks=list(checks),
        )

    @staticmethod
    def check(
        name: str,
        passed: bool,
        *,
        score: float | None = None,
        detail: str = "",
        severity: Severity = Severity.WARNING,
        expected: Any = None,
        actual: Any = None,
    ) -> Check:
        """Build a `Check`, defaulting the score to 1.0 on pass and 0.0 on fail."""
        return Check(
            name=name,
            passed=passed,
            score=(1.0 if passed else 0.0) if score is None else round(score, 4),
            detail=detail,
            severity=severity,
            expected=expected,
            actual=actual,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"
