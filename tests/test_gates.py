"""The CI gates: the decisions that turn a report into an exit code.

These tests exist because the gate was wrong in a way no case-level test could
catch: it computed a correct pass rate and then drew the wrong conclusion from
it. The reports here are built by hand rather than run through the engine, so a
regression in the verdict logic cannot hide behind a change in the simulator.

The CLI's rendering of these verdicts is tested in test_cli.py.
"""

from __future__ import annotations

from typing import Any

from askback.core.contracts import Severity
from askback.evaluation.base import Check, EvalResult
from askback.evaluation.gates import (
    CRITICAL_SEVERITY,
    EXIT_ERROR,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REGRESSION,
    PASS_RATE,
    SUITE_FLOOR,
    GateVerdict,
    RunVerdict,
    evaluate_gates,
)
from askback.evaluation.results import CaseResult, RunReport

SETTINGS: dict[str, Any] = {
    "suite_pass_rate": 0.8,
    "suite_must_pass": {"hallucination": 0.9, "adversarial": 0.95},
}

#: The six detections Phase 0 made visible. examples/hr/README.md explains each one.
KNOWN_CRITICAL_CASES = (
    "adv-benign-incident-report",
    "hall-sabbatical",
    "hall-crypto-expenses",
    "hall-quantum-certification",
    "hall-future-policy-date",
    "reason-professional-cert-prerequisites",
)


def case(
    case_id: str,
    suite: str,
    *,
    passed: bool = True,
    critical: str | None = None,
) -> CaseResult:
    """One graded case. `critical` names a failing CRITICAL check to attach."""
    results = []
    if critical is not None:
        results.append(
            EvalResult(
                evaluator="rule_based",
                passed=False,
                checks=[
                    Check(
                        name=critical,
                        passed=False,
                        detail="Turn ended as 'answered'.",
                        severity=Severity.CRITICAL,
                    )
                ],
            )
        )
    return CaseResult(
        case_id=case_id,
        suite=suite,
        message="q",
        score=1.0 if passed else 0.0,
        passed=passed,
        results=results,
    )


def report(*cases: CaseResult) -> RunReport:
    run = RunReport(run_id="r1")
    run.cases = list(cases)
    return run


def floors_met(*cases: CaseResult) -> tuple[CaseResult, ...]:
    """Pad a run so both named suites exist and clear their floors."""
    return (
        *cases,
        *(case(f"hall-{i}", "hallucination") for i in range(10)),
        *(case(f"adv-{i}", "adversarial") for i in range(20)),
    )


def gate_for(verdict: RunVerdict, gate: str, subject: str = "") -> GateVerdict:
    found = [g for g in verdict.gates if g.gate == gate and g.subject == subject]
    assert len(found) == 1, f"expected one {gate}/{subject!r} gate, got {len(found)}"
    return found[0]


def test_exit_codes_distinguish_regression_from_a_broken_harness() -> None:
    """CLAUDE.md section 5: CI must tell "quality regressed" from "harness broken"."""
    assert (EXIT_OK, EXIT_FAILED, EXIT_REGRESSION, EXIT_ERROR) == (0, 1, 2, 3)


class TestCriticalGate:
    def test_critical_failure_fails_a_run_at_a_full_pass_rate(self) -> None:
        run = report(*floors_met(case("hall-sabbatical", "golden", critical="expected_status")))
        assert run.summary()["pass_rate"] == 1.0

        verdict = evaluate_gates(run, SETTINGS)

        assert verdict.exit_code == EXIT_FAILED
        assert not verdict.passed
        assert gate_for(verdict, PASS_RATE).passed
        assert not gate_for(verdict, CRITICAL_SEVERITY).passed

    def test_the_critical_gate_names_the_case_and_the_check(self) -> None:
        run = report(*floors_met(case("hall-sabbatical", "golden", critical="expected_status")))

        offenders = gate_for(evaluate_gates(run, SETTINGS), CRITICAL_SEVERITY).offenders

        assert [o.label for o in offenders] == ["golden/hall-sabbatical"]
        assert offenders[0].check == "expected_status"
        assert offenders[0].detail == "Turn ended as 'answered'."

    def test_both_gates_are_reported_when_both_fail(self) -> None:
        run = report(
            case("a", "golden", passed=False, critical="pii_egress"),
            case("b", "golden"),
        )

        verdict = evaluate_gates(run, {"suite_pass_rate": 0.8})

        assert {g.gate for g in verdict.failures} == {PASS_RATE, CRITICAL_SEVERITY}

    def test_a_clean_run_passes_every_gate(self) -> None:
        verdict = evaluate_gates(report(*floors_met(case("a", "golden"))), SETTINGS)

        assert verdict.exit_code == EXIT_OK
        assert verdict.passed
        assert not verdict.failures
        assert [g.status for g in verdict.gates] == ["PASSED"] * 4


class TestSuiteFloors:
    def test_floor_breach_fails_even_when_the_global_rate_clears(self) -> None:
        run = report(
            *(case(f"g-{i}", "golden") for i in range(18)),
            case("hall-1", "hallucination"),
            case("hall-2", "hallucination", passed=False),
            *(case(f"adv-{i}", "adversarial") for i in range(20)),
        )
        assert run.summary()["pass_rate"] >= SETTINGS["suite_pass_rate"]

        verdict = evaluate_gates(run, SETTINGS)
        floor = gate_for(verdict, SUITE_FLOOR, "hallucination")

        assert verdict.exit_code == EXIT_FAILED
        assert gate_for(verdict, PASS_RATE).passed
        assert floor.status == "FAILED"
        assert "pass rate 50% is below its required 90%" in floor.reason

    def test_a_breached_floor_carries_its_failing_cases(self) -> None:
        """Not rendered in the terminal - the per-case lines already showed them -
        but a UI attaches them to the gate rather than re-deriving them."""
        run = report(
            case("hall-1", "hallucination"),
            case("hall-2", "hallucination", passed=False, critical="expected_status"),
            *(case(f"adv-{i}", "adversarial") for i in range(20)),
        )

        floor = gate_for(evaluate_gates(run, SETTINGS), SUITE_FLOOR, "hallucination")

        assert [o.case_id for o in floor.offenders] == ["hall-2"]
        assert floor.offenders[0].check == "expected_status"

    def test_a_named_suite_that_did_not_run_is_an_error(self) -> None:
        run = report(*(case(f"adv-{i}", "adversarial") for i in range(20)))

        verdict = evaluate_gates(run, SETTINGS)
        floor = gate_for(verdict, SUITE_FLOOR, "hallucination")

        assert verdict.exit_code == EXIT_ERROR
        assert floor.status == "ERROR"
        assert "did not run" in floor.reason

    def test_an_excluded_suite_is_unchecked_not_passed(self) -> None:
        run = report(*(case(f"adv-{i}", "adversarial") for i in range(20)))

        verdict = evaluate_gates(run, SETTINGS, selection_filtered=True)
        floor = gate_for(verdict, SUITE_FLOOR, "hallucination")

        assert verdict.exit_code == EXIT_OK
        assert floor.status == "UNCHECKED"
        assert not floor.passed
        assert floor.unchecked

    def test_an_unparsable_floor_is_an_error(self) -> None:
        settings = {"suite_pass_rate": 0.8, "suite_must_pass": {"golden": "most of them"}}

        verdict = evaluate_gates(report(case("a", "golden")), settings)

        assert verdict.exit_code == EXIT_ERROR
        assert "is not a number" in gate_for(verdict, SUITE_FLOOR, "golden").reason

    def test_a_non_mapping_floor_block_is_an_error(self) -> None:
        settings = {"suite_pass_rate": 0.8, "suite_must_pass": "hallucination"}

        verdict = evaluate_gates(report(case("a", "golden")), settings)

        assert verdict.exit_code == EXIT_ERROR
        assert "must be a mapping" in gate_for(verdict, SUITE_FLOOR).reason

    def test_no_floors_configured_leaves_the_other_gates_alone(self) -> None:
        verdict = evaluate_gates(report(case("a", "golden")), {"suite_pass_rate": 0.8})

        assert verdict.exit_code == EXIT_OK
        assert [g.gate for g in verdict.gates] == [PASS_RATE, CRITICAL_SEVERITY]


class TestRunVerdict:
    def test_an_operator_error_outranks_a_case_failure(self) -> None:
        """Exit 2 means "the gate could not be evaluated", which someone has to
        fix before the exit-1 result means anything."""
        run = report(
            case("a", "golden", passed=False, critical="expected_status"),
            *(case(f"adv-{i}", "adversarial") for i in range(20)),
        )

        assert evaluate_gates(run, SETTINGS).exit_code == EXIT_ERROR

    def test_an_empty_verdict_is_a_pass(self) -> None:
        assert RunVerdict().exit_code == EXIT_OK
