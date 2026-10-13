"""Run-level gates: the decisions that turn a report into an exit code.

Separate from the CLI because a decision and its presentation are different
concerns, and because more than one caller needs the decision. The CLI renders
these verdicts to a terminal; CI reads the exit code; the dashboard wants to show
which gate failed without shelling out through argparse.

Each gate returns a `GateVerdict` carrying its own name, outcome, reason and
offending cases. Nothing here prints. A gate with nothing to measure says so
(`unchecked`) instead of defaulting either way - the same rule the evaluators
follow for a case they cannot assess.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.config import max_degraded
from .base import Check
from .results import CaseResult, RunReport

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REGRESSION = 2
EXIT_ERROR = 3

PASS_RATE = "pass_rate"
CRITICAL_SEVERITY = "critical_severity"
SUITE_FLOOR = "suite_floor"
JUDGE_DEGRADED = "judge_degraded"
JUDGE_BUDGET = "judge_budget"


@dataclass(frozen=True, slots=True)
class Offender:
    """One case that caused a gate to fail, and the check that did it."""

    case_id: str
    suite: str
    check: str
    detail: str

    @property
    def label(self) -> str:
        return f"{self.suite}/{self.case_id}"


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """What one gate concluded. `reason` is a full sentence; callers print it."""

    gate: str
    passed: bool
    exit_code: int
    reason: str
    offenders: tuple[Offender, ...] = ()
    #: The gate had nothing it could measure - a floor whose suite was
    #: deliberately excluded from the selection, say. Not a pass, not a failure.
    unchecked: bool = False
    #: Suite name for a floor gate; empty for the run-wide gates.
    subject: str = ""

    @property
    def status(self) -> str:
        """PASSED / FAILED / ERROR / UNCHECKED, for whoever renders this."""
        if self.unchecked:
            return "UNCHECKED"
        if self.passed:
            return "PASSED"
        return "ERROR" if self.exit_code == EXIT_ERROR else "FAILED"


@dataclass(frozen=True, slots=True)
class RunVerdict:
    """Every gate's verdict, and the one exit code that follows from them."""

    gates: tuple[GateVerdict, ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        """The worst outcome wins, so an operator error outranks a case failure."""
        return max((gate.exit_code for gate in self.gates), default=EXIT_OK)

    @property
    def passed(self) -> bool:
        return self.exit_code == EXIT_OK

    @property
    def failures(self) -> tuple[GateVerdict, ...]:
        return tuple(g for g in self.gates if not g.passed and not g.unchecked)


def critical_failures(report: RunReport) -> list[tuple[CaseResult, Check]]:
    """Every failing CRITICAL check in the run, paired with the case that produced it."""
    return [(case, check) for case in report.cases for check in case.critical_checks]


def pass_rate_gate(report: RunReport, settings: Mapping[str, Any]) -> GateVerdict:
    required = float(settings.get("suite_pass_rate", 0.8))
    actual = float(report.summary()["pass_rate"])
    if actual < required:
        return GateVerdict(
            gate=PASS_RATE,
            passed=False,
            exit_code=EXIT_FAILED,
            reason=f"pass rate {actual:.0%} is below the required {required:.0%}.",
        )
    return GateVerdict(
        gate=PASS_RATE,
        passed=True,
        exit_code=EXIT_OK,
        reason=f"pass rate {actual:.0%} meets the required {required:.0%}.",
    )


def critical_severity_gate(report: RunReport) -> GateVerdict:
    """Fail on any CRITICAL check, at any pass rate.

    Severity outranks arithmetic. The cases carrying that severity are the ones
    whose failure is an incident rather than a score, so averaging them into a
    rate is what hid six of them behind a green build.
    """
    failures = critical_failures(report)
    if not failures:
        return GateVerdict(
            gate=CRITICAL_SEVERITY,
            passed=True,
            exit_code=EXIT_OK,
            reason="no critical checks failed.",
        )
    return GateVerdict(
        gate=CRITICAL_SEVERITY,
        passed=False,
        exit_code=EXIT_FAILED,
        reason=(
            f"{len(failures)} critical check(s) failed, which gates the run "
            f"regardless of pass rate."
        ),
        offenders=tuple(
            Offender(
                case_id=case.case_id,
                suite=case.suite,
                check=check.name,
                detail=check.detail or "(no detail)",
            )
            for case, check in failures
        ),
    )


def _floor_breached(report: RunReport, suite: str, rate: float, minimum: float, tally: str) -> GateVerdict:
    return GateVerdict(
        gate=SUITE_FLOOR,
        passed=False,
        exit_code=EXIT_FAILED,
        reason=(
            f"suite '{suite}' pass rate {rate:.0%} is below its required "
            f"{minimum:.0%} ({tally})."
        ),
        offenders=tuple(
            Offender(
                case_id=case.case_id,
                suite=case.suite,
                check=", ".join(c.name for c in case.failed_checks) or "(no failing check)",
                detail=case.error or next((c.detail for c in case.failed_checks if c.detail), "(no detail)"),
            )
            for case in report.cases
            if case.suite == suite and not case.passed
        ),
        subject=suite,
    )


def _floor_absent(suite: str, minimum: float, selection_filtered: bool) -> GateVerdict:
    """A named suite that did not run. Never a pass; sometimes not an error."""
    if selection_filtered:
        return GateVerdict(
            gate=SUITE_FLOOR,
            passed=False,
            exit_code=EXIT_OK,
            reason=(
                f"suite '{suite}' requires {minimum:.0%} but was excluded by the "
                f"case selection."
            ),
            unchecked=True,
            subject=suite,
        )
    return GateVerdict(
        gate=SUITE_FLOOR,
        passed=False,
        exit_code=EXIT_ERROR,
        reason=(
            f"suite '{suite}' requires a pass rate of {minimum:.0%} but did not run. "
            f"A floor that cannot be measured is not a pass."
        ),
        subject=suite,
    )


def suite_floor_gates(
    report: RunReport, settings: Mapping[str, Any], *, selection_filtered: bool = False
) -> list[GateVerdict]:
    """One verdict per `suite_must_pass` entry.

    The global rate cannot absorb these: the suites carrying the real risk are
    small, so a large easy suite can hold the average up while they rot.

    A named suite that did not run is never a pass. Under an explicit
    `--suite`/`--tag`/`--case` selection the operator meant to leave it out, so
    the floor is reported unchecked; in an unfiltered run the suite has gone
    missing from the datasets, which makes the floor unmeasurable - an operator
    problem, not a failing agent.
    """
    floors = settings.get("suite_must_pass") or {}
    if not isinstance(floors, Mapping):
        return [
            GateVerdict(
                gate=SUITE_FLOOR,
                passed=False,
                exit_code=EXIT_ERROR,
                reason=(
                    f"evaluation.suite_must_pass must be a mapping, "
                    f"got {type(floors).__name__}."
                ),
            )
        ]

    observed = report.by_suite()
    verdicts: list[GateVerdict] = []
    for suite, floor in sorted(floors.items(), key=lambda kv: str(kv[0])):
        name = str(suite)
        try:
            minimum = float(floor)
        except (TypeError, ValueError):
            verdicts.append(
                GateVerdict(
                    gate=SUITE_FLOOR,
                    passed=False,
                    exit_code=EXIT_ERROR,
                    reason=f"evaluation.suite_must_pass['{name}'] is not a number: {floor!r}.",
                    subject=name,
                )
            )
            continue

        data = observed.get(name)
        if data is None:
            verdicts.append(_floor_absent(name, minimum, selection_filtered))
            continue

        rate = float(data["pass_rate"])
        tally = f"{data['passed']}/{data['total']} passed"
        if rate < minimum:
            verdicts.append(_floor_breached(report, name, rate, minimum, tally))
        else:
            verdicts.append(
                GateVerdict(
                    gate=SUITE_FLOOR,
                    passed=True,
                    exit_code=EXIT_OK,
                    reason=(
                        f"suite '{name}' pass rate {rate:.0%} meets its required "
                        f"{minimum:.0%} ({tally})."
                    ),
                    subject=name,
                )
            )
    return verdicts


def judge_degraded_gate(report: RunReport, settings: Mapping[str, Any]) -> GateVerdict:
    """Fail when more judged responses fell back to the heuristic than `max_degraded` allows.

    An operator error, not a case failure: nothing is wrong with the agent, the
    grader was not available to say. Those responses were scored on a different
    scale from the rest, so a run with too many of them has no trustworthy mean.
    """
    allowed = max_degraded(settings)
    degraded = report.degraded_results
    if len(degraded) <= allowed:
        return GateVerdict(
            gate=JUDGE_DEGRADED,
            passed=True,
            exit_code=EXIT_OK,
            reason=f"{len(degraded)} judged response(s) degraded; max_degraded is {allowed}.",
        )
    return GateVerdict(
        gate=JUDGE_DEGRADED,
        passed=False,
        exit_code=EXIT_ERROR,
        reason=(
            f"{len(degraded)} judged response(s) were graded by the heuristic fallback because "
            f"the model judge failed; max_degraded is {allowed}. Their scores are not comparable "
            f"to the rest of the run."
        ),
        offenders=tuple(
            Offender(
                case_id=case.case_id,
                suite=case.suite,
                check=result.evaluator,
                detail=result.degraded or "(no reason recorded)",
            )
            for case, result in degraded
        ),
    )


def incomplete_gate(report: RunReport) -> GateVerdict:
    """The only verdict on a run that stopped early: it has none.

    Pass rate, floors and the critical gate describe the cases that ran first, not
    the suite, and a green one would read as a pass on cases nobody executed.
    """
    info = report.incomplete or {}
    return GateVerdict(
        gate=JUDGE_BUDGET,
        passed=False,
        exit_code=EXIT_ERROR,
        reason=(
            f"judge budget ${float(info.get('budget_usd', 0)):.4f} exceeded "
            f"(spent ${float(info.get('spent_usd', 0)):.4f}); run stopped after "
            f"{info.get('cases_run', len(report.cases))} of {info.get('cases_planned', '?')} "
            f"cases. The run is incomplete and reaches no verdict."
        ),
    )


def evaluate_gates(
    report: RunReport, settings: Mapping[str, Any], *, selection_filtered: bool = False
) -> RunVerdict:
    """Run every gate. All of them, always.

    A verdict that stops at the first failure hides the rest, and the next person
    re-runs the suite to learn what this run already knew. The exception is a run
    that stopped early: it has no verdict to hide the cause behind.
    """
    if report.incomplete is not None:
        return RunVerdict(gates=(incomplete_gate(report),))

    gates = [
        pass_rate_gate(report, settings),
        critical_severity_gate(report),
        *suite_floor_gates(report, settings, selection_filtered=selection_filtered),
    ]
    # Only a run a model judge took part in has anything to degrade from; a run on
    # the heuristic by choice prints exactly the gates it always did.
    if report.judge_usage is not None:
        gates.append(judge_degraded_gate(report, settings))
    return RunVerdict(gates=tuple(gates))
