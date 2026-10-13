"""A custom evaluator: answers to certain cases must carry a disclaimer.

An evaluator turns one reply into named checks. This one is deliberately small, and
every branch is a decision worth copying:

- A case it does not apply to is **skipped**, not passed. It has nothing to say.
- A reply it cannot read is a **failure**, not a pass. Absent input is never evidence
  that nothing is wrong.
- A reply that is not an answer (a refusal, an honest "I don't know") needs no
  disclaimer, and is skipped for the same reason as the first.

Register it by importing this module, then name it in `evaluation.evaluators`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from askback.core.contracts import ChatResponse, ResponseStatus, Severity
from askback.core.errors import ConfigError
from askback.core.registry import EVALUATORS
from askback.core.text import normalize
from askback.datasets.schema import TestCase, Turn
from askback.evaluation.base import EvalResult, Evaluator


@EVALUATORS.register("disclaimer")
class DisclaimerEvaluator(Evaluator):
    """Replies to cases carrying one of `tags` must contain `phrase`."""

    name: ClassVar[str] = "disclaimer"
    description: ClassVar[str] = "Answers to tagged cases carry a required disclaimer."

    def __init__(self, phrase: str = "", tags: Sequence[str] = (), **options: Any) -> None:
        super().__init__(**options)
        # Refused at construction, not discovered as a pass on every case.
        if not phrase.strip():
            raise ConfigError("evaluator_options.disclaimer.phrase is required")
        if not tags:
            raise ConfigError("evaluator_options.disclaimer.tags needs at least one tag")
        self.phrase = normalize(phrase)
        self.tags = set(tags)

    def evaluate(self, case: TestCase, turn: Turn, response: ChatResponse) -> EvalResult:
        if not self.tags & set(case.tags):
            return self.build([])  # no checks means skipped, which is what we mean

        if response.status in (ResponseStatus.UNKNOWN, ResponseStatus.ERROR):
            return self.build([
                self.check(
                    "disclaimer_present",
                    False,
                    detail=f"The reply was {response.status.value}, so it could not be checked.",
                    severity=Severity.CRITICAL,
                )
            ])

        if response.status is not ResponseStatus.ANSWERED:
            return self.build([])

        present = self.phrase in normalize(response.reply)
        return self.build([
            self.check(
                "disclaimer_present",
                present,
                detail="Disclaimer present." if present else "Answer has no disclaimer.",
                severity=Severity.CRITICAL,
                expected=self.phrase,
            )
        ])
