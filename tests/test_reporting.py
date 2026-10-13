"""Reports as files: what each redaction mode lets out, and what survives a round trip.

Invariant 4. The assertions that matter are absences, so they are made against
the files as written, every format, not against the objects that produced them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_safety_evaluators import EMAIL, SALARY, SSN

from askback.adapters.scripted import ScriptedAdapter
from askback.cli import _print_verdict
from askback.core.contracts import ChatResponse, RetrievedChunk
from askback.core.errors import ConfigError, DatasetError
from askback.datasets.schema import Expectations, TestCase, Turn
from askback.evaluation.base import EvalResult, JudgeUsage
from askback.evaluation.engine import EvaluationEngine
from askback.evaluation.gates import evaluate_gates
from askback.evaluation.pii_eval import ALL_CATEGORIES
from askback.evaluation.regression import load_baseline, save_baseline
from askback.evaluation.results import CaseResult, RunReport
from askback.reporting import redacted, render_markdown, resolve_mode, write_reports
from askback.reporting.redaction import REPLY_LIMIT
from askback.runner import TestRunner
from optional_deps import requires

#: The same fragments the evaluator's own no-leak test searches for.
LEAKED = (EMAIL, "john123", "acme-corp", SSN, "6789", "85,000", "4111")
PASSAGE = "Grade 7 retention bonuses are approved by the remuneration committee."
TAIL = "This closing sentence sits past the cut."


def leaky_run(*, samples: int = 1) -> RunReport:
    """One case whose question, reply and failing check detail all carry PII.

    The `rag` evaluator is here for its detail string: it quotes the reply's
    unsupported sentences, which is a second road into the report for anything
    the reply leaked.
    """
    answer = ChatResponse(
        reply=(
            f"Write to {EMAIL}. Her SSN is {SSN}. Her salary is {SALARY}. "
            f"Card 4111 1111 1111 1111. {'Nothing else is on file. ' * 8}{TAIL}"
        ),
        conversation_id="scripted",
        retrieved=[RetrievedChunk(doc_id="HR-9", chunk_id="HR-9#0", text=PASSAGE, title="Bonuses")],
    )
    case = TestCase(
        id="leak-1",
        suite="golden",
        turns=[Turn(user=f"Who is {EMAIL}?", expect=Expectations())],
        samples=samples,
    )
    # Every category on: the salary pattern is opt-in, and this run leaks a salary on purpose.
    engine = EvaluationEngine.from_config(
        {
            "evaluators": ["rag", "pii_egress"],
            "evaluator_options": {"pii_egress": {"categories": list(ALL_CATEGORIES)}},
        }
    )
    return TestRunner(ScriptedAdapter(replies={}, default=answer), engine, workers=1).run([case])


def written(report: RunReport, directory: Path, mode: str) -> dict[str, str]:
    """Each format's text as it landed on disk at `mode`."""
    paths = write_reports(report, directory, formats=("json", "html"), redaction=mode)
    # A writer that failed is skipped by design, and would be searched as nothing.
    assert set(paths) == {"json", "html"}
    return {fmt: path.read_text(encoding="utf-8") for fmt, path in paths.items()}


class TestRedaction:
    def test_a_ci_report_carries_no_pii_value_and_no_chunk_text(self, tmp_path: Path) -> None:
        report = leaky_run()

        for fmt, text in written(report, tmp_path, "ci").items():
            for value in (*LEAKED, PASSAGE, TAIL):
                assert value not in text, f"{value!r} reached the ci {fmt} report"

        saved = json.loads((tmp_path / f"{report.run_id}.json").read_text(encoding="utf-8"))
        case = saved["cases"][0]
        assert saved["redaction"] == "ci"
        assert "[REDACTED:email]" in case["reply"] and "[REDACTED:email]" in case["message"]
        assert len(case["reply"]) < REPLY_LIMIT + 40
        assert case["response"]["retrieved"] == [
            {"doc_id": "HR-9", "score": 0.0, "title": "Bonuses", "text": "[withheld]"}
        ]
        # The verdict is untouched: the leak is still counted, by category.
        assert saved["summary"] == report.summary()
        (pii,) = [c for r in case["results"] for c in r["checks"] if c["name"] == "pii_egress"]
        assert pii["actual"] == {"credit_card": 1, "email": 1, "salary_figure": 1, "us_ssn": 1}

    def test_a_full_report_carries_both(self, tmp_path: Path) -> None:
        text = written(leaky_run(), tmp_path, "full")

        for value in (EMAIL, SSN, "85,000", PASSAGE, TAIL):
            assert value in text["json"]
        assert EMAIL in text["html"]

    def test_the_workbook_is_redacted_like_the_other_formats(self, tmp_path: Path) -> None:
        openpyxl = requires("openpyxl", extra="excel", test_module="tests/test_reporting.py")

        def cells(mode: str) -> str:
            paths = write_reports(leaky_run(), tmp_path / mode, formats=("xlsx",), redaction=mode)
            book = openpyxl.load_workbook(paths["xlsx"])
            return "\n".join(
                str(c) for sheet in book for row in sheet.iter_rows(values_only=True) for c in row
            )

        assert not [value for value in (*LEAKED, TAIL) if value in cells("ci")]
        assert EMAIL in cells("full")

    def test_metrics_only_keeps_verdicts_and_no_text(self, tmp_path: Path) -> None:
        report = leaky_run()

        for fmt, text in written(report, tmp_path, "metrics_only").items():
            for value in (*LEAKED, PASSAGE, "Nothing else is on file", "Who is", "Unsupported"):
                assert value not in text, f"{value!r} reached the metrics_only {fmt} report"

        saved = json.loads((tmp_path / f"{report.run_id}.json").read_text(encoding="utf-8"))
        assert saved["summary"] == report.summary()
        assert saved["failing_checks"] == report.failing_checks()
        assert saved["cases"][0]["reply"] == "[withheld]"

    def test_redaction_only_strengthens(self) -> None:
        ci = redacted(leaky_run(), "ci")

        again = redacted(ci, "full")
        assert again.redaction == "ci", "a ci report must not come back labelled full"
        assert again.to_dict() == ci.to_dict(), "and a second pass must not cut the reply twice"

    def test_an_unknown_mode_is_refused_and_full_is_refused_under_ci(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CI", raising=False)
        assert resolve_mode({}) == "ci"
        assert resolve_mode({"redaction": "metrics_only"}, None) == "metrics_only"
        assert resolve_mode({"redaction": "ci"}, "full") == "full"
        with pytest.raises(ConfigError, match="redaction"):
            resolve_mode({"redaction": "none"})

        monkeypatch.setenv("CI", "true")
        with pytest.raises(ConfigError, match="local runs only"):
            resolve_mode({}, "full")
        assert resolve_mode({}, "ci") == "ci"


class TestRoundTrip:
    @staticmethod
    def _sampled() -> RunReport:
        """A 2-of-3 case that fails its last draw: sampled, flaky, and passing."""
        asked: list[str] = []

        def respond(message: str, prior: list[str]) -> ChatResponse:
            asked.append(message)
            return ChatResponse(
                reply="No idea." if len(asked) == 3 else "24 days.", conversation_id="scripted"
            )

        case = TestCase(
            id="leave-sampled",
            suite="golden",
            turns=[Turn(user="leave?", expect=Expectations(required_facts=["24 days"]))],
            samples=3,
            min_passing_samples=2,
        )
        engine = EvaluationEngine.from_config({"evaluators": ["rule_based"]})
        return TestRunner(ScriptedAdapter(responder=respond), engine, workers=1).run([case])

    def test_a_sampled_report_round_trips_samples_and_flaky(self, tmp_path: Path) -> None:
        report = self._sampled()
        assert report.summary()["flaky"] == 1, "the fixture must be flaky or this proves nothing"

        read_back = RunReport.from_dict(json.loads(json.dumps(report.to_dict())))

        assert read_back.to_dict() == report.to_dict()
        case = read_back.cases[0]
        assert (case.flaky, case.passing_samples, len(case.samples)) == (True, 2, 3)
        assert [c.name for c in case.failed_checks] == ["required_facts"]

    def test_a_baseline_is_a_summary_that_keeps_verdicts_and_no_text(self, tmp_path: Path) -> None:
        report = leaky_run()
        path = save_baseline(report, tmp_path / "baseline.json")

        text = path.read_text(encoding="utf-8")
        for value in (*LEAKED, PASSAGE, "Nothing else is on file", "Who is", "Unsupported", "reply"):
            assert value not in text, f"{value!r} reached the baseline"
        # One case per line, so a moved verdict is one changed line in review.
        assert sum(1 for line in text.splitlines() if '"case_id"' in line) == len(report.cases)

        baseline = load_baseline(path)
        (case,) = baseline.cases
        assert (case.case_id, case.suite, case.passed, case.score) == ("leak-1", "golden", False, 0.0)
        # The run's own aggregates, stored: recomputed from a summary there would
        # be no checks to count, and this would read zero.
        assert baseline.summary()["critical_failures"] == report.summary()["critical_failures"] > 0
        assert baseline.evaluators == report.by_evaluator()
        assert baseline.context == report.context

    def test_a_sampled_baseline_keeps_the_k_of_n_outcome_and_flaky(self, tmp_path: Path) -> None:
        # The path the round-trip gap was found on. A summary drops the samples
        # themselves; it must not drop the fact that the case was flaky.
        baseline = load_baseline(save_baseline(self._sampled(), tmp_path / "baseline.json"))

        (case,) = baseline.cases
        assert baseline.summary()["flaky"] == 1
        assert (case.passed, case.passing_samples, case.samples_planned, case.flaky) == (True, 2, 3, True)

    def test_a_baseline_is_not_read_as_a_run_report(self, tmp_path: Path) -> None:
        path = save_baseline(self._sampled(), tmp_path / "baseline.json")

        with pytest.raises(DatasetError, match="baseline summary"):
            RunReport.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def test_a_report_missing_a_verdict_is_refused_not_read_as_a_failure(self) -> None:
        data = self._sampled().to_dict()
        del data["cases"][0]["results"][0]["checks"][0]["severity"]

        with pytest.raises(DatasetError, match="severity"):
            RunReport.from_dict(data)


class TestMarkdownSummary:
    def test_it_gives_the_numbers_and_names_the_critical_checks_but_quotes_nothing(self) -> None:
        # Rendered unredacted on purpose: the summary must be safe by what it
        # leaves out, not by what a redaction pass happened to catch.
        text = render_markdown(leaky_run())

        assert "| Pass rate | **0%** (0 of 1) |" in text
        assert "| Critical failures | **2** |" in text
        assert "- `golden/leak-1`: groundedness, pii_egress" in text
        assert not [value for value in (*LEAKED, PASSAGE, "Who is") if value in text]
        # A free judge has no cost. "$0.00" would say a metered one had been priced.
        assert "none: no metered judge" in text and "$" not in text

    def test_a_metered_judge_shows_its_cost_and_an_incomplete_run_says_so(self) -> None:
        usage = JudgeUsage(calls=3, cache_hits=1, cost_usd=0.0123)
        report = RunReport(run_id="nightly", context={"judge": "azure_openai:gpt-4o-mini"})
        report.cases = [
            CaseResult(
                case_id="a",
                suite="golden",
                message="m",
                score=1.0,
                passed=True,
                results=[EvalResult(evaluator="llm_judge", score=1.0, judge=usage)],
            )
        ]
        assert "| Estimated judge cost | $0.0123 (3 calls, 1 cached) |" in render_markdown(report)

        report.incomplete = {"reason": "judge_budget_exceeded", "cases_run": 1, "cases_planned": 9}
        assert "**Incomplete: no verdict.** 1 of 9 cases ran" in render_markdown(report)


def test_the_gates_decide_the_same_at_every_mode_and_print_no_leaked_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`eval` gates and prints the redacted view, because its output is the CI log."""
    report = leaky_run()
    settings = {"suite_pass_rate": 0.8}
    as_run = evaluate_gates(report, settings)

    for mode in ("full", "ci", "metrics_only"):
        verdict = evaluate_gates(redacted(report, mode), settings)
        assert [(g.gate, g.status, len(g.offenders)) for g in verdict.gates] == [
            (g.gate, g.status, len(g.offenders)) for g in as_run.gates
        ], mode

    _print_verdict(evaluate_gates(redacted(report, "ci"), settings))
    out = capsys.readouterr().out
    assert "golden/leak-1  groundedness" in out
    assert not [value for value in (*LEAKED, TAIL) if value in out]
