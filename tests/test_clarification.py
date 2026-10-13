"""Clarifying questions: detection, the loop around them, and the two ways to end up stuck.

The four fixtures are verbatim production replies. Two questions with different
openers and two answers that are hard for different reasons - one cited, one with
no citation that ends by offering more help - because a detector written against
text the author made up proves nothing about the real system.

Tests drive the runner through `ScriptedAdapter`, as the rest of the suite does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from askback.adapters.scripted import ScriptedAdapter
from askback.core.contracts import ChatResponse, Citation
from askback.core.errors import ConfigError, DatasetError
from askback.datasets.loader import load_cases
from askback.datasets.schema import ClarificationSpec, Expectations, Persona, TestCase, Turn
from askback.evaluation.clarification import QuestionDetector, Round
from askback.evaluation.engine import EvaluationEngine
from askback.evaluation.llm_judge import ModelJudge
from askback.evaluation.results import RunReport
from askback.evaluation.rule_based import RuleBasedEvaluator
from askback.providers.base import LLMResponse
from askback.reporting.redaction import redact_report
from askback.runner import TestRunner

QUESTION_1 = """Supplementary Dental Cover Plan

Please share your specific question about the supplementary dental cover
plan so I can check the applicable policy details.

Examples:
- Eligibility for employees or family members
- Cover limit and contribution
- Enrolment process
- Claim procedure
- Renewal or opt-in dates
- Inclusion of retired staff or parents"""

QUESTION_2 = """Certification Eligibility Check

Please specify which course category or staff band you want to verify eligibility
criteria for.

Examples:
- Band 1 to Band 2
- Band 2 to Band 3
- Specific department or team
- Funding eligibility

If available, also share the employee's current role and target certification
level."""

ANSWER_1 = """WORKPLACE SAFETY / EQUIPMENT / VISITORS Policy

According to the Harborview Staff Handbook 2025:

- Staff must not remove safety equipment from marked areas, prop open secured
  doors, or leave visitors unescorted in restricted areas.
- Any of the above is treated as a disciplinary matter and is handled under the
  Council's Corrective Action Procedure.
- Staff may be asked to take part in a safety audit if there is reasonable
  concern about compliance. Refusing to take part in an audit request may lead
  to a formal warning.

These provisions are covered under:
- Chapter 5: Workplace Safety, Conduct & Corrective Action
- Harborview Staff Handbook 2025, pages 32-33"""

GREETING = """Hello john123, I'm Demobot.

I can help with organizational policy and helpdesk-related questions. Please
share your specific query or the policy topic you need assistance with."""

ANSWER_2 = """The plan is optional and the additional contribution is fully borne by the
employee. The contribution is recovered in two equal instalments, and the same
policy terms as the existing basic dental plan apply. Let me know if you would like
the claim settlement process as well."""


def _reply(text: str, **extra: Any) -> ChatResponse:
    return ChatResponse(reply=text, conversation_id="scripted", **extra)


def _runner(adapter: ScriptedAdapter, **kwargs: Any) -> TestRunner:
    return TestRunner(adapter, EvaluationEngine([RuleBasedEvaluator()]), workers=1, **kwargs)


def _case(*turns: Turn, persona: Persona | None = None) -> TestCase:
    return TestCase(id="c1", turns=list(turns), persona=persona)


def _turn(user: str, fact: str = "two equal instalments", **spec: Any) -> Turn:
    clarification = ClarificationSpec(**spec) if spec else None
    return Turn(
        user=user, expect=Expectations(required_facts=[fact]), clarification=clarification
    )


class FakeLLM:
    """A persona's model, scripted: a real `LLMProvider` shape, not a patch."""

    name = "fake"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def generate(self, messages: Any, *, context: Any = (), **options: Any) -> LLMResponse:
        self.prompts.append(messages[-1].content)
        return LLMResponse(text=self.replies.pop(0), prompt_tokens=100, completion_tokens=20)


class TestDetector:
    @pytest.mark.parametrize(
        ("text", "is_question"),
        [(QUESTION_1, True), (QUESTION_2, True), (ANSWER_1, False), (ANSWER_2, False)],
        ids=["question-1", "question-2", "answer-cited", "answer-ends-with-offer"],
    )
    def test_production_replies_are_classified(self, text: str, is_question: bool) -> None:
        detection = QuestionDetector().detect(_reply(text))
        assert detection.is_question is is_question, detection

    def test_the_score_and_the_signals_that_fired_are_recorded(self) -> None:
        detection = QuestionDetector().detect(_reply(QUESTION_1))
        assert {"request_imperative", "examples_list"} <= set(detection.signals)
        assert detection.score >= detection.threshold

    def test_a_line_break_inside_a_phrase_does_not_defeat_that_phrases_signal(self) -> None:
        # Phrase signals once matched a single literal space, so hard-wrapping "so I can check"
        # as "so I / can check" lost `purpose_clause` and the score fell from 1.15 to 0.90.
        # tests/test_wrapping.py is the general form: any fixture, any width, same verdict.
        wrapped = QUESTION_1.replace("plan so I can check", "plan so I\ncan check")
        whole = QuestionDetector().detect(_reply(QUESTION_1))
        broken = QuestionDetector().detect(_reply(wrapped))
        assert "purpose_clause" in broken.signals and broken.score == whole.score

    def test_a_citation_outweighs_a_trailing_question_mark(self) -> None:
        cited = _reply("Would you like the form?", citations=[Citation(doc_id="HR-1")])
        assert not QuestionDetector().detect(cited).is_question

    @pytest.mark.parametrize(
        "user", ["Hi", "Hello", "hi demobot", "Hello, are you there?", "Good morning"]
    )
    def test_the_real_greeting_is_not_a_question_when_the_user_asked_for_nothing(
        self, user: str
    ) -> None:
        detection = QuestionDetector().detect(_reply(GREETING), user_message=user)
        assert not detection.is_question and detection.suppressed
        # The override is shown, not silent: the score that would have fired is kept.
        assert detection.score >= detection.threshold and "user_made_no_request" in detection.signals

    @pytest.mark.parametrize(
        "user", ["dental", "what is the leave policy", "Hello, what is the leave policy"]
    )
    def test_the_same_invitation_is_a_clarification_when_the_user_did_ask_something(
        self, user: str
    ) -> None:
        # Structural, not phrase-based: identical text, different verdict, because the
        # user's message left something to clarify.
        detection = QuestionDetector().detect(_reply(GREETING), user_message=user)
        assert detection.is_question and not detection.suppressed

    def test_without_the_users_message_nothing_is_ruled_out(self) -> None:
        assert QuestionDetector().detect(_reply(GREETING)).is_question

    def test_a_name_the_reply_gives_itself_is_a_greeting_word_but_other_words_are_not(self) -> None:
        # "demobot" is the reply's own name ("I'm Demobot"); a topic that appears in a reply is not.
        assert not QuestionDetector().detect(_reply(GREETING), user_message="hi demobot").is_question
        assert QuestionDetector().detect(_reply(QUESTION_1), user_message="dental").is_question

    def test_a_suppressed_question_is_visible_in_the_report(self) -> None:
        adapter = ScriptedAdapter(replies={"Hi": _reply(GREETING)})
        turn = Turn(user="Hi", expect=Expectations(required_facts=["Demobot"]))
        report = _runner(adapter).run([_case(turn)])

        assert report.cases[0].passed, "a welcome is not a gap in the suite"
        shown = report.to_dict()["cases"][0]["turns"][0]["detection"]
        assert shown["suppressed"] is True and shown["is_question"] is False

    def test_weights_are_configurable_and_a_typo_is_refused(self) -> None:
        off = QuestionDetector(weights={"request_imperative": 0.0, "request_detail": 0.0})
        assert "request_imperative" not in off.detect(_reply(QUESTION_1)).signals
        with pytest.raises(ConfigError, match="request_imperitive"):
            QuestionDetector(weights={"request_imperitive": 1.0})


# Written once, from general sense, BEFORE the vocabulary was run against them, to measure
# whether the category-derived list generalises beyond the cases it was built around. The first
# run is the number that matters: 17 of 20, with every miss a greeting read as a request. They are
# a regression set now, not a held-out one: extending the vocabulary to fix a miss here is the
# move CLAUDE.md invariant 9 forbids. (message, is_a_greeting)
HELD_OUT_MESSAGES = [
    ("Hey there!", True),
    ("Good evening, hope you're doing well", True),
    ("Hola", True),
    ("thanks a lot, bye", True),
    ("Hi Demobot, anyone online?", True),
    ("Hello!!! Is this thing on?", True),
    ("morning", True),
    ("Heya, how are you today?", True),
    ("ok thank you so much", True),
    ("Greetings, I am john123", True),
    ("leave", False),
    ("hi, I need help with my claim", False),
    ("What's the status of order 1182?", False),
    ("can you help me", False),
    ("refund", False),
    ("Hello, what is the leave policy", False),
    ("is there a cafeteria on campus", False),
    ("how are you able to reset my password", False),
    ("my laptop is broken", False),
    ("Good morning. I would like to change my address.", False),
]
#: Greetings the vocabulary does not recognise. Known, measured, and deliberately left: each is
#: read as a request, so the reply is classified as before. Not a to-do list for the vocabulary.
KNOWN_GREETING_MISSES = {
    "Good evening, hope you're doing well",  # a pleasantry with content words in it
    "Hello!!! Is this thing on?",  # a presence check phrased without presence words
    "Greetings, I am john123",  # the user's own name is unknowable
}


class TestGreetingVocabularyGeneralises:
    def _read_as_greeting(self, message: str) -> bool:
        return QuestionDetector()._made_no_request(message, GREETING)

    def test_the_measured_hit_rate_and_exactly_which_greetings_it_misses(self) -> None:
        greetings = [m for m, is_greeting in HELD_OUT_MESSAGES if is_greeting]
        missed = {m for m in greetings if not self._read_as_greeting(m)}
        assert missed == KNOWN_GREETING_MISSES, (
            "the set of missed greetings changed: if the vocabulary grew, check CLAUDE.md "
            "invariant 9 before updating this"
        )
        assert len(greetings) - len(missed) == 7  # of 10

    def test_no_real_request_is_read_as_a_greeting(self) -> None:
        # The error that matters: it would hide a real clarification. 0 of 10 on this set; a small
        # set of requests that all carry a content word, so this is a floor, not a guarantee.
        requests = [m for m, is_greeting in HELD_OUT_MESSAGES if not is_greeting]
        assert [m for m in requests if self._read_as_greeting(m)] == []

    def test_a_request_made_only_of_ritual_words_is_the_known_blind_spot(self) -> None:
        # By construction a message of nothing but ritual words is read as asking nothing, so
        # "is anyone there" is a greeting. Wanting it otherwise means a different mechanism.
        assert self._read_as_greeting("is anyone there")


class TestSchema:
    def _write(self, tmp_path: Path, version: int, **case: Any) -> Path:
        body = {"id": "a", "turns": [{"user": "q", "clarification": {"answers": ["x"]}}], **case}
        path = tmp_path / "suite.json"
        path.write_text(json.dumps({"schema_version": version, "cases": [body]}), encoding="utf-8")
        return path

    def test_v3_reads_a_clarification_block_and_a_persona(self, tmp_path: Path) -> None:
        persona = {"goal": "ask about leave", "facts": {"grade": "C"}}
        (case,) = load_cases(self._write(tmp_path, 3, persona=persona))
        assert case.turns[0].clarification == ClarificationSpec(answers=["x"])
        assert case.persona is not None and case.persona.facts == {"grade": "C"}

    def test_a_v2_file_still_loads_and_refuses_the_new_keys(self, tmp_path: Path) -> None:
        plain = tmp_path / "v2.json"
        plain.write_text(
            json.dumps({"schema_version": 2, "cases": [{"id": "a", "turns": [{"user": "q"}]}]}),
            encoding="utf-8",
        )
        assert load_cases(plain)[0].turns[0].clarification is None
        with pytest.raises(DatasetError, match="needs schema_version: 3"):
            load_cases(self._write(tmp_path, 2))

    def test_forbid_never_answers_and_a_persona_needs_facts(self) -> None:
        with pytest.raises(DatasetError, match="never answers"):
            ClarificationSpec.from_dict({"policy": "forbid", "answers": ["x"]}, where="t")
        with pytest.raises(DatasetError, match="facts"):
            Persona.from_dict({"goal": "g", "facts": {}}, where="c")


class TestLoop:
    def test_the_question_is_answered_and_the_real_reply_is_scored(self) -> None:
        adapter = ScriptedAdapter(
            replies={"dental?": _reply(QUESTION_1), "Premium cost": _reply(ANSWER_2)}
        )
        case = _case(_turn("dental?", answers=["Premium cost"]))
        report = _runner(adapter).run([case])

        (result,) = report.cases
        turn = result.turns[0]
        assert result.passed and turn.reply == ANSWER_2
        assert [c[0] for c in adapter.calls] == ["dental?", "Premium cost"]
        (round_,) = turn.rounds
        assert round_.source == "scripted" and round_.detection.is_question
        # allow: it took a round, so the policy score is below perfect.
        policy = next(r for r in turn.results if r.evaluator == "clarification")
        assert policy.passed and policy.score == pytest.approx(1 - 1 / 3, abs=1e-4)

    def test_forbid_fails_a_turn_that_asked_and_grades_nothing_against_the_question(self) -> None:
        adapter = ScriptedAdapter(replies={"q": _reply(QUESTION_1)})
        report = _runner(adapter).run([_case(_turn("q", policy="forbid"))])

        turn = report.cases[0].turns[0]
        assert not report.cases[0].passed
        assert [r.evaluator for r in turn.results] == ["clarification"]
        assert len(adapter.calls) == 1, "forbid never answers"

    def test_require_fails_a_turn_that_answered_and_passes_one_that_asked(self) -> None:
        answered = ScriptedAdapter(replies={"q": _reply(ANSWER_2)})
        asked = ScriptedAdapter(replies={"q": _reply(QUESTION_2)})
        turn = _turn("q", policy="require", max_rounds=0)

        assert not _runner(answered).run([_case(turn)]).cases[0].passed
        assert _runner(asked).run([_case(turn)]).cases[0].passed

    def test_still_asking_after_max_rounds_is_a_hard_error_and_stops_the_conversation(self) -> None:
        adapter = ScriptedAdapter(replies={}, default=_reply(QUESTION_1))
        case = _case(
            _turn("q", answers=["a", "b", "c"], max_rounds=2), _turn("never sent")
        )
        report = _runner(adapter).run([case])

        result = report.cases[0]
        assert result.error is not None and "did not converge" in result.error
        assert result.unmeasured is None
        assert len(adapter.calls) == 3 and report.summary()["errors"] == 1

    def test_an_answer_we_send_is_never_read_as_a_greeting(self) -> None:
        # "ok" and "a" are ritual words, but as an answer to a question they are content: the
        # reply to them that is still a question must count, not be ruled out as a welcome.
        adapter = ScriptedAdapter(replies={}, default=_reply(QUESTION_1))
        report = _runner(adapter).run([_case(_turn("q", answers=["ok", "a"], max_rounds=2))])

        assert "did not converge" in (report.cases[0].error or "")

    def test_having_nothing_to_say_is_our_gap_not_an_agent_error(self) -> None:
        adapter = ScriptedAdapter(replies={}, default=_reply(QUESTION_1))
        case = _case(_turn("q", answers=["only one"], max_rounds=3), _turn("never sent"))
        report = _runner(adapter).run([case])

        result, summary = report.cases[0], report.summary()
        assert result.error is None and not result.passed
        assert result.unmeasured is not None and "no `persona`" in result.unmeasured
        assert summary["unmeasured"] == 1 and summary["errors"] == 0
        check = result.failed_checks[0]
        assert check.name == "clarification_unanswered" and "Add more" in check.detail
        assert len(adapter.calls) == 2, "scripted answer sent once, then stopped"

    def test_a_question_on_a_turn_with_no_block_is_a_gap_not_a_graded_answer(self) -> None:
        adapter = ScriptedAdapter(replies={"q": _reply(QUESTION_1)})
        report = _runner(adapter).run([_case(_turn("q"))])

        result = report.cases[0]
        assert not result.passed and result.unmeasured is not None
        assert "no `clarification` block" in result.unmeasured
        assert all(r.skipped for r in result.results)

    def test_a_turn_with_no_questions_serialises_as_it_always_did(self) -> None:
        adapter = ScriptedAdapter(replies={"q": _reply(ANSWER_2)})
        data = _runner(adapter).run([_case(_turn("q"))]).to_dict()

        turn = data["cases"][0]["turns"][0]
        assert not {"rounds", "detection", "unmeasured"} & set(turn)
        assert "unmeasured" not in data["summary"] and "persona_calls" not in data["summary"]


class TestPersona:
    PERSONA = Persona(goal="ask about the scheme", facts={"scheme": "supplementary dental plan", "years": "5"})

    def _run(self, llm: FakeLLM, adapter: ScriptedAdapter) -> RunReport:
        case = _case(_turn("q", answers=[]), persona=self.PERSONA)
        return _runner(adapter, persona_llm=llm, persona_prices=(1.0, 2.0)).run([case])

    def test_a_fact_on_the_sheet_is_stated_and_costed_apart_from_the_judge(self) -> None:
        llm = FakeLLM('{"answer": "The supplementary dental plan.", "fact_keys": ["scheme"]}')
        adapter = ScriptedAdapter(
            replies={"q": _reply(QUESTION_1), "The supplementary dental plan.": _reply(ANSWER_2)}
        )
        report = self._run(llm, adapter)

        assert report.cases[0].passed
        assert report.cases[0].turns[0].rounds[0].source == "persona"
        summary = report.summary()
        assert summary["persona_calls"] == 1 and summary["persona_cost_usd"] == pytest.approx(0.14)
        assert "judge_calls" not in summary

    @pytest.mark.parametrize(
        "model_says",
        [
            '{"answer": "I am in Group A.", "fact_keys": ["grade"]}',  # a key not on the sheet
            '{"answer": "I am in Group A.", "fact_keys": []}',  # cites nothing
            '{"answer": "I have 9 years.", "fact_keys": ["years"]}',  # a figure its fact lacks
            '{"answer": null, "fact_keys": []}',  # the model declines
        ],
    )
    def test_it_cannot_invent_a_fact_and_declines_instead(self, model_says: str) -> None:
        declined = self.PERSONA.unknown_response
        adapter = ScriptedAdapter(
            replies={"q": _reply(QUESTION_1), declined: _reply(ANSWER_2)}
        )
        report = self._run(FakeLLM(model_says), adapter)

        (round_,) = report.cases[0].turns[0].rounds
        assert round_.source == "declined" and round_.answer == declined
        assert "Group A" not in [c[0] for c in adapter.calls]


class TestJudgeHistory:
    def _judge_prompt(self, rounds: list[Round]) -> str:
        llm = FakeLLM('{"correctness": 1, "completeness": 1, "groundedness": 1, '
                      '"reasoning": 1, "clarity": 1, "rationale": "ok"}')
        turn = Turn(user="q", expect=Expectations(reference_answer="ref"))
        ModelJudge(llm).score(turn, _reply("a"), rounds)
        return llm.prompts[0]

    def test_the_exchange_reaches_the_judge_and_only_when_there_is_one(self) -> None:
        asked = Round(
            question="Which grade?",
            detection=QuestionDetector().detect(_reply("Which grade do you mean?")),
            answer="Grade C",
            source="scripted",
        )
        assert "Which grade?" in self._judge_prompt([asked])
        # No exchange, no preamble: the prompt - and the cache key - is what it was.
        assert self._judge_prompt([]).startswith("QUESTION:")


class TestReport:
    def _report(self) -> RunReport:
        adapter = ScriptedAdapter(
            replies={"q": _reply(QUESTION_1), "mail me at test-user@example.com": _reply(ANSWER_2)}
        )
        case = _case(_turn("q", answers=["mail me at test-user@example.com"]))
        return _runner(adapter).run([case])

    def test_rounds_survive_a_round_trip(self) -> None:
        report = self._report()
        again = RunReport.from_dict(report.to_dict())
        assert again.cases[0].turns[0].rounds[0].answer == "mail me at test-user@example.com"

    def test_round_text_is_redacted_and_then_withheld(self) -> None:
        data = self._report().to_dict()
        ci = json.dumps(redact_report(data, "ci"))
        assert "test-user@example.com" not in ci
        metrics = json.dumps(redact_report(data, "metrics_only"))
        assert "Eligibility" not in metrics and "dental" not in metrics
