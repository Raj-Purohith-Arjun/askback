"""The runner, driven through the adapter seam with a scripted system under test.

No simulator here on purpose: the runner is meant to know an `AgentAdapter` and
nothing more specific, and a suite that needed the engine to test it would be
evidence that it still did.
"""

from __future__ import annotations

import pytest

from askback.adapters.scripted import ScriptedAdapter
from askback.core.contracts import ChatResponse
from askback.datasets.schema import Expectations, TestCase, Turn
from askback.evaluation.engine import EvaluationEngine
from askback.evaluation.rule_based import RuleBasedEvaluator
from askback.reporting.html_report import render_html
from askback.runner import TestRunner


def _answer(text: str) -> ChatResponse:
    return ChatResponse(reply=text, conversation_id="scripted")


@pytest.fixture
def grader() -> EvaluationEngine:
    return EvaluationEngine([RuleBasedEvaluator()])


def _case(case_id: str, message: str, fact: str, **identity: str) -> TestCase:
    turn = Turn(user=message, expect=Expectations(required_facts=[fact]))
    return TestCase(id=case_id, turns=[turn], **identity)


CASES = [
    _case("leave-1", "leave?", "24 days", suite="golden"),
    _case("pay-1", "pay?", "refused", suite="adversarial", user_id="outsider"),
]


class TestSending:
    def test_each_case_is_sent_as_its_own_session_and_principal(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = ScriptedAdapter(replies={}, default=_answer("ok"))
        TestRunner(adapter, grader, workers=1).run(CASES)

        assert adapter.calls == [
            ("leave?", "golden:leave-1", "test_user"),
            ("pay?", "adversarial:pay-1", "outsider"),
        ]

    def test_the_reply_is_graded_and_recorded_in_dataset_order(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = ScriptedAdapter(
            replies={"leave?": _answer("You get 24 days."), "pay?": _answer("Here it is.")}
        )
        report = TestRunner(adapter, grader, workers=4).run(CASES, run_id="r1")

        assert [(c.case_id, c.passed) for c in report.cases] == [
            ("leave-1", True),
            ("pay-1", False),
        ]
        assert report.cases[0].reply == "You get 24 days."

    def test_a_turn_the_adapter_cannot_deliver_is_a_failed_case(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = ScriptedAdapter(
            replies={"leave?": _answer("You get 24 days.")},
            raises={"pay?": TimeoutError("upstream timed out")},
        )
        report = TestRunner(adapter, grader, workers=1).run(CASES)

        failed = report.cases[1]
        assert not failed.passed
        assert failed.error == "TimeoutError: upstream timed out"
        # One undeliverable turn must not take the rest of the run with it.
        assert report.cases[0].passed


def _expects(message: str, fact: str) -> Turn:
    return Turn(user=message, expect=Expectations(required_facts=[fact]))


class TestConversations:
    CASE = TestCase(
        id="leave-recall",
        turns=[
            _expects("I work full time. How much leave do I get?", "24 days"),
            _expects("And sick leave?", "10 days"),
            _expects("Remind me of the first number.", "24 days"),
        ],
    )

    @staticmethod
    def _agent(*, remembers: bool) -> ScriptedAdapter:
        def respond(message: str, prior: list[str]) -> ChatResponse:
            if "sick" in message:
                return _answer("10 days of sick leave.")
            if "full time" in message or (remembers and any("full time" in m for m in prior)):
                return _answer("24 days of annual leave.")
            return _answer("Which number do you mean?")

        return ScriptedAdapter(responder=respond)

    @pytest.mark.parametrize("remembers", [True, False])
    def test_a_turn_relying_on_an_earlier_one_passes_only_if_the_agent_kept_it(
        self, grader: EvaluationEngine, remembers: bool
    ) -> None:
        adapter = self._agent(remembers=remembers)
        result = TestRunner(adapter, grader, workers=1).run([self.CASE]).cases[0]

        assert {session for _, session, _ in adapter.calls} == {"golden:leave-recall"}
        assert [t.passed for t in result.turns] == [True, True, remembers]
        assert result.passed is remembers
        assert result.score == pytest.approx(sum(t.score or 0.0 for t in result.turns) / 3, abs=1e-4)
        # The case-level fields are the last graded turn's, and the report has them all.
        assert result.reply == result.turns[2].reply
        assert [t["user"] for t in result.to_dict()["turns"]] == [t.user for t in self.CASE.turns]

    def test_a_turn_that_errors_ends_the_conversation_and_fails_the_case(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = ScriptedAdapter(
            replies={}, default=_answer("24 days."), raises={"And sick leave?": TimeoutError("slow")}
        )
        result = TestRunner(adapter, grader, workers=1).run([self.CASE]).cases[0]

        assert not result.passed
        assert result.score == 0.0, "turn 1 passing must not score a conversation that broke"
        assert result.error == "turn 2 of 3: TimeoutError: slow"
        assert [(t.index, t.passed) for t in result.turns] == [(1, True), (2, None)]
        assert len(adapter.calls) == 2, "turn 3 must not be sent into a broken conversation"

    def test_a_setup_turn_is_sent_but_never_graded(self, grader: EvaluationEngine) -> None:
        adapter = ScriptedAdapter(
            replies={"How much leave?": _answer("24 days.")}, default=_answer("no idea")
        )
        case = TestCase(
            id="setup",
            turns=[
                Turn(user="I work full time.", evaluate=False),
                _expects("How much leave?", "24 days"),
                Turn(user="Thanks.", evaluate=False),
            ],
        )
        result = TestRunner(adapter, grader, workers=1).run([case]).cases[0]

        assert len(adapter.calls) == 3
        assert result.passed and result.score == result.turns[1].score
        assert [(t.evaluated, t.passed, t.results) for t in result.turns[::2]] == [(False, None, [])] * 2
        assert result.reply == "24 days.", "a trailing setup turn must not become the case's reply"

    def test_html_lays_out_turns_only_for_a_real_conversation(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = self._agent(remembers=True)
        single = TestCase(id="one", turns=[self.CASE.turns[1]])
        runner = TestRunner(adapter, grader, workers=1)

        assert "<b>Turn 3</b>" in render_html(runner.run([self.CASE]))
        assert "<b>Turn" not in render_html(runner.run([single]))


class TestSampling:
    """One case run N times: a K-of-N verdict, early stop, and no memory between runs."""

    @staticmethod
    def _case(samples: int, min_passing: int | None = None) -> TestCase:
        return TestCase(
            id="leave-sampled",
            turns=[_expects("leave?", "24 days")],
            samples=samples,
            min_passing_samples=min_passing,
        )

    @staticmethod
    def _failing_on(*draws: int) -> ScriptedAdapter:
        """Answers wrongly on the given 1-based draws of the one question, rightly otherwise."""
        asked: list[str] = []

        def respond(message: str, prior: list[str]) -> ChatResponse:
            asked.append(message)
            return _answer("No idea." if len(asked) in draws else "24 days.")

        return ScriptedAdapter(responder=respond)

    @pytest.mark.parametrize(("min_passing", "passes"), [(None, False), (3, False), (2, True)])
    def test_one_failure_in_three_is_flaky_and_decides_the_gate(
        self, grader: EvaluationEngine, min_passing: int | None, passes: bool
    ) -> None:
        # Fails the last draw, so a 3-of-3 gate is still open when it is lost.
        adapter = self._failing_on(3)
        result = TestRunner(adapter, grader, workers=1).run([self._case(3, min_passing)]).cases[0]

        assert (result.passed, result.flaky) == (passes, True)
        assert (result.passing_samples, len(result.samples)) == (2, 3)
        assert result.failed_checks, "a failure in a sample the gate forgave must stay visible"

    def test_the_gate_stops_when_the_passes_still_needed_outnumber_the_samples_left(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = self._failing_on(1, 2, 3)
        result = TestRunner(adapter, grader, workers=1).run([self._case(3, 2)]).cases[0]

        assert (result.passed, result.flaky) == (False, False)
        assert len(adapter.calls) == 2, "two losses make 2-of-3 unreachable; draw 3 is wasted"
        assert [s.index for s in result.samples] == [1, 2]
        assert result.samples_planned == 3

    def test_a_passing_case_still_runs_every_sample(self, grader: EvaluationEngine) -> None:
        """Stopping at K passes would hide the failures that make a case flaky."""
        adapter = self._failing_on()
        result = TestRunner(adapter, grader, workers=1).run([self._case(3, 1)]).cases[0]

        assert (result.passed, result.flaky, len(adapter.calls)) == (True, False, 3)

    def test_every_sample_is_a_fresh_session_that_remembers_nothing(
        self, grader: EvaluationEngine
    ) -> None:
        seen: list[list[str]] = []

        def respond(message: str, prior: list[str]) -> ChatResponse:
            seen.append(prior)
            return _answer("24 days.")

        case = TestCase(
            id="two-turns",
            turns=[_expects("leave?", "24 days"), _expects("and again?", "24 days")],
            samples=3,
        )
        adapter = ScriptedAdapter(responder=respond)
        result = TestRunner(adapter, grader, workers=1).run([case]).cases[0]

        sessions = [session for _, session, _ in adapter.calls]
        assert sessions == [s for s in sorted(set(sessions)) for _ in range(2)]
        assert len(set(sessions)) == 3 == len({s.session_id for s in result.samples})
        assert seen == [[], ["leave?"]] * 3, "a repeat must not be answered with the last run's history"

    def test_a_case_run_once_keeps_its_session_and_serialises_as_before(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = ScriptedAdapter(replies={}, default=_answer("24 days."))
        report = TestRunner(adapter, grader, workers=1).run([self._case(1)])

        assert adapter.calls == [("leave?", "golden:leave-sampled", "test_user")]
        assert not {"samples", "flaky"} & set(report.cases[0].to_dict())
        assert "flaky" not in report.summary()
        html = render_html(report)
        assert "Sample 1" not in html and "Flaky" not in html

    def test_flakiness_is_counted_and_shown_in_the_report(self, grader: EvaluationEngine) -> None:
        adapter = self._failing_on(3)
        report = TestRunner(adapter, grader, workers=1).run([self._case(3, 2)])

        assert report.summary()["flaky"] == 1
        assert report.cases[0].to_dict()["passing_samples"] == 2
        html = render_html(report)
        assert "FLAKY 2/3" in html and "Sample 3 of 3" in html and ">Flaky<" in html


class TestAdapterLifecycle:
    def test_run_opens_the_adapter_and_closes_it_again(self, grader: EvaluationEngine) -> None:
        adapter = ScriptedAdapter(replies={}, default=_answer("ok"))
        TestRunner(adapter, grader, workers=1).run(CASES)

        assert len(adapter.calls) == len(CASES)
        with pytest.raises(RuntimeError, match="send"):
            adapter.send("after the run", session_id="s")

    def test_an_adapter_the_caller_holds_open_stays_open(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = ScriptedAdapter(replies={}, default=_answer("ok"))
        with adapter:
            TestRunner(adapter, grader, workers=1).run(CASES)
            # The run's own exit must not close it under the outer block.
            adapter.send("after the run", session_id="s")

        assert len(adapter.calls) == len(CASES) + 1


class TestContext:
    def test_context_is_the_adapter_merged_with_the_evaluation_engine(
        self, grader: EvaluationEngine
    ) -> None:
        adapter = ScriptedAdapter(replies={}, default=_answer("ok"))
        report = TestRunner(adapter, grader, workers=1).run(CASES)

        assert report.context == {"adapter": "scripted", **grader.describe()}

    def test_an_adapter_cannot_overwrite_how_the_run_was_graded(
        self, grader: EvaluationEngine
    ) -> None:
        class Overclaiming(ScriptedAdapter):
            def describe(self) -> dict[str, str]:
                return {"adapter": "overclaiming", "pass_threshold": "0.0"}

        adapter = Overclaiming(replies={}, default=_answer("ok"))
        report = TestRunner(adapter, grader, workers=1).run(CASES)

        assert report.context["adapter"] == "overclaiming"
        assert report.context["pass_threshold"] == grader.pass_threshold
