"""Rule-based evaluator: deterministic checks with no model in the loop.

Covers route, intent, status, tool usage, required facts, prohibited content,
citation count and response schema. These are the cheapest and most stable
signals available, so run them first and treat a failure here as a real defect.
"""

from __future__ import annotations

import re
from typing import ClassVar

from ..core.contracts import ChatResponse, ResponseStatus, Severity
from ..core.registry import EVALUATORS
from ..core.text import normalize, tolerate_line_breaks
from ..datasets.schema import TestCase, Turn
from .base import Check, EvalResult, Evaluator

#: Facts wrapped in slashes are treated as regular expressions: /within \d+ days/
_REGEX_FACT = re.compile(r"^/(.+)/$", re.DOTALL)


def fact_present(fact: str, text: str) -> bool:
    """Match a required fact against the reply.

    Plain strings match case-insensitively with whitespace collapsed, so dataset
    authors do not have to reproduce the source formatting exactly. Wrap a fact
    in /slashes/ when you need a regular expression; a space in one matches a run of
    whitespace too, so neither kind of fact depends on where the agent's renderer wrapped.
    """
    pattern = _REGEX_FACT.match(fact.strip())
    if pattern:
        return re.search(tolerate_line_breaks(pattern.group(1)), text, re.IGNORECASE) is not None
    return normalize(fact) in normalize(text)


@EVALUATORS.register("rule_based")
class RuleBasedEvaluator(Evaluator):
    """Asserts the structural expectations a turn declares."""

    name: ClassVar[str] = "rule_based"
    description: ClassVar[str] = "Route, tool, status, facts, prohibited content, citations, schema."

    def evaluate(self, case: TestCase, turn: Turn, response: ChatResponse) -> EvalResult:
        expect = turn.expect
        checks: list[Check] = []

        # --- routing --------------------------------------------------------- #
        if expect.expected_route:
            actual = response.route
            checks.append(
                self.check(
                    "expected_route",
                    actual == expect.expected_route,
                    detail=f"Routed to {actual!r}.",
                    severity=Severity.CRITICAL,
                    expected=expect.expected_route,
                    actual=actual,
                )
            )

        if expect.expected_intent:
            checks.append(
                self.check(
                    "expected_intent",
                    response.intent.name == expect.expected_intent,
                    detail=f"Classified as {response.intent.name!r} "
                    f"(confidence {response.intent.confidence:.2f}).",
                    expected=expect.expected_intent,
                    actual=response.intent.name,
                )
            )

        if expect.expected_status:
            # UNKNOWN matches no expectation, including a case that (absurdly)
            # expects "unknown": the agent said nothing the check could grade.
            # Failed rather than skipped, so a status-blind agent cannot go green.
            unreadable = response.status is ResponseStatus.UNKNOWN
            checks.append(
                self.check(
                    "expected_status",
                    not unreadable and response.status.value == expect.expected_status,
                    detail=(
                        "The agent reported no status, so the expected status "
                        f"{expect.expected_status!r} could not be evaluated."
                        if unreadable
                        else f"Turn ended as {response.status.value!r}."
                    ),
                    severity=Severity.CRITICAL,
                    expected=expect.expected_status,
                    actual=response.status.value,
                )
            )

        # --- tools ----------------------------------------------------------- #
        used = set(response.tool_names)
        if expect.expected_tools:
            missing = [t for t in expect.expected_tools if t not in used]
            checks.append(
                self.check(
                    "expected_tools",
                    not missing,
                    score=1.0 - len(missing) / len(expect.expected_tools),
                    detail=f"Missing tool calls: {missing}." if missing else "All expected tools called.",
                    expected=expect.expected_tools,
                    actual=sorted(used),
                )
            )

        if expect.forbidden_tools:
            violations = sorted(used & set(expect.forbidden_tools))
            checks.append(
                self.check(
                    "forbidden_tools",
                    not violations,
                    detail=f"Called forbidden tools: {violations}." if violations else "No forbidden tools called.",
                    severity=Severity.CRITICAL,
                    expected=f"none of {expect.forbidden_tools}",
                    actual=sorted(used),
                )
            )

        # --- content --------------------------------------------------------- #
        if expect.required_facts:
            missing = [f for f in expect.required_facts if not fact_present(f, response.reply)]
            found = len(expect.required_facts) - len(missing)
            checks.append(
                self.check(
                    "required_facts",
                    not missing,
                    score=found / len(expect.required_facts),
                    detail=f"{found}/{len(expect.required_facts)} facts present."
                    + (f" Missing: {missing}." if missing else ""),
                    severity=Severity.CRITICAL,
                    expected=expect.required_facts,
                    actual=response.reply[:400],
                )
            )

        if expect.prohibited_content:
            present = [p for p in expect.prohibited_content if fact_present(p, response.reply)]
            checks.append(
                self.check(
                    "prohibited_content",
                    not present,
                    detail=f"Reply contains prohibited content: {present}." if present else "Clean.",
                    severity=Severity.CRITICAL,
                    expected=f"none of {expect.prohibited_content}",
                    actual=present,
                )
            )

        if expect.min_citations:
            count = len(response.citations)
            checks.append(
                self.check(
                    "min_citations",
                    count >= expect.min_citations,
                    score=min(1.0, count / expect.min_citations),
                    detail=f"{count} citation(s) present, {expect.min_citations} required.",
                    expected=expect.min_citations,
                    actual=count,
                )
            )

        # --- schema ---------------------------------------------------------- #
        # Always runs: a malformed response is a defect regardless of the case.
        problems = schema_problems(response)
        checks.append(
            self.check(
                "schema_valid",
                not problems,
                detail="; ".join(problems) if problems else "Response is well formed.",
                severity=Severity.CRITICAL,
                actual=problems,
            )
        )

        return self.build(checks)


def schema_problems(response: ChatResponse) -> list[str]:
    """Structural defects that make a response unusable downstream."""
    problems: list[str] = []
    if not response.reply.strip():
        problems.append("reply is empty")
    if not response.conversation_id:
        problems.append("conversation_id is missing")
    if not response.route:
        problems.append("route is missing")
    if response.error:
        problems.append(f"error field set: {response.error}")

    retrieved = set(response.retrieved_doc_ids)
    invented = sorted({c.doc_id for c in response.citations} - retrieved)
    if invented:
        problems.append(f"citations reference documents that were not retrieved: {invented}")
    return problems