"""Regression comparator: candidate run versus the approved baseline.

This is the gate you put in CI. It answers four questions:

  1. Did any case that used to pass start failing?  (the blocking one)
  2. Did the overall score drop beyond tolerance?
  3. Which suites moved, and by how much?
  4. What changed case by case: newly failing, newly passing, or only the score?

A baseline is a run promoted with `askback baseline save`, and kept as a summary:
each case's verdict and score, the metric means, and the run context. It holds
no reply, no turn and no passage, because it is committed. The file is there to
be reviewed when it changes, and a re-baseline that rewrote a megabyte of
transcript would bury the one line that says which case moved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import DatasetError
from ..core.logging import get_logger
from .results import CaseResult, RunReport

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BaselineCase:
    """One case as a baseline remembers it: how it was graded, and nothing it said."""

    case_id: str
    suite: str
    passed: bool
    score: float
    route: str = ""
    status: str = ""
    #: Set only for a case that was sampled, so its K-of-N outcome and whether it
    #: was flaky survive being summarised. Counts, never the samples themselves.
    passing_samples: int | None = None
    samples_planned: int | None = None
    flaky: bool | None = None

    @classmethod
    def from_case(cls, case: CaseResult) -> BaselineCase:
        return cls(
            case_id=case.case_id,
            suite=case.suite,
            passed=case.passed,
            score=round(case.score, 4),
            route=case.route,
            status=case.status,
            passing_samples=case.passing_samples if case.sampled else None,
            samples_planned=case.samples_planned if case.sampled else None,
            flaky=case.flaky if case.sampled else None,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "case_id": self.case_id,
            "suite": self.suite,
            "passed": self.passed,
            "score": self.score,
            "route": self.route,
            "status": self.status,
        }
        if self.samples_planned is not None:
            data.update(
                passing_samples=self.passing_samples,
                samples_planned=self.samples_planned,
                flaky=self.flaky,
            )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaselineCase:
        return cls(
            case_id=str(data["case_id"]),
            suite=str(data.get("suite", "")),
            passed=bool(data["passed"]),
            score=float(data["score"]),
            route=str(data.get("route", "")),
            status=str(data.get("status", "")),
            passing_samples=data.get("passing_samples"),
            samples_planned=data.get("samples_planned"),
            flaky=data.get("flaky"),
        )


@dataclass(slots=True)
class Baseline:
    """The approved run, reduced to what a later run is compared against.

    The aggregates are the ones the run reported, stored, not recomputed here: a
    summary has no checks to count critical failures from, and one worked out
    from what is left would say zero.

    `summary()` and `by_suite()` answer as a `RunReport` does, so the comparator
    reads either without knowing which it was given.
    """

    run_id: str
    finished_at: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    totals: dict[str, Any] = field(default_factory=dict)
    suites: dict[str, dict[str, Any]] = field(default_factory=dict)
    evaluators: dict[str, dict[str, Any]] = field(default_factory=dict)
    cases: list[BaselineCase] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return self.totals

    def by_suite(self) -> dict[str, dict[str, Any]]:
        return self.suites

    @classmethod
    def from_report(cls, report: RunReport) -> Baseline:
        # Latency is a fact about the machine, not the agent: kept, it would
        # change on every re-baseline and say nothing.
        totals = {k: v for k, v in report.summary().items() if k != "mean_latency_ms"}
        return cls(
            run_id=report.run_id,
            finished_at=report.finished_at,
            context=dict(report.context),
            totals=totals,
            suites=report.by_suite(),
            evaluators=report.by_evaluator(),
            # In id order, not dataset order: reordering a suite file is then not a diff.
            cases=sorted(
                (BaselineCase.from_case(c) for c in report.cases),
                key=lambda c: (c.suite, c.case_id),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "baseline",
            "run_id": self.run_id,
            "finished_at": self.finished_at,
            "context": self.context,
            "summary": self.totals,
            "by_suite": self.suites,
            "by_evaluator": self.evaluators,
            "cases": [c.to_dict() for c in self.cases],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Baseline:
        """Read a baseline, or a whole run report used as one.

        Refuses a file with no recorded summary instead of working one out: the
        mean and pass rate a candidate is held to must be the ones that were
        approved.
        """
        try:
            return cls(
                run_id=str(data.get("run_id", "")),
                finished_at=str(data.get("finished_at", "")),
                context=dict(data.get("context", {})),
                totals=dict(data["summary"]),
                suites={str(k): dict(v) for k, v in data["by_suite"].items()},
                evaluators={str(k): dict(v) for k, v in data.get("by_evaluator", {}).items()},
                cases=[BaselineCase.from_dict(c) for c in data["cases"]],
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DatasetError(f"Not a readable baseline ({type(exc).__name__}: {exc}).") from exc

    def dumps(self) -> str:
        """The file's text: indented, with one case per line.

        A case whose verdict moved is then exactly one changed line in the diff.
        """
        head = self.to_dict()
        cases = head.pop("cases")
        lines = ",\n".join(f"    {json.dumps(c)}" for c in cases)
        return f'{json.dumps(head, indent=2)[:-2]},\n  "cases": [\n{lines}\n  ]\n}}\n'


@dataclass(slots=True)
class CaseDelta:
    """How one case moved between the baseline and the candidate."""

    case_id: str
    suite: str = ""
    baseline_score: float = 0.0
    candidate_score: float = 0.0
    baseline_passed: bool = False
    candidate_passed: bool = False
    #: regressed | improved | unchanged | new | missing
    verdict: str = "unchanged"

    @property
    def delta(self) -> float:
        return round(self.candidate_score - self.baseline_score, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "suite": self.suite,
            "baseline_score": round(self.baseline_score, 4),
            "candidate_score": round(self.candidate_score, 4),
            "delta": self.delta,
            "baseline_passed": self.baseline_passed,
            "candidate_passed": self.candidate_passed,
            "verdict": self.verdict,
        }


@dataclass(slots=True)
class SuiteDelta:
    """How one suite moved between the baseline and the candidate.

    A side that did not run the suite is None, not a row of zeros: a suite added
    since the baseline has no pass rate to have risen from, and its deltas are
    None with it.
    """

    suite: str
    #: The suite's `RunReport.by_suite()` row on each side.
    baseline: dict[str, Any] | None = None
    candidate: dict[str, Any] | None = None
    newly_failing: int = 0
    newly_passing: int = 0

    def _delta(self, key: str) -> float | None:
        if self.baseline is None or self.candidate is None:
            return None
        return round(float(self.candidate[key]) - float(self.baseline[key]), 4)

    @property
    def pass_rate_delta(self) -> float | None:
        return self._delta("pass_rate")

    @property
    def mean_delta(self) -> float | None:
        return self._delta("mean_score")

    @property
    def moved(self) -> bool:
        """False only when both runs had the suite and nothing about it changed."""
        return bool(
            self.pass_rate_delta is None
            or self.pass_rate_delta
            or abs(self.mean_delta or 0.0) > RegressionComparator.NOISE_FLOOR
            or self.newly_failing
            or self.newly_passing
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "pass_rate_delta": self.pass_rate_delta,
            "mean_delta": self.mean_delta,
            "newly_failing": self.newly_failing,
            "newly_passing": self.newly_passing,
        }


@dataclass(slots=True)
class RegressionReport:
    """The comparison result, and whether the build should be allowed through."""

    passed: bool = True
    tolerance: float = 0.05
    baseline_mean: float = 0.0
    candidate_mean: float = 0.0
    baseline_pass_rate: float = 0.0
    candidate_pass_rate: float = 0.0
    #: Every case that got worse or better, by verdict or by score alone.
    regressed: list[CaseDelta] = field(default_factory=list)
    improved: list[CaseDelta] = field(default_factory=list)
    #: The subsets whose verdict flipped. `newly_failing` is what blocks a build.
    newly_failing: list[CaseDelta] = field(default_factory=list)
    newly_passing: list[CaseDelta] = field(default_factory=list)
    #: Every suite either run had, in name order.
    suites: list[SuiteDelta] = field(default_factory=list)
    new_cases: list[str] = field(default_factory=list)
    missing_cases: list[str] = field(default_factory=list)
    #: Populated when the two runs used different providers or thresholds.
    context_warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def mean_delta(self) -> float:
        return round(self.candidate_mean - self.baseline_mean, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "tolerance": self.tolerance,
            "baseline_mean": round(self.baseline_mean, 4),
            "candidate_mean": round(self.candidate_mean, 4),
            "mean_delta": self.mean_delta,
            "baseline_pass_rate": round(self.baseline_pass_rate, 4),
            "candidate_pass_rate": round(self.candidate_pass_rate, 4),
            "regressed": [d.to_dict() for d in self.regressed],
            "improved": [d.to_dict() for d in self.improved],
            "newly_failing": [d.to_dict() for d in self.newly_failing],
            "newly_passing": [d.to_dict() for d in self.newly_passing],
            "suites": [s.to_dict() for s in self.suites],
            "new_cases": self.new_cases,
            "missing_cases": self.missing_cases,
            "context_warnings": self.context_warnings,
            "reasons": self.reasons,
        }


class RegressionComparator:
    """Compares a candidate run against a baseline run."""

    #: Score movement smaller than this is noise, not a change.
    NOISE_FLOOR = 0.001

    def __init__(self, *, tolerance: float = 0.05, block_on_new_failures: bool = True) -> None:
        self.tolerance = tolerance
        # A case that used to pass and now fails blocks the build, however small
        # the score change. That is the whole point of a regression gate.
        self.block_on_new_failures = block_on_new_failures

    def compare(self, baseline: Baseline | RunReport, candidate: RunReport) -> RegressionReport:
        base_cases = {c.case_id: c for c in baseline.cases}
        cand_cases = {c.case_id: c for c in candidate.cases}

        report = RegressionReport(
            tolerance=self.tolerance,
            baseline_mean=baseline.summary()["mean_score"],
            candidate_mean=candidate.summary()["mean_score"],
            baseline_pass_rate=baseline.summary()["pass_rate"],
            candidate_pass_rate=candidate.summary()["pass_rate"],
            new_cases=sorted(set(cand_cases) - set(base_cases)),
            missing_cases=sorted(set(base_cases) - set(cand_cases)),
            context_warnings=_context_warnings(baseline.context, candidate.context),
        )

        for case_id in sorted(set(base_cases) & set(cand_cases)):
            base, cand = base_cases[case_id], cand_cases[case_id]
            delta = CaseDelta(
                case_id=case_id,
                suite=cand.suite,
                baseline_score=base.score,
                candidate_score=cand.score,
                baseline_passed=base.passed,
                candidate_passed=cand.passed,
            )
            if base.passed and not cand.passed:
                delta.verdict = "regressed"
                report.regressed.append(delta)
                report.newly_failing.append(delta)
            elif not base.passed and cand.passed:
                delta.verdict = "improved"
                report.improved.append(delta)
                report.newly_passing.append(delta)
            elif delta.delta < -self.tolerance:
                delta.verdict = "regressed"
                report.regressed.append(delta)
            elif delta.delta > self.NOISE_FLOOR:
                delta.verdict = "improved"
                report.improved.append(delta)

        base_suites, cand_suites = baseline.by_suite(), candidate.by_suite()
        report.suites = [
            SuiteDelta(
                suite=name,
                baseline=base_suites.get(name),
                candidate=cand_suites.get(name),
                newly_failing=sum(1 for d in report.newly_failing if d.suite == name),
                newly_passing=sum(1 for d in report.newly_passing if d.suite == name),
            )
            for name in sorted(base_suites.keys() | cand_suites.keys())
        ]

        if self.block_on_new_failures and report.newly_failing:
            report.passed = False
            report.reasons.append(
                f"{len(report.newly_failing)} case(s) that passed in the baseline now fail: "
                f"{', '.join(d.case_id for d in report.newly_failing[:10])}"
            )

        if report.mean_delta < -self.tolerance:
            report.passed = False
            report.reasons.append(
                f"Mean score dropped by {abs(report.mean_delta):.3f}, "
                f"beyond the {self.tolerance:.3f} tolerance."
            )

        if report.missing_cases:
            report.reasons.append(
                f"{len(report.missing_cases)} baseline case(s) were not run: "
                f"{', '.join(report.missing_cases[:10])}"
            )

        if report.passed and not report.reasons:
            report.reasons.append("No regressions detected.")
        return report


def _context_warnings(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Flag configuration differences that make two runs hard to compare.

    Every key both runs recorded is compared, not a fixed list: an adapter
    chooses its own provenance keys, so a list kept here would go stale the
    first time one was added. A key only one run recorded is not a difference -
    the other simply did not know about it.
    """
    warnings: list[str] = []
    for key in sorted(baseline.keys() & candidate.keys()):
        old, new = baseline[key], candidate[key]
        if old is not None and new is not None and old != new:
            warnings.append(f"{key} changed: {old!r} -> {new!r}")
    return warnings


def load_baseline(path: str | Path) -> Baseline:
    """Read the approved baseline from disk."""
    p = Path(path)
    if not p.exists():
        raise DatasetError(
            f"No baseline at {p}. Create one with: askback baseline save"
        )
    return Baseline.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_baseline(report: RunReport, path: str | Path) -> Path:
    """Promote a run to be the approved baseline, keeping its summary only.

    Whatever redaction the report was written at, none of its text is kept, so
    promoting a local `full` report cannot put a reply into the repository.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(Baseline.from_report(report).dumps(), encoding="utf-8", newline="\n")
    log.info("Baseline saved to %s (%d cases)", p, len(report.cases))
    return p
