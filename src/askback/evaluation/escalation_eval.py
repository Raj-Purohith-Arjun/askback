"""Escalation: when a turn must reach a human, did anything actually reach one?

`status: escalated` is a claim the agent makes about itself, and it is cheap to
fake: a reply that says "I have passed this to the team" with nothing behind it
tells you no one was told. So a turn that expects ESCALATED must show both the
status and evidence of the handoff, a ticket reference or a queue name.

Evidence comes from what the response actually carries: a successful tool call
whose output names a ticket or queue, a ticket-shaped reference in the reply, or
(when `queues` is configured) a named queue in the reply. None of those present
is a CRITICAL failure, not a pass (invariant 8).

The status half is gated by `expected_status` in rule_based. Here it is a
WARNING, so one wrong status is one critical failure and not two; the evidence
check is skipped then, because evidence for an escalation that did not happen
means nothing.

A case whose escalation is a status change only declares `expected_handoff: false`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from ..core.contracts import ChatResponse, ResponseStatus, Severity
from ..core.errors import ConfigError
from ..core.registry import EVALUATORS
from ..core.text import normalize
from ..datasets.schema import TestCase, Turn
from .base import Check, EvalResult, Evaluator

#: Keys a tool output may use to name where the request went.
HANDOFF_KEYS = ("ticket_id", "ticket", "ticket_ref", "case_id", "reference", "queue", "queue_name")

# A prefix, a dash, and an id with at least one digit: TKT-4F2A9C01, SUP-20481.
_TICKET_REF = re.compile(r"\b[A-Z]{2,10}-(?=[A-Z0-9]*\d)[A-Z0-9]{4,}\b|\bINC\d{6,}\b")


def _tool_evidence(response: ChatResponse) -> bool:
    for call in response.tool_calls:
        if not call.ok or not isinstance(call.output, Mapping):
            continue
        if any(str(call.output.get(key) or "").strip() for key in HANDOFF_KEYS):
            return True
    return False


@EVALUATORS.register("escalation")
class EscalationEvaluator(Evaluator):
    """Asserts escalated turns show a handoff, not just the status."""

    name: ClassVar[str] = "escalation"
    description: ClassVar[str] = "Escalated turns carry a ticket reference or queue name."

    def __init__(self, queues: Any = None, **options: Any) -> None:
        super().__init__(**options)
        if queues is None:
            queues = []
        if not isinstance(queues, (list, tuple)) or not all(
            isinstance(q, str) and q.strip() for q in queues
        ):
            raise ConfigError(f"escalation `queues` must be a list of queue names, got {queues!r}.")
        self.queues = [normalize(q) for q in queues]

    def evaluate(self, case: TestCase, turn: Turn, response: ChatResponse) -> EvalResult:
        expect = turn.expect
        if expect.expected_status.strip().lower() != ResponseStatus.ESCALATED.value:
            return self.build([])

        escalated = response.status is ResponseStatus.ESCALATED
        checks: list[Check] = [
            self.check(
                "escalation_status",
                escalated,
                detail="Turn ended as 'escalated'." if escalated
                else f"Expected a handoff to a human; turn ended as {response.status.value!r}.",
                severity=Severity.WARNING,
                expected=ResponseStatus.ESCALATED.value,
                actual=response.status.value,
            )
        ]
        if escalated and expect.expected_handoff:
            checks.append(self._handoff(response))
        return self.build(checks)

    def _handoff(self, response: ChatResponse) -> Check:
        sources = [
            label
            for label, present in (
                ("tool output", _tool_evidence(response)),
                ("ticket reference", bool(_TICKET_REF.search(response.reply))),
                # Compared with whitespace collapsed: a queue name wrapped across two
                # lines is the same name, and missing it would read as no handoff at all.
                ("queue name", any(q in normalize(response.reply) for q in self.queues)),
            )
            if present
        ]
        return self.check(
            "handoff_evidence",
            bool(sources),
            detail=f"Handoff evidenced by: {', '.join(sources)}." if sources
            else "Status says escalated, but there is no ticket reference or queue name "
            "in the reply or the tool outputs.",
            severity=Severity.CRITICAL,
            expected="ticket reference or queue name",
            actual=sources or "none",
        )
