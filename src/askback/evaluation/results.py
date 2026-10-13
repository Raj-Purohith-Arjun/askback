"""Run results: what one evaluated case produced, and what a whole run produced.

These structures are the wire format for JSON reports, so they round-trip
through `to_dict` / `from_dict`: reading a report back and writing it again
gives the same report. The JSON is the canonical form, and the other formats are
rendered from a report read back out of it, so anything `from_dict` dropped
would be missing from them too. A baseline is not one of these: it is a summary
of a run, and `regression.Baseline` owns its format.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.contracts import ChatResponse
from ..core.errors import DatasetError
from .base import Check, EvalResult, JudgeUsage
from .clarification import Detection, PersonaUsage, Round


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class TurnResult:
    """One turn of a case's conversation: what was sent and what came back."""

    #: 1-based, like the loader's "turn N" in a dataset error.
    index: int
    user: str
    #: False for a setup turn, which is sent and never graded.
    evaluated: bool = True
    reply: str = ""
    route: str = ""
    status: str = ""
    latency_ms: float = 0.0
    #: None until the turn is graded. A setup turn, or one the adapter could not
    #: deliver, has no verdict - which is not the same thing as a passing one.
    score: float | None = None
    passed: bool | None = None
    results: list[EvalResult] = field(default_factory=list)
    error: str | None = None
    #: Every question the agent asked on the way to `reply`, and what we answered.
    rounds: list[Round] = field(default_factory=list)
    #: How the final reply was classified. Written only when it is a question, was ruled
    #: out as one, or questions were answered on the way; a plain answer says nothing.
    detection: Detection | None = None
    #: Set when the agent asked and we had nothing to say: a gap in the suite, not a
    #: failure of the agent. The turn is neither graded nor passed.
    unmeasured: str | None = None

    @property
    def persona_usage(self) -> PersonaUsage | None:
        usages = [r.usage for r in self.rounds if r.usage is not None]
        return sum(usages[1:], usages[0]) if usages else None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "user": self.user,
            "evaluated": self.evaluated,
            "reply": self.reply,
            "route": self.route,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "score": self.score,
            "passed": self.passed,
            "error": self.error,
            "results": [r.to_dict() for r in self.results],
        }
        # Each only when it has something to say, so a turn with no questions in it
        # serialises exactly as it did before clarification existed.
        if self.rounds:
            data["rounds"] = [r.to_dict() for r in self.rounds]
        if self.detection is not None and (
            self.detection.is_question or self.detection.suppressed or self.rounds
        ):
            data["detection"] = self.detection.to_dict()
        if self.unmeasured is not None:
            data["unmeasured"] = self.unmeasured
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TurnResult:
        score, passed = data.get("score"), data.get("passed")
        return cls(
            index=int(data["index"]),
            user=str(data.get("user", "")),
            evaluated=bool(data.get("evaluated", True)),
            reply=str(data.get("reply", "")),
            route=str(data.get("route", "")),
            status=str(data.get("status", "")),
            latency_ms=float(data.get("latency_ms", 0.0)),
            # None stays None: an ungraded turn must not come back as a failed one.
            score=None if score is None else float(score),
            passed=None if passed is None else bool(passed),
            results=[EvalResult.from_dict(r) for r in data.get("results", [])],
            error=data.get("error"),
            rounds=[Round.from_dict(r) for r in data.get("rounds", [])],
            detection=Detection.from_dict(data["detection"]) if data.get("detection") else None,
            unmeasured=data.get("unmeasured"),
        )


@dataclass(slots=True)
class SampleResult:
    """One run of a case's whole conversation, in a session of its own.

    `message`, `reply`, `route`, `status`, `latency_ms` and `response` describe
    the last graded turn that ran, as they do on `CaseResult`. A sample passes
    only if every graded turn did; one the adapter could not deliver never does.
    """

    #: 1-based position among the case's samples.
    index: int
    session_id: str
    message: str
    score: float = 0.0
    passed: bool = False
    reply: str = ""
    route: str = ""
    status: str = ""
    latency_ms: float = 0.0
    response: dict[str, Any] = field(default_factory=dict)
    results: list[EvalResult] = field(default_factory=list)
    error: str | None = None
    #: Every turn that was attempted. Turns after an error are absent, not blank.
    turns: list[TurnResult] = field(default_factory=list)
    #: Why the sample could not be measured (see `TurnResult.unmeasured`). Not an error.
    unmeasured: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "session_id": self.session_id,
            "score": round(self.score, 4),
            "passed": self.passed,
            "error": self.error,
            "response": self.response,
            "turns": [t.to_dict() for t in self.turns],
        }
        if self.unmeasured is not None:
            data["unmeasured"] = self.unmeasured
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SampleResult:
        return cls.from_turns(
            [TurnResult.from_dict(t) for t in data.get("turns", [])],
            index=int(data["index"]),
            session_id=str(data.get("session_id", "")),
            score=float(data.get("score", 0.0)),
            passed=bool(data["passed"]),
            response=dict(data.get("response") or {}),
            error=data.get("error"),
            unmeasured=data.get("unmeasured"),
        )

    @classmethod
    def from_turns(
        cls,
        turns: list[TurnResult],
        *,
        index: int,
        session_id: str,
        score: float,
        passed: bool,
        response: dict[str, Any],
        error: str | None,
        unmeasured: str | None = None,
    ) -> SampleResult:
        """Rebuild a sample around its turns, re-deriving what `to_dict` leaves out.

        The exchange a sample shows is its last graded turn that ran, and its
        results are its graded turns' results; neither is written twice.
        """
        graded = [t for t in turns if t.evaluated and t.error is None]
        shown = graded[-1] if graded else None
        return cls(
            index=index,
            session_id=session_id,
            message=shown.user if shown else (turns[0].user if turns else ""),
            score=score,
            passed=passed,
            reply=shown.reply if shown else "",
            route=shown.route if shown else "",
            status=shown.status if shown else "",
            latency_ms=shown.latency_ms if shown else 0.0,
            response=response,
            results=[r for t in turns if t.evaluated and t.error is None for r in t.results],
            error=error,
            turns=turns,
            unmeasured=unmeasured,
        )


@dataclass(slots=True)
class CaseResult:
    """One test case, run and graded: one sample, or several when it declares them.

    `message`, `reply`, `route`, `status`, `latency_ms`, `response` and `turns`
    are the representative sample's - the first one that failed, else the last -
    so a reader that knows nothing about samples or turns sees one coherent
    exchange, and for a case run once that is simply the run. `results` is every
    graded turn's results from every sample that ran: a critical failure in any
    of them gates the run, however the case's K-of-N verdict came out.
    """

    case_id: str
    suite: str
    message: str
    score: float = 0.0
    passed: bool = False
    description: str = ""
    risk: str = "low"
    tags: list[str] = field(default_factory=list)
    reply: str = ""
    route: str = ""
    status: str = ""
    latency_ms: float = 0.0
    results: list[EvalResult] = field(default_factory=list)
    #: Set when the case could not be run at all (an exception, not a low score).
    error: str | None = None
    response: dict[str, Any] = field(default_factory=dict)
    #: Every sample that ran. Fewer than `samples_planned` when the gate ended early.
    samples: list[SampleResult] = field(default_factory=list)
    samples_planned: int = 1
    #: The K of the K-of-N gate this case was judged against.
    min_passing_samples: int = 1
    #: The representative sample's `unmeasured`: our gap, so the case neither passed nor
    #: counts as the agent failing it.
    unmeasured: str | None = None

    @property
    def representative(self) -> SampleResult | None:
        return next((s for s in self.samples if not s.passed), self.samples[-1] if self.samples else None)

    @property
    def turns(self) -> list[TurnResult]:
        sample = self.representative
        return sample.turns if sample else []

    @property
    def passing_samples(self) -> int:
        return sum(1 for s in self.samples if s.passed)

    @property
    def sampled(self) -> bool:
        """True when the case asked for more than one run, so non-determinism was measured."""
        return self.samples_planned > 1

    @property
    def flaky(self) -> bool:
        """Some samples passed and some did not: the same input gave different verdicts."""
        return 0 < self.passing_samples < len(self.samples)

    @property
    def judge_usage(self) -> JudgeUsage | None:
        """What the judge spent on every graded turn of every sample. None: no metered judge."""
        usages = [r.judge for r in self.results if r.judge is not None]
        return sum(usages[1:], usages[0]) if usages else None

    @property
    def persona_usage(self) -> PersonaUsage | None:
        """What the simulated user cost across every turn of every sample. None: no persona ran."""
        usages = [u for s in self.samples for t in s.turns if (u := t.persona_usage) is not None]
        return sum(usages[1:], usages[0]) if usages else None

    @property
    def degraded_results(self) -> list[EvalResult]:
        """Results graded by the fallback because the judge that was asked for failed."""
        return [r for r in self.results if r.degraded is not None]

    @property
    def failed_checks(self) -> list[Check]:
        return [c for r in self.results for c in r.failures]

    @property
    def critical_checks(self) -> list[Check]:
        return [c for r in self.results for c in r.critical_failures]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "case_id": self.case_id,
            "suite": self.suite,
            "message": self.message,
            "description": self.description,
            "risk": self.risk,
            "tags": self.tags,
            "score": round(self.score, 4),
            "passed": self.passed,
            "reply": self.reply,
            "route": self.route,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "results": [r.to_dict() for r in self.results],
            "response": self.response,
            "turns": [t.to_dict() for t in self.turns],
        }
        if self.unmeasured is not None:
            data["unmeasured"] = self.unmeasured
        # Only a case that asked for sampling says anything about it, so a case
        # run once serialises exactly as it did before samples existed.
        if self.sampled:
            data.update(
                samples_planned=self.samples_planned,
                min_passing_samples=self.min_passing_samples,
                passing_samples=self.passing_samples,
                flaky=self.flaky,
                samples=[s.to_dict() for s in self.samples],
            )
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CaseResult:
        """Rebuild the whole case, samples and checks included.

        A case run once wrote no `samples`, so its one run is rebuilt from the
        case-level fields, and it serialises again exactly as it was read.
        """
        passed, error = bool(data["passed"]), data.get("error")
        score = float(data.get("score", 0.0))
        response = dict(data.get("response") or {})
        if "samples" in data:
            samples = [SampleResult.from_dict(s) for s in data["samples"]]
        else:
            samples = [
                SampleResult.from_turns(
                    [TurnResult.from_dict(t) for t in data.get("turns", [])],
                    index=1,
                    session_id="",
                    score=score,
                    passed=passed,
                    response=response,
                    error=error,
                    unmeasured=data.get("unmeasured"),
                )
            ]
        return cls(
            case_id=str(data["case_id"]),
            suite=str(data.get("suite", "")),
            message=str(data.get("message", "")),
            score=score,
            passed=passed,
            description=str(data.get("description", "")),
            risk=str(data.get("risk", "low")),
            tags=[str(t) for t in data.get("tags", [])],
            reply=str(data.get("reply", "")),
            route=str(data.get("route", "")),
            status=str(data.get("status", "")),
            latency_ms=float(data.get("latency_ms", 0.0)),
            results=[EvalResult.from_dict(r) for r in data.get("results", [])],
            error=error,
            response=response,
            samples=samples,
            samples_planned=int(data.get("samples_planned", 1)),
            min_passing_samples=int(data.get("min_passing_samples", 1)),
            unmeasured=data.get("unmeasured"),
        )


@dataclass(slots=True)
class RunReport:
    """Every case in one execution, plus the context needed to reproduce it."""

    run_id: str
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    duration_s: float = 0.0
    #: Provider, retriever, prompt version and thresholds. Two runs are only
    #: comparable when this matches; the regression comparator says so if not.
    context: dict[str, Any] = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)
    #: Set when the run stopped before every case ran (judge budget exhausted).
    #: Such a report has no verdict: its pass rate is about cases that happened to
    #: run first, not about the suite.
    incomplete: dict[str, Any] | None = None
    #: How much of its text has been withheld: full | ci | metrics_only. A report
    #: straight from the runner is `full`; `reporting.redaction` is what changes it.
    redaction: str = "full"

    # ------------------------------------------------------------- summaries #

    @property
    def judge_usage(self) -> JudgeUsage | None:
        """The judge's total spend across the run; None when no metered judge ran."""
        usages = [u for c in self.cases if (u := c.judge_usage) is not None]
        return sum(usages[1:], usages[0]) if usages else None

    @property
    def persona_usage(self) -> PersonaUsage | None:
        """The simulated user's total spend; None when no persona ran. Never part of the judge's."""
        usages = [u for c in self.cases if (u := c.persona_usage) is not None]
        return sum(usages[1:], usages[0]) if usages else None

    @property
    def degraded_results(self) -> list[tuple[CaseResult, EvalResult]]:
        return [(c, r) for c in self.cases for r in c.degraded_results]

    @property
    def sampled(self) -> bool:
        return any(c.sampled for c in self.cases)

    @property
    def flaky_cases(self) -> list[CaseResult]:
        return [c for c in self.cases if c.flaky]

    def summary(self) -> dict[str, Any]:
        total = len(self.cases)
        passed = sum(1 for c in self.cases if c.passed)
        errors = sum(1 for c in self.cases if c.error)
        summary: dict[str, Any] = {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "errors": errors,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "mean_score": round(sum(c.score for c in self.cases) / total, 4) if total else 0.0,
            "critical_failures": sum(len(c.critical_checks) for c in self.cases),
            "mean_latency_ms": round(
                sum(c.latency_ms for c in self.cases) / total, 2
            ) if total else 0.0,
        }
        # Absent, not zero: "0 unmeasured" would claim clarification was looked for.
        if unmeasured := sum(1 for c in self.cases if c.unmeasured):
            summary["unmeasured"] = unmeasured
        if (persona := self.persona_usage) is not None:
            summary.update(
                persona_calls=persona.calls,
                persona_prompt_tokens=persona.prompt_tokens,
                persona_completion_tokens=persona.completion_tokens,
                persona_cost_usd=round(persona.cost_usd, 6),
            )
        # Absent, not zero, when nothing was sampled: "0 flaky" is a finding
        # about cases that were run more than once, and none were.
        if self.sampled:
            summary["flaky"] = len(self.flaky_cases)
        # Likewise absent for a free judge: zero calls would claim a metered one ran.
        if (usage := self.judge_usage) is not None:
            summary.update(
                judge_calls=usage.calls,
                judge_cache_hits=usage.cache_hits,
                judge_prompt_tokens=usage.prompt_tokens,
                judge_completion_tokens=usage.completion_tokens,
                judge_cost_usd=round(usage.cost_usd, 6),
                judge_degraded=len(self.degraded_results),
            )
        return summary

    def by_suite(self) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[CaseResult]] = defaultdict(list)
        for case in self.cases:
            buckets[case.suite].append(case)
        return {
            suite: {
                "total": len(items),
                "passed": sum(1 for c in items if c.passed),
                "pass_rate": round(sum(1 for c in items if c.passed) / len(items), 4),
                "mean_score": round(sum(c.score for c in items) / len(items), 4),
            }
            for suite, items in sorted(buckets.items())
        }

    def by_evaluator(self) -> dict[str, dict[str, Any]]:
        scores: dict[str, list[float]] = defaultdict(list)
        failures: dict[str, int] = defaultdict(int)
        for case in self.cases:
            for result in case.results:
                if result.skipped:
                    continue
                scores[result.evaluator].append(result.score)
                failures[result.evaluator] += 0 if result.passed else 1
        return {
            name: {
                "cases": len(values),
                "mean_score": round(sum(values) / len(values), 4),
                "failed": failures[name],
            }
            for name, values in sorted(scores.items())
        }

    def failing_checks(self) -> dict[str, int]:
        """Which named checks fail most often. The first thing to read in a report."""
        counts: dict[str, int] = defaultdict(int)
        for case in self.cases:
            for check in case.failed_checks:
                counts[check.name] += 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "redaction": self.redaction,
            "context": self.context,
            "summary": self.summary(),
            "by_suite": self.by_suite(),
            "by_evaluator": self.by_evaluator(),
            "failing_checks": self.failing_checks(),
            "cases": [c.to_dict() for c in self.cases],
        }
        if self.incomplete is not None:
            data["incomplete"] = self.incomplete
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunReport:
        """Read a report back whole. The summaries are recomputed from its cases.

        A verdict, a severity or a case id that is not there is refused, not
        defaulted: a baseline that read as passing because a key was missing
        would hide every regression measured against it.
        """
        # A baseline has the same top-level keys and none of the checks. Read as
        # a report it would look like a run with no critical failures.
        if data.get("kind") == "baseline":
            raise DatasetError(
                "This file is a baseline summary, not a run report: it holds verdicts "
                "only, and cannot be rendered or promoted again."
            )
        try:
            return cls(
                run_id=str(data.get("run_id", "")),
                started_at=str(data.get("started_at", "")),
                finished_at=str(data.get("finished_at", "")),
                duration_s=float(data.get("duration_s", 0.0)),
                context=dict(data.get("context", {})),
                cases=[CaseResult.from_dict(c) for c in data.get("cases", [])],
                incomplete=dict(data["incomplete"]) if data.get("incomplete") else None,
                # Reports older than the redaction layer were written whole.
                redaction=str(data.get("redaction", "full")),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DatasetError(
                f"Not a readable run report ({type(exc).__name__}: {exc})."
            ) from exc


def response_snapshot(response: ChatResponse, *, max_chunks: int = 5) -> dict[str, Any]:
    """A compact, JSON-safe view of a response for the report.

    Unredacted, like everything the runner produces: the reply is whole and the
    top chunks carry their text, so a local `full` report can show what the agent
    read. Whether any of it reaches a file is decided in one place, by
    `reporting.redaction`, which withholds chunk text from every mode but `full`.
    """
    return {
        "reply": response.reply,
        "route": response.route,
        "status": response.status.value,
        "intent": response.intent.name,
        "intent_confidence": round(response.intent.confidence, 4),
        "citations": [c.doc_id for c in response.citations],
        "retrieved": [
            {"doc_id": c.doc_id, "score": c.score, "title": c.title, "text": c.text}
            for c in response.retrieved[:max_chunks]
        ],
        "tool_calls": [
            {"name": t.name, "ok": t.ok, "error": t.error} for t in response.tool_calls
        ],
        "safety": {
            "blocked": response.safety.blocked,
            "injection_detected": response.safety.injection_detected,
            "risk_score": response.safety.risk_score,
            "reasons": response.safety.reasons,
        },
        "latency_ms": response.latency_ms,
        "trace_id": response.trace_id,
    }


def merge_reports(reports: Sequence[RunReport], run_id: str) -> RunReport:
    """Combine several runs into one report. Used when suites run separately."""
    merged = RunReport(run_id=run_id)
    for report in reports:
        merged.cases.extend(report.cases)
        merged.context.update(report.context)
    merged.finished_at = _now()
    merged.duration_s = sum(r.duration_s for r in reports)
    return merged
