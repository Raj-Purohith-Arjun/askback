"""Run the custom evaluator over scripted replies, with no agent involved.

    python examples/custom-evaluator/run.py

`ScriptedAdapter` answers each message from a table, so a failing check points at the
evaluator and not at a model's mood. This is how you test an evaluator you wrote.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import disclaimer  # noqa: E402,F401  (importing it is what registers "disclaimer")

from askback.adapters.scripted import ScriptedAdapter  # noqa: E402
from askback.core.contracts import ChatResponse  # noqa: E402
from askback.datasets.schema import Expectations, TestCase, Turn  # noqa: E402
from askback.evaluation.engine import EvaluationEngine  # noqa: E402
from askback.runner import TestRunner  # noqa: E402

CONFIG = {
    "evaluators": ["disclaimer"],
    "evaluator_options": {
        "disclaimer": {"phrase": "this is not financial advice", "tags": ["financial"]},
    },
}


def case(case_id: str, question: str, *tags: str) -> TestCase:
    return TestCase(id=case_id, turns=[Turn(user=question, expect=Expectations())], tags=list(tags))


def main() -> int:
    adapter = ScriptedAdapter(
        replies={
            "Should I buy it?": ChatResponse(
                reply="Probably. This is not financial advice.", conversation_id="s"
            ),
            "Should I sell it?": ChatResponse(reply="Definitely.", conversation_id="s"),
            "What is the weather?": ChatResponse(reply="Sunny.", conversation_id="s"),
        }
    )
    cases = [
        case("buy", "Should I buy it?", "financial"),
        case("sell", "Should I sell it?", "financial"),
        case("weather", "What is the weather?"),
    ]
    report = TestRunner(adapter, EvaluationEngine.from_config(CONFIG), workers=1).run(cases)
    for result in report.cases:
        print(f"{result.case_id:<8} {'PASS' if result.passed else 'FAIL'}")
    return 0 if [c.case_id for c in report.cases if not c.passed] == ["sell"] else 1


if __name__ == "__main__":
    sys.exit(main())
