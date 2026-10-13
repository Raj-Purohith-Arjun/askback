"""Evaluation modules and the regression gate."""

from __future__ import annotations

from typing import Any

import pytest

from askback.core.contracts import ChatResponse, Citation, ResponseStatus, RetrievedChunk
from askback.core.errors import ConfigError
from askback.datasets.schema import Expectations, TestCase, Turn
from askback.evaluation.engine import EvaluationEngine
from askback.evaluation.llm_judge import (
    HeuristicJudge,
    LLMJudgeEvaluator,
    ModelJudge,
    build_judge,
)
from askback.evaluation.rag_eval import RagEvaluator, groundedness
from askback.evaluation.regression import RegressionComparator
from askback.evaluation.results import CaseResult, RunReport
from askback.evaluation.rule_based import RuleBasedEvaluator, fact_present
from askback.evaluation.safety_eval import SafetyEvaluator
from askback.providers.base import LLM_PROVIDERS, LLMProvider, LLMResponse


def make_response(**overrides) -> ChatResponse:
    defaults = dict(
        reply="Full-time employees receive 24 days of paid annual leave. [POL-1]",
        conversation_id="c1",
        route="policy_agent",
        status=ResponseStatus.ANSWERED,
        retrieved=[
            RetrievedChunk(
                doc_id="POL-1",
                chunk_id="POL-1#0",
                text="Full-time employees receive 24 days of paid annual leave.",
            )
        ],
        citations=[Citation(doc_id="POL-1", chunk_id="POL-1#0", snippet="Full-time employees")],
    )
    defaults.update(overrides)
    return ChatResponse(**defaults)


def one_turn(tags: list[str] | None = None, **expect: Any) -> TestCase:
    """A single-turn case whose one turn carries `expect`."""
    return TestCase(id="t1", turns=[Turn(user="q", expect=Expectations(**expect))], tags=tags or [])


def grade(grader: Any, case: TestCase, response: ChatResponse) -> Any:
    """Grade the reply to the case's only turn, as the runner does."""
    return grader.evaluate(case, case.turns[0], response)


class TestFactMatching:
    def test_plain_substring_ignores_case_and_spacing(self) -> None:
        assert fact_present("24 DAYS", "receive 24  days of leave")

    def test_regex_facts_use_slashes(self) -> None:
        assert fact_present(r"/TKT-[A-Z0-9]{4}/", "Raised ticket TKT-9F2A.")
        assert not fact_present(r"/TKT-\d{8}/", "Raised ticket TKT-9F2A.")


class TestRuleBased:
    def test_all_expectations_met(self) -> None:
        case = one_turn(
            expected_route="policy_agent",
            expected_status="answered",
            required_facts=["24 days"],
            min_citations=1,
        )
        result = grade(RuleBasedEvaluator(), case, make_response())
        assert result.passed
        assert result.score == 1.0

    def test_wrong_route_is_a_critical_failure(self) -> None:
        case = one_turn(expected_route="learning_agent")
        result = grade(RuleBasedEvaluator(), case, make_response())
        assert not result.passed
        assert [c.name for c in result.critical_failures] == ["expected_route"]

    @pytest.mark.parametrize("expected", ["answered", "no_answer", "unknown"])
    def test_an_unknown_status_fails_critically_whatever_was_expected(
        self, expected: str
    ) -> None:
        case = one_turn(expected_status=expected)
        response = make_response(status=ResponseStatus.UNKNOWN)
        result = grade(RuleBasedEvaluator(), case, response)

        assert [c.name for c in result.critical_failures] == ["expected_status"]
        detail = result.critical_failures[0].detail
        assert "reported no status" in detail and "could not be evaluated" in detail

    def test_an_unknown_status_is_not_checked_when_none_was_expected(self) -> None:
        response = make_response(status=ResponseStatus.UNKNOWN)
        result = grade(RuleBasedEvaluator(), one_turn(), response)
        assert not [c for c in result.checks if c.name == "expected_status"]

    def test_missing_facts_score_partial_credit(self) -> None:
        case = one_turn(required_facts=["24 days", "not present here"])
        result = grade(RuleBasedEvaluator(), case, make_response())
        check = next(c for c in result.checks if c.name == "required_facts")
        assert check.score == pytest.approx(0.5)

    def test_prohibited_content_is_detected(self) -> None:
        case = one_turn(prohibited_content=["24 days"])
        result = grade(RuleBasedEvaluator(), case, make_response())
        assert not result.passed

    def test_invented_citation_fails_the_schema_check(self) -> None:
        response = make_response(citations=[Citation(doc_id="GHOST")])
        result = grade(RuleBasedEvaluator(), one_turn(), response)
        check = next(c for c in result.checks if c.name == "schema_valid")
        assert not check.passed


class TestRag:
    def test_recall_and_rank(self) -> None:
        case = one_turn(expected_docs=["POL-1"])
        result = grade(RagEvaluator(), case, make_response())
        assert result.passed
        assert next(c for c in result.checks if c.name == "retrieval_mrr").score == 1.0

    def test_missing_document_fails_recall(self) -> None:
        case = one_turn(expected_docs=["POL-9"])
        result = grade(RagEvaluator(), case, make_response())
        assert not result.passed

    def test_ungrounded_answer_is_caught(self) -> None:
        response = make_response(reply="Employees get unlimited holiday on a yacht in Monaco.")
        result = grade(RagEvaluator(), one_turn(), response)
        assert not next(c for c in result.checks if c.name == "groundedness").passed

    def test_refusals_are_not_graded_for_groundedness(self) -> None:
        # A fixed refusal template is not a claim about the corpus.
        response = make_response(
            reply="I do not have that information in my knowledge base.",
            status=ResponseStatus.NO_ANSWER,
            citations=[],
        )
        result = grade(RagEvaluator(), one_turn(), response)
        assert not [c for c in result.checks if c.name == "groundedness"]

    def test_groundedness_helper_reports_the_offending_sentence(self) -> None:
        chunks = [RetrievedChunk(doc_id="A", chunk_id="A#0", text="Leave is 24 days per year.")]
        score, unsupported = groundedness("Leave is 24 days per year. Bonuses are paid monthly.", chunks, 0.55)
        assert score == pytest.approx(0.5)
        assert "Bonuses" in unsupported[0]


class TestSafetyEvaluator:
    def test_expected_refusal_is_rewarded(self) -> None:
        case = one_turn(must_refuse=True, tags=["injection"])
        response = make_response(status=ResponseStatus.REFUSED, reply="I cannot help with that.")
        response.safety.injection_detected = True
        assert grade(SafetyEvaluator(), case, response).passed

    def test_missing_refusal_is_critical(self) -> None:
        case = one_turn(must_refuse=True)
        result = grade(SafetyEvaluator(), case, make_response())
        assert not result.passed
        assert result.critical_failures

    def test_over_refusal_is_also_a_failure(self) -> None:
        case = one_turn(must_refuse=False)
        response = make_response(status=ResponseStatus.REFUSED)
        result = grade(SafetyEvaluator(), case, response)
        assert [c.name for c in result.critical_failures] == ["no_over_refusal"]

    def test_leaked_secret_is_detected(self) -> None:
        response = make_response(reply="The token is sk-live-EXAMPLE-NOT-A-REAL-KEY")
        result = grade(SafetyEvaluator(), one_turn(), response)
        assert not next(c for c in result.checks if c.name == "no_secret_leakage").passed

    def test_cross_tenant_retrieval_is_detected(self) -> None:
        response = make_response(
            retrieved=[
                RetrievedChunk(doc_id="X", chunk_id="X#0", text="t", metadata={"tenant": "acme"})
            ]
        )
        result = grade(SafetyEvaluator(), one_turn(), response)
        assert not next(c for c in result.checks if c.name == "tenant_isolation").passed

    def test_no_tool_declarations_is_a_critical_failure_not_a_pass(self, monkeypatch) -> None:
        # Invariant 8: with nothing declared the check has measured nothing.
        monkeypatch.setattr("askback.evaluation.safety_eval._SIDE_EFFECT_SOURCES", [])
        result = grade(SafetyEvaluator(), one_turn(), make_response())
        check = next(c for c in result.checks if c.name == "tool_side_effects")
        assert not check.passed and check in result.critical_failures
        assert "declar" in check.detail

    def test_declared_but_empty_tool_set_can_pass(self, monkeypatch) -> None:
        monkeypatch.setattr("askback.evaluation.safety_eval._SIDE_EFFECT_SOURCES", [lambda: []])
        result = grade(SafetyEvaluator(), one_turn(), make_response())
        assert next(c for c in result.checks if c.name == "tool_side_effects").passed


class TestLLMJudge:
    def test_skipped_without_a_reference_answer(self) -> None:
        result = grade(LLMJudgeEvaluator(), one_turn(), make_response())
        assert result.skipped

    def test_scores_every_dimension(self) -> None:
        case = one_turn(
            reference_answer="Full-time employees receive 24 days of paid annual leave."
        )
        result = grade(LLMJudgeEvaluator(), case, make_response())
        assert {c.name for c in result.checks} == {
            "correctness", "completeness", "groundedness", "reasoning", "clarity"
        }
        assert result.score > 0.7


class _RemoteStub(LLMProvider):
    """A provider that claims to be a remote model, with switchable credentials."""

    name = "judge-stub"
    remote = True
    available = True

    def __init__(self, model: str = "", env_prefix: str = "") -> None:
        self.model = model
        self.env_prefix = env_prefix

    def generate(self, messages: Any, *, context: Any = (), **options: Any) -> LLMResponse:
        return LLMResponse(text="{}")

    def is_available(self) -> bool:
        return self.available


@pytest.fixture
def judge_stub(monkeypatch: pytest.MonkeyPatch) -> type[_RemoteStub]:
    monkeypatch.setitem(LLM_PROVIDERS._items, "judge-stub", _RemoteStub)
    monkeypatch.setattr(_RemoteStub, "available", True)
    return _RemoteStub


class TestJudgeSelection:
    """Which judge grades is decided by `evaluation.judge` alone, and recorded."""

    @pytest.mark.parametrize("evaluation", [{}, {"judge": None}, {"judge": {}}])
    def test_unconfigured_is_the_heuristic_judge_and_says_so(
        self, evaluation: dict[str, Any]
    ) -> None:
        engine = EvaluationEngine.from_config(evaluation)

        judge = next(e for e in engine.evaluators if isinstance(e, LLMJudgeEvaluator)).judge
        assert isinstance(judge, HeuristicJudge)
        assert engine.describe()["judge"] == "heuristic"

    def test_the_engine_takes_no_llm_from_its_caller(self) -> None:
        with pytest.raises(TypeError):
            EvaluationEngine.from_config({}, llm=object())  # type: ignore[call-arg]

    def test_a_configured_model_judge_is_built_and_named(
        self, judge_stub: type[_RemoteStub]
    ) -> None:
        engine = EvaluationEngine.from_config(
            {"judge": {"llm": {"provider": "judge-stub", "model": "m-1"}}}
        )

        judge = next(e for e in engine.evaluators if isinstance(e, LLMJudgeEvaluator)).judge
        assert isinstance(judge, ModelJudge) and isinstance(judge.llm, judge_stub)
        assert judge.llm.env_prefix == "JUDGE_"
        assert engine.describe()["judge"] == "model:judge-stub/m-1"

    def test_a_configured_judge_without_credentials_stops_the_run(
        self, judge_stub: type[_RemoteStub], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Never a quiet switch to the heuristic: that changes the scoring regime.
        monkeypatch.setattr(judge_stub, "available", False)
        with pytest.raises(ConfigError, match="not available"):
            EvaluationEngine.from_config({"judge": {"llm": {"provider": "judge-stub"}}})

    @pytest.mark.parametrize(
        ("judge", "match"),
        [
            ("azure_openai", "must be a mapping"),
            ({"model": "gpt"}, "unknown evaluation.judge key"),
            ({"llm": "azure_openai"}, "judge.llm must be a mapping"),
            ({"llm": {"model": "gpt"}}, "provider is required"),
            ({"llm": {"provider": "no-such-model"}}, "no-such-model"),
            # Grading the offline provider with itself would be circular.
            ({"llm": {"provider": "extractive"}}, "not a model that can judge"),
            ({"llm": {"provider": "judge-stub", "nonsense": 1}}, "does not fit"),
            # The prefix is what keeps the judge off the agent's credentials.
            ({"llm": {"provider": "judge-stub", "env_prefix": ""}}, "env_prefix cannot be set"),
        ],
    )
    def test_a_bad_judge_block_is_refused_by_name(
        self, judge_stub: type[_RemoteStub], judge: Any, match: str
    ) -> None:
        with pytest.raises(ConfigError, match=match):
            EvaluationEngine.from_config({"judge": judge})

    def test_the_judge_in_use_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("INFO", logger="askback.evaluation.engine"):
            EvaluationEngine.from_config({})

        assert [r.getMessage() for r in caplog.records] == ["Judge in use: heuristic"]

    def test_a_changed_judge_is_flagged_by_the_regression_comparison(self) -> None:
        baseline = RunReport(run_id="base", context={"judge": "heuristic"})
        candidate = RunReport(run_id="cand", context={"judge": "model:judge-stub/m-1"})

        result = RegressionComparator().compare(baseline, candidate)
        assert result.context_warnings == [
            "judge changed: 'heuristic' -> 'model:judge-stub/m-1'"
        ]


_AGENT_AZURE = {
    "AZURE_OPENAI_ENDPOINT": "https://agent.example",
    "AZURE_OPENAI_DEPLOYMENT": "agent-deployment",
    "AZURE_OPENAI_API_KEY": "agent-key",
}


class TestJudgeCredentials:
    """The judge never grades on the agent's own model resource."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", *_AGENT_AZURE):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(f"JUDGE_{name}", raising=False)

    def test_the_agents_azure_credentials_are_not_the_judges(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in _AGENT_AZURE.items():
            monkeypatch.setenv(name, value)

        with pytest.raises(ConfigError) as refused:
            build_judge({"llm": {"provider": "azure_openai"}})

        message = str(refused.value)
        for setting, variable in (
            ("endpoint", "JUDGE_AZURE_OPENAI_ENDPOINT"),
            ("deployment", "JUDGE_AZURE_OPENAI_DEPLOYMENT"),
            ("api_key", "JUDGE_AZURE_OPENAI_API_KEY"),
        ):
            assert f"evaluation.judge.llm.{setting} or {variable}" in message
        assert "agent-key" not in message and "agent.example" not in message

    def test_only_what_is_missing_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JUDGE_AZURE_OPENAI_API_KEY", "judge-key")

        with pytest.raises(ConfigError) as refused:
            build_judge({"llm": {"provider": "azure_openai", "endpoint": "https://judge.example"}})

        message = str(refused.value)
        assert "JUDGE_AZURE_OPENAI_DEPLOYMENT" in message
        assert "JUDGE_AZURE_OPENAI_API_KEY" not in message and "endpoint (" not in message

    def test_judge_prefixed_variables_and_explicit_config_are_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in _AGENT_AZURE.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("JUDGE_AZURE_OPENAI_ENDPOINT", "https://judge.example/")
        monkeypatch.setenv("JUDGE_AZURE_OPENAI_API_KEY", "judge-key")

        judge = build_judge({"llm": {"provider": "azure_openai", "deployment": "judge-gpt"}})

        assert isinstance(judge, ModelJudge)
        llm: Any = judge.llm
        assert (llm.endpoint, llm.deployment, llm.api_key) == (
            "https://judge.example", "judge-gpt", "judge-key",
        )  # fmt: skip
        assert judge.identity == "model:azure_openai/judge-gpt"

    @pytest.mark.parametrize(
        ("provider", "agent_variable"),
        [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")],
    )
    def test_the_same_holds_for_the_other_remote_providers(
        self, monkeypatch: pytest.MonkeyPatch, provider: str, agent_variable: str
    ) -> None:
        # Anthropic has no default model, so the test names one; it is not what is under test.
        settings = {"provider": provider, **({"model": "a-model"} if provider == "anthropic" else {})}
        monkeypatch.setenv(agent_variable, "agent-key")
        with pytest.raises(ConfigError, match=f"JUDGE_{agent_variable}"):
            build_judge({"llm": settings})

        monkeypatch.setenv(f"JUDGE_{agent_variable}", "judge-key")
        judge = build_judge({"llm": settings})
        assert isinstance(judge, ModelJudge)
        llm: Any = judge.llm
        assert llm.api_key == "judge-key"

    def test_an_agent_built_from_the_same_class_still_reads_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prefix separates the two users; it must not break the agent's."""
        from askback_sim.llm import build_provider

        for name, value in _AGENT_AZURE.items():
            monkeypatch.setenv(name, value)

        provider: Any = build_provider({"provider": "azure_openai"})
        assert provider.name == "azure_openai" and provider.api_key == "agent-key"


class TestEvaluationEngine:
    def test_critical_failure_vetoes_a_high_score(self) -> None:
        engine = EvaluationEngine([RuleBasedEvaluator(), SafetyEvaluator()], pass_threshold=0.5)
        case = one_turn(expected_route="learning_agent")
        _, score, passed = grade(engine, case, make_response())
        assert score > 0.5, "the numeric score is still respectable"
        assert not passed, "but a critical failure must veto the pass"

    def test_skipped_evaluators_do_not_dilute_the_score(self) -> None:
        engine = EvaluationEngine([RuleBasedEvaluator(), LLMJudgeEvaluator()])
        results, score, passed = grade(engine, one_turn(), make_response())
        assert any(r.skipped for r in results)
        assert score == 1.0 and passed

    def test_a_turn_every_evaluator_skipped_is_unmeasured_and_does_not_pass(self) -> None:
        engine = EvaluationEngine([LLMJudgeEvaluator()])
        results, score, passed = grade(engine, one_turn(), make_response())
        assert all(r.skipped for r in results)
        assert score == 0.0 and not passed


class TestRegression:
    @staticmethod
    def _report(run_id: str, cases: list[tuple[str, float, bool]]) -> RunReport:
        report = RunReport(run_id=run_id)
        report.cases = [
            CaseResult(case_id=cid, suite="s", message="m", score=score, passed=ok)
            for cid, score, ok in cases
        ]
        return report

    def test_newly_failing_case_blocks_the_build(self) -> None:
        baseline = self._report("base", [("a", 1.0, True), ("b", 1.0, True)])
        candidate = self._report("cand", [("a", 1.0, True), ("b", 0.2, False)])
        result = RegressionComparator().compare(baseline, candidate)
        assert not result.passed
        assert [d.case_id for d in result.regressed] == ["b"]

    def test_improvements_do_not_block(self) -> None:
        baseline = self._report("base", [("a", 0.4, False)])
        candidate = self._report("cand", [("a", 0.9, True)])
        result = RegressionComparator().compare(baseline, candidate)
        assert result.passed
        assert [d.case_id for d in result.improved] == ["a"]

    def test_mean_drop_beyond_tolerance_blocks(self) -> None:
        baseline = self._report("base", [("a", 1.0, True)])
        candidate = self._report("cand", [("a", 0.8, True)])
        result = RegressionComparator(tolerance=0.05).compare(baseline, candidate)
        assert not result.passed

    def test_new_and_missing_cases_are_reported(self) -> None:
        baseline = self._report("base", [("a", 1.0, True)])
        candidate = self._report("cand", [("b", 1.0, True)])
        result = RegressionComparator().compare(baseline, candidate)
        assert result.new_cases == ["b"]
        assert result.missing_cases == ["a"]

    def test_flipped_verdicts_are_split_from_score_moves_and_counted_per_suite(self) -> None:
        def run(run_id: str, rows: list[tuple[str, str, float, bool]]) -> RunReport:
            report = RunReport(run_id=run_id)
            report.cases = [
                CaseResult(case_id=cid, suite=suite, message="m", score=score, passed=ok)
                for cid, suite, score, ok in rows
            ]
            return report

        baseline = run(
            "base",
            [("a", "s1", 1.0, True), ("b", "s1", 0.4, False), ("c", "s1", 1.0, True), ("d", "s2", 1.0, True)],
        )
        candidate = run(
            "cand",
            [("a", "s1", 0.2, False), ("b", "s1", 0.9, True), ("c", "s1", 0.9, True), ("e", "s3", 1.0, True)],
        )
        result = RegressionComparator(tolerance=0.05).compare(baseline, candidate)

        assert [d.case_id for d in result.newly_failing] == ["a"]
        assert [d.case_id for d in result.newly_passing] == ["b"]
        # `c` still passes but dropped past tolerance: regressed, and not a flip.
        assert [d.case_id for d in result.regressed] == ["a", "c"]

        s1, s2, s3 = result.suites
        assert (s1.suite, s1.newly_failing, s1.newly_passing) == ("s1", 1, 1)
        # One in, one out: the pass rate hides a move that the split and the mean show.
        assert (s1.pass_rate_delta, s1.mean_delta, s1.moved) == (0.0, -0.1333, True)
        # A suite only one run had has no delta. None, never a move to or from zero.
        assert (s2.candidate, s2.pass_rate_delta, s2.moved) == (None, None, True)
        assert (s3.baseline, s3.mean_delta) == (None, None)

    def test_configuration_changes_are_flagged(self) -> None:
        baseline = self._report("base", [("a", 1.0, True)])
        baseline.context = {"llm_provider": "extractive"}
        candidate = self._report("cand", [("a", 1.0, True)])
        candidate.context = {"llm_provider": "azure_openai"}
        result = RegressionComparator().compare(baseline, candidate)
        assert result.context_warnings

    def test_any_context_key_both_runs_recorded_is_compared(self) -> None:
        baseline = self._report("base", [("a", 1.0, True)])
        baseline.context = {"adapter": "in_process", "llm_provider": "extractive"}
        candidate = self._report("cand", [("a", 1.0, True)])
        candidate.context = {"adapter": "http", "llm_provider": "extractive"}
        result = RegressionComparator().compare(baseline, candidate)
        assert result.context_warnings == ["adapter changed: 'in_process' -> 'http'"]

    def test_a_context_key_only_one_run_recorded_is_not_a_difference(self) -> None:
        baseline = self._report("base", [("a", 1.0, True)])
        baseline.context = {"llm_provider": "extractive"}
        candidate = self._report("cand", [("a", 1.0, True)])
        candidate.context = {"llm_provider": "extractive", "adapter": "in_process"}
        result = RegressionComparator().compare(baseline, candidate)
        assert result.context_warnings == []
