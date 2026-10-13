"""Grade a Python function with the framework: a custom adapter, no HTTP.

Use this shape when the agent you are testing is a library call, a CLI, a queue
consumer or anything else the built-in `http` adapter does not reach. The adapter
is the only code you write; the suite, evaluators, gates and reports are the same.

    python examples/python-adapter/run.py

Runs the toy agent's FAQ suite (examples/toy-agent/suites/faq.yaml) against a
function in this file, and exits with the framework's own exit code.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import TracebackType

from askback.core.contracts import ChatResponse, ResponseStatus
from askback.datasets import load_cases
from askback.evaluation.engine import EvaluationEngine
from askback.evaluation.gates import evaluate_gates
from askback.evaluation.safety_eval import declare_side_effecting_tools
from askback.runner import TestRunner

SUITE = Path(__file__).resolve().parents[1] / "toy-agent" / "suites"


def my_agent(question: str, history: list[str]) -> tuple[str, bool]:
    """The system under test: any callable. Returns (reply, knew_the_answer)."""
    text = question.lower()
    if "leave" in text:
        return "Full-time employees receive 24 days of paid annual leave per year.", True
    if "expense" in text:
        return "Expense claims must be submitted within 30 days.", True
    return "I do not have that information.", False


class FunctionAdapter:
    """An `AgentAdapter` around `my_agent`. A shape, not a subclass."""

    def __init__(self) -> None:
        self._history: dict[str, list[str]] = {}
        # `send` is called from several worker threads at once, one per session.
        self._lock = threading.Lock()

    def __enter__(self) -> FunctionAdapter:
        # Open a connection, start a client, load a model: once per run, here.
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        # Release it. Runs whether or not the run finished.
        return None

    def send(self, message: str, *, session_id: str, principal: str = "default") -> ChatResponse:
        with self._lock:
            history = self._history.setdefault(session_id, [])
            earlier = list(history)
            history.append(message)
        reply, knew = my_agent(message, earlier)
        # Report only what the agent gave us. No retrieved chunks, no citations,
        # no route: the evaluators that need them will call the case unmeasurable
        # rather than grade something we invented.
        return ChatResponse(
            reply=reply,
            conversation_id=session_id,
            status=ResponseStatus.ANSWERED if knew else ResponseStatus.NO_ANSWER,
        )

    def describe(self) -> dict[str, str]:
        # What would make two runs differ for reasons other than behaviour.
        # Strings only, stable keys, never a secret: it is written into reports.
        return {"adapter": "function", "agent": "my_agent v1"}


def main() -> int:
    # This agent has no tools that act on the world; saying so is what makes the
    # tool-misuse check measurable. Without a declaration it fails every case.
    declare_side_effecting_tools(lambda: [])

    cases = load_cases(SUITE)
    report = TestRunner(FunctionAdapter(), EvaluationEngine.from_config({})).run(cases)

    summary = report.summary()
    print(f"{summary['passed']}/{summary['total']} passed, "
          f"{summary['critical_failures']} critical failure(s)")
    verdict = evaluate_gates(report, {})
    for gate in verdict.gates:
        print(f"{gate.status}: {gate.reason}")
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
