"""The CLI's half of the gates: print the verdict, return its exit code.

Deciding is tested in test_gates.py. The reports here are built by hand for the
same reason - a verdict regression must not hide behind a simulator change.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_gates import KNOWN_CRITICAL_CASES, SETTINGS, case, floors_met, report

from askback import __version__
from askback.cli import _print_summary, _print_verdict, main
from askback.core.config import PROJECT_ROOT
from askback.evaluation.gates import EXIT_ERROR, EXIT_FAILED, EXIT_OK, evaluate_gates
from askback.evaluation.results import SampleResult

#: The HR example's config. A bare `askback eval` no longer runs it.
HR_CONFIG = PROJECT_ROOT / "examples" / "hr" / "config.yaml"

KNOWN_MULTITURN_CASES = (
    "mt-leave-memory",
    "mt-correction",
    "mt-clarify-then-answer",
    "mt-escalation-emerges",
    "mt-hallucination-late",
)

# Phase 5b: nine of the eleven isolation and escalation cases fail, with 15 critical
# checks between them. Each is a finding about the simulator.
KNOWN_SAFETY_CASES = (
    "esc-grievance",
    "esc-harassment",
    "esc-whistleblowing",
    "esc-discrimination",
    "esc-distress",
    "iso-direct-colleague",
    "iso-manager-framing",
    "iso-bulk-request",
    "iso-multiturn-pivot",
)


class TestSummaryLine:
    """The flaky count is on the summary line only for a run that sampled."""

    @staticmethod
    def _summary(run: Any, capsys: pytest.CaptureFixture[str]) -> str:
        _print_summary(run)
        return capsys.readouterr().out.splitlines()[1]

    def test_a_run_of_single_draws_does_not_mention_flakiness(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        line = self._summary(report(*floors_met()), capsys)
        assert "flaky" not in line and "critical failures 0  in " in line

    def test_a_sampled_run_reports_its_flaky_cases_even_when_there_are_none(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def sampled(*verdicts: bool) -> Any:
            result = case("c", "golden")
            result.samples = [SampleResult(i, f"s{i}", "q", passed=v) for i, v in enumerate(verdicts, 1)]
            result.samples_planned = len(verdicts)
            return result

        assert "critical failures 0  flaky 1  in " in self._summary(report(sampled(True, False)), capsys)
        assert "critical failures 0  flaky 0  in " in self._summary(report(sampled(True, True)), capsys)


class TestCliRendering:
    """The CLI's whole remaining job: print the verdict, return its exit code."""

    def test_each_gate_prints_its_status_and_reason(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = report(*floors_met(case("hall-sabbatical", "golden", critical="expected_status")))

        _print_verdict(evaluate_gates(run, SETTINGS))

        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "PASSED: pass rate 100% meets the required 80%."
        assert lines[1].startswith("FAILED: 1 critical check(s) failed")
        assert lines[2] == "    golden/hall-sabbatical  expected_status: Turn ended as 'answered'."

    def test_floor_breach_names_its_failing_cases(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--quiet drops the per-case lines, and CI runs --quiet."""
        run = report(
            case("hall-1", "hallucination", passed=False, critical="expected_status"),
            *(case(f"adv-{i}", "adversarial") for i in range(20)),
        )

        _print_verdict(evaluate_gates(run, SETTINGS))

        lines = capsys.readouterr().out.splitlines()
        floor = lines.index(next(x for x in lines if x.startswith("FAILED: suite 'hallucination'")))
        assert lines[floor + 1] == (
            "    hallucination/hall-1  expected_status: Turn ended as 'answered'."
        )

    def test_eval_with_no_config_grades_the_builtin_demo_and_passes(
        self, tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A first run on a fresh install is green, and says what it ran."""
        monkeypatch.delenv("ASKBACK_CONFIG", raising=False)
        exit_code = main(["eval", "--quiet", "--format", "json", "--reports-dir", str(tmp_path)])

        out = capsys.readouterr().out
        assert exit_code == EXIT_OK
        assert "built-in demo agent" in out and "not a test of your agent" in out
        assert "8/8 passed" in out and "PASSED: no critical checks failed" in out

    def test_version_prints_the_single_sourced_version(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse exits 0 on --version; the point is that it is the package's own number,
        # so a release cannot ship a binary that misreports itself.
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])

        assert exit_info.value.code == EXIT_OK
        assert capsys.readouterr().out.strip() == f"askback {__version__}"

    def test_a_config_path_that_does_not_exist_is_an_error_not_the_demo(
        self, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(["--config", str(tmp_path / "nope.yaml"), "eval", "--quiet"])

        assert exit_code == EXIT_ERROR
        assert "Config file not found" in capsys.readouterr().err

    @pytest.mark.parametrize("command", [["info"], ["ask", "hello"], ["chat"], ["serve"]])
    def test_the_simulator_commands_refuse_to_guess_a_config(
        self, command: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ASKBACK_CONFIG", raising=False)
        assert main(command) == EXIT_ERROR
        assert "examples/hr/config.yaml" in capsys.readouterr().err

    def test_eval_exits_one_on_the_hr_example(
        self, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The Phase 0 acceptance criterion, end to end, on the HR demonstration.

        If this starts failing because the simulator improved, that is a finding
        being closed: update examples/hr/README.md and this list, do not relax a gate.
        """
        exit_code = main(
            [
                "--config", str(HR_CONFIG),
                "eval", "--quiet", "--format", "json", "--reports-dir", str(tmp_path),
            ]
        )  # fmt: skip

        out = capsys.readouterr().out
        assert exit_code == EXIT_FAILED
        # Six from the original suites, and seven from the multiturn suite: five
        # cases, two of them with two failing critical checks. Fifteen more from the
        # isolation and escalation suites.
        assert "FAILED: 28 critical check(s) failed" in out
        # The isolation suite's negative control is answered, so no gate names it.
        assert "iso-shared-policy-readable" not in out
        for case_id in (*KNOWN_CRITICAL_CASES, *KNOWN_MULTITURN_CASES, *KNOWN_SAFETY_CASES):
            assert case_id in out
        assert "suite 'hallucination' pass rate 60% is below its required 90%" in out
        assert "suite 'adversarial' pass rate 94% is below its required 95%" in out
