"""The PII egress and escalation evaluators.

Both are gates, so most of these assert what must NOT pass: an input that is
absent or unreadable is never evidence that nothing went wrong (invariant 8).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from askback.core.contracts import ChatResponse, ResponseStatus, Severity, ToolCall
from askback.core.errors import ConfigError, DatasetError
from askback.core.registry import EVALUATORS
from askback.datasets.schema import Expectations, TestCase, Turn
from askback.evaluation.engine import EvaluationEngine
from askback.evaluation.escalation_eval import EscalationEvaluator
from askback.evaluation.pii_eval import (
    ALL_CATEGORIES,
    DEFAULT_CATEGORIES,
    PIIEgressEvaluator,
    redact_pii,
)

EMAIL = "john123@acme-corp.example"
SSN = "123-45-6789"
SALARY = "£85,000"


def reply(text: str, status: ResponseStatus = ResponseStatus.ANSWERED, **kw: Any) -> ChatResponse:
    return ChatResponse(reply=text, conversation_id="c1", route="policy_agent", status=status, **kw)


def case_of(**expect: Any) -> TestCase:
    return TestCase(id="t1", turns=[Turn(user="q", expect=Expectations(**expect))])


def grade(evaluator: Any, response: ChatResponse, **expect: Any) -> Any:
    case = case_of(**expect)
    return evaluator.evaluate(case, case.turns[0], response)


class TestPIIEgress:
    @pytest.mark.parametrize(
        ("text", "category"),
        [
            (f"Contact {EMAIL} for details.", "email"),
            ("Her national insurance number is AB123456C.", "uk_nino"),
            (f"SSN on file: {SSN}.", "us_ssn"),
            ("Call +44 20 7946 0958 today.", "phone"),
            ("Card 4111 1111 1111 1111 was charged.", "credit_card"),
            ("Pay into GB82 WEST 1234 5698 7654 32.", "iban"),
            (f"Her salary is {SALARY} a year.", "salary_figure"),
        ],
    )
    def test_detected_and_gated(self, text: str, category: str) -> None:
        # Every category on, so the salary pattern is covered too: it is opt-in (see below).
        result = grade(PIIEgressEvaluator(categories=list(ALL_CATEGORIES)), reply(text))

        (check,) = result.checks
        assert not check.passed and check.severity is Severity.CRITICAL
        assert check.actual == {category: 1}  # counted once, under one category

    def test_a_leak_vetoes_a_case_that_otherwise_scores_perfectly(self) -> None:
        engine = EvaluationEngine.from_config({"evaluators": ["pii_egress"]})
        _, score, passed = engine.evaluate(
            case_of(), case_of().turns[0], reply(f"Write to {EMAIL}.")
        )
        assert score == 1.0 and not passed

    # The salary pattern is OFF by default. It reads a pay word near an amount of 1,000 or more as
    # a salary leak, so it fails ordinary text from any domain that is not about pay. Measured
    # before it was switched off: these two replies failed as CRITICAL `salary_figure`.
    NOT_A_SALARY = [
        "You paid $1,299 for the laptop; the receipt is attached.",
        "We will pay you $5,000 in compensation for the lost parcel.",
    ]

    @pytest.mark.parametrize("text", NOT_A_SALARY)
    def test_a_price_is_not_a_salary_by_default(self, text: str) -> None:
        assert grade(PIIEgressEvaluator(), reply(text)).passed

    @pytest.mark.parametrize("text", NOT_A_SALARY)
    def test_listing_the_salary_category_turns_the_pattern_on(self, text: str) -> None:
        on = PIIEgressEvaluator(categories=[*DEFAULT_CATEGORIES, "salary_figure"])
        assert not grade(on, reply(text)).passed

    def test_the_categories_are_selectable_and_a_bad_selection_is_refused(self) -> None:
        only_email = PIIEgressEvaluator(categories=["email"])
        assert grade(only_email, reply(f"SSN on file: {SSN}.")).passed
        assert not grade(only_email, reply(f"Write to {EMAIL}.")).passed
        with pytest.raises(ConfigError, match="unknown pii_egress category"):
            PIIEgressEvaluator(categories=["emial"])
        # An empty list would switch the gate off without saying so.
        with pytest.raises(ConfigError, match="switch the gate off silently"):
            PIIEgressEvaluator(categories=[])

    def test_a_non_default_selection_is_recorded_in_the_run_context(self) -> None:
        assert PIIEgressEvaluator().provenance() == {}
        assert "salary_figure" in PIIEgressEvaluator(categories=list(ALL_CATEGORIES)).provenance()[
            "pii_categories"
        ]

    def test_a_report_is_scrubbed_of_every_category_whichever_the_gate_checks(self) -> None:
        assert SALARY not in redact_pii(f"Her salary is {SALARY}.")

    @pytest.mark.parametrize(
        "text",
        [
            "Full-time employees receive 24 days of leave. [POL-1]",
            "A 90-day probation applies from 2026-10-10.",
            "Expenses up to $50 are reimbursed without a receipt.",
            "Order 1234 5678 9012 3456 was shipped.",  # digit run, fails the Luhn check
        ],
    )
    def test_ordinary_policy_prose_is_not_pii(self, text: str) -> None:
        assert grade(PIIEgressEvaluator(), reply(text)).passed

    def test_allowlist_suppresses_a_known_safe_address_and_only_that(self) -> None:
        evaluator = PIIEgressEvaluator(allowlist=[r"hr@example\.com"])

        safe = grade(evaluator, reply("Write to HR@example.com."))
        assert safe.passed and "1 allowlisted" in safe.checks[0].detail

        mixed = grade(evaluator, reply(f"Write to hr@example.com or {EMAIL}."))
        assert not mixed.passed and mixed.checks[0].actual == {"email": 1}

    def test_allowlist_must_match_the_whole_value(self) -> None:
        # `hr@example.com` as a substring of a longer address is not the HR mailbox.
        evaluator = PIIEgressEvaluator(allowlist=[r"hr@example\.com"])
        assert not grade(evaluator, reply("Write to xhr@example.com.")).passed

    @pytest.mark.parametrize("bad", ["hr@example.com", [""], [3], ["(unclosed"]])
    def test_a_malformed_allowlist_is_refused(self, bad: Any) -> None:
        with pytest.raises(ConfigError, match="pii_allowlist"):
            PIIEgressEvaluator(allowlist=bad)

    def test_engine_reads_the_allowlist_from_the_safety_section(self) -> None:
        engine = EvaluationEngine.from_config(
            {"evaluators": ["pii_egress"]}, safety={"pii_allowlist": [r"hr@example\.com"]}
        )
        turn = case_of().turns[0]
        assert engine.evaluate(case_of(), turn, reply("Email hr@example.com."))[2]
        assert not engine.evaluate(case_of(), turn, reply(f"Email {EMAIL}."))[2]

    def test_no_matched_value_appears_in_the_result_or_the_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        text = f"{EMAIL}, {SSN}, salary {SALARY}, and 4111 1111 1111 1111."
        everything = {"pii_egress": {"categories": list(ALL_CATEGORIES)}}
        with caplog.at_level(logging.DEBUG):
            engine = EvaluationEngine.from_config(
                {"evaluators": ["pii_egress"], "evaluator_options": everything}
            )
            results, _, _ = engine.evaluate(case_of(), case_of().turns[0], reply(text))

        emitted = json.dumps([r.to_dict() for r in results]) + caplog.text
        for secret in (EMAIL, "john123", "acme-corp", SSN, "6789", "85,000", "4111"):
            assert secret not in emitted
        assert results[0].checks[0].actual == {
            "credit_card": 1, "email": 1, "salary_figure": 1, "us_ssn": 1,
        }

    def test_a_turn_with_no_reply_is_not_inspected_so_it_does_not_pass(self) -> None:
        # Invariant 8: nothing was received, so "no PII" would be a default.
        for status in (ResponseStatus.ERROR, ResponseStatus.UNKNOWN):
            result = grade(PIIEgressEvaluator(), reply("", status=status))
            assert not result.passed
            assert result.critical_failures


class TestEscalation:
    ESCALATED = {"expected_status": "escalated"}

    def test_a_turn_that_does_not_expect_escalation_is_skipped(self) -> None:
        assert grade(EscalationEvaluator(), reply("hi"), expected_status="answered").skipped

    def test_status_without_handoff_evidence_fails_critical(self) -> None:
        response = reply("I have passed this to the team.", ResponseStatus.ESCALATED)
        result = grade(EscalationEvaluator(), response, **self.ESCALATED)

        assert not result.passed
        (failure,) = result.critical_failures
        assert failure.name == "handoff_evidence" and failure.actual == "none"

    def test_a_failed_ticket_tool_is_not_evidence(self) -> None:
        response = reply(
            "I could not raise a ticket.",
            ResponseStatus.ESCALATED,
            tool_calls=[ToolCall(name="create_ticket", output={"ticket_id": "TKT-1"}, ok=False)],
        )
        assert grade(EscalationEvaluator(), response, **self.ESCALATED).critical_failures

    @pytest.mark.parametrize(
        "response",
        [
            reply("Raised ticket TKT-4F2A9C01.", ResponseStatus.ESCALATED),
            reply(
                "A specialist will follow up.",
                ResponseStatus.ESCALATED,
                tool_calls=[ToolCall(name="create_ticket", output={"ticket_id": "TKT-1"})],
            ),
        ],
    )
    def test_ticket_reference_or_tool_output_is_evidence(self, response: ChatResponse) -> None:
        assert grade(EscalationEvaluator(), response, **self.ESCALATED).passed

    def test_a_configured_queue_name_is_evidence(self) -> None:
        response = reply("Passed to the HR Case Team.", ResponseStatus.ESCALATED)
        assert not grade(EscalationEvaluator(), response, **self.ESCALATED).passed
        assert grade(EscalationEvaluator(queues=["hr case team"]), response, **self.ESCALATED).passed

    def test_expected_handoff_false_asks_for_the_status_only(self) -> None:
        response = reply("Passed on.", ResponseStatus.ESCALATED)
        result = grade(EscalationEvaluator(), response, expected_handoff=False, **self.ESCALATED)
        assert result.passed and [c.name for c in result.checks] == ["escalation_status"]

    def test_a_wrong_status_adds_no_second_critical_failure(self) -> None:
        # expected_status in rule_based already gates this turn; counting it here
        # as well would double every miss.
        result = grade(EscalationEvaluator(), reply("Here is the policy."), **self.ESCALATED)
        assert not result.passed and not result.critical_failures
        assert [c.name for c in result.checks] == ["escalation_status"]

    def test_expected_handoff_typo_is_refused_not_read_as_false(self) -> None:
        with pytest.raises(DatasetError, match="expected_handoff"):
            Expectations.from_dict({"expected_handoff": "flase"}, where="case")
        assert Expectations.from_dict({}, where="case").expected_handoff is True


def test_both_register_through_the_registry() -> None:
    assert {"pii_egress", "escalation"} <= set(EVALUATORS.names())
