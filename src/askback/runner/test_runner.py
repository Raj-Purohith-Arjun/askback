"""The test runner: dataset in, run report out.

The runner knows the system under test only as an `AgentAdapter`. Whether that
is the simulator in this process or a deployed agent is the adapter's business.

Each case is one conversation: its turns are sent in order under one session id
that no other case shares, so turns see each other's history and cases cannot
contaminate each other through memory. A case that declares `samples` is run that
many times, each in a session of its own: a repeat sent into a used session would
be answered by a system that remembers the first attempt, and would measure that
memory rather than the agent's variance. Cases run in a
thread pool because the work is I/O bound once a real model is wired in; set
`parallel_workers: 1` for deterministic ordering while debugging.

A reply that is a question rather than an answer is not graded as one. The
detector looks at every reply; a question is answered (a scripted answer, else the
case's persona) up to the turn's `max_rounds`, and the turn's expectations are
scored against the LAST reply. Two ways a turn can end still asking are kept
apart, because they blame different parties: it asked after every round we were
allowed (the agent did not converge - an error), or we had nothing to say (a gap
in the suite - unmeasured, which cannot pass and is counted on its own).

A metered judge can be given a budget. It is checked between cases, against what
the finished cases report having spent, so a run overshoots by at most the cases
in flight when the ceiling is crossed (one, with a single worker). Cases not yet
started are then not run, and the report says it is incomplete: a verdict on the
cases that happened to run first would be a verdict on nothing in particular.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from ..adapters import AgentAdapter
from ..core.contracts import ChatResponse
from ..core.logging import get_logger
from ..datasets.schema import DEFAULT_MAX_ROUNDS, ClarificationSpec, TestCase, Turn
from ..evaluation.base import EvalResult
from ..evaluation.clarification import (
    QuestionDetector,
    Round,
    gap_result,
    policy_result,
)
from ..evaluation.engine import EvaluationEngine
from ..evaluation.results import CaseResult, RunReport, SampleResult, TurnResult, response_snapshot
from ..providers.base import LLMProvider
from .responders import PersonaResponder, TurnResponder

log = get_logger(__name__)

ProgressHook = Callable[[CaseResult], None]


class TestRunner:
    """Executes test cases against an `AgentAdapter` and grades the replies."""

    #: Stops pytest trying to collect this class as a test class.
    __test__ = False

    def __init__(
        self,
        adapter: AgentAdapter,
        eval_engine: EvaluationEngine,
        *,
        workers: int = 4,
        on_case: ProgressHook | None = None,
        judge_budget_usd: float | None = None,
        detector: QuestionDetector | None = None,
        persona_llm: LLMProvider | None = None,
        persona_prices: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self.adapter = adapter
        self.eval_engine = eval_engine
        self.detector = detector or QuestionDetector()
        # Free to leave out: a case with no `persona` never asks for it.
        self.persona_llm = persona_llm
        self.persona_prices = persona_prices
        self.workers = max(1, int(workers))
        self.on_case = on_case
        self.judge_budget_usd = judge_budget_usd
        self._spend_lock = threading.Lock()
        self._spent_usd = 0.0

    def run(self, cases: Sequence[TestCase], *, run_id: str | None = None) -> RunReport:
        """Run every case and return the graded report.

        The adapter is entered here rather than left to the caller: its
        transport has to be open for `describe()` and for every send, and
        released if the run blows up. Adapters count their entries, so a
        caller already holding one open keeps it open afterwards.
        """
        with self.adapter:
            report = RunReport(
                run_id=run_id or f"run_{uuid.uuid4().hex[:10]}",
                context=self._context(),
            )
            started = time.perf_counter()
            self._spent_usd = 0.0

            if self.workers == 1:
                results = [self._run_if_affordable(c) for c in cases]
            else:
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    results = list(pool.map(self._run_if_affordable, cases))

            report.duration_s = time.perf_counter() - started

        # Preserve dataset order regardless of completion order.
        report.cases = [r for r in results if r is not None]
        if len(report.cases) < len(cases):
            report.incomplete = {
                "reason": "judge_budget_exceeded",
                "budget_usd": self.judge_budget_usd,
                "spent_usd": round(self._spent_usd, 6),
                "cases_run": len(report.cases),
                "cases_planned": len(cases),
            }
            log.warning(
                "Run %s stopped: judge spend $%.4f exceeds the $%.4f budget; %d of %d cases ran.",
                report.run_id, self._spent_usd, self.judge_budget_usd or 0.0,
                len(report.cases), len(cases),
            )
        report.finished_at = _now()

        summary = report.summary()
        log.info(
            "Run %s finished: %d/%d passed (%.0f%%) in %.1fs",
            report.run_id,
            summary["passed"],
            summary["total"],
            summary["pass_rate"] * 100,
            report.duration_s,
        )
        return report

    def _run_if_affordable(self, case: TestCase) -> CaseResult | None:
        """Run the case unless the judge budget is already spent; None means not run."""
        with self._spend_lock:
            if self.judge_budget_usd is not None and self._spent_usd > self.judge_budget_usd:
                return None
        result = self.run_case(case)
        usage = result.judge_usage
        if usage is not None:
            with self._spend_lock:
                self._spent_usd += usage.cost_usd
        return result

    def run_case(self, case: TestCase) -> CaseResult:
        """Run and grade one case, once or `case.samples` times. Never raises.

        The case passes when `case.required_samples` of its samples do. Samples
        run one after another, so the gate can be called early: once the passes
        still needed outnumber the samples left, the rest cannot change the
        verdict and are not sent. They are never skipped on the way to a pass,
        because stopping at K passes would hide the failures that make a case
        flaky, and finding those is what sampling is for.

        Needs the adapter open. `run()` sees to that; a direct caller must
        hold the adapter's `with` block itself.
        """
        planned, needed = case.samples, case.required_samples
        samples: list[SampleResult] = []
        passing = 0
        for number in range(1, planned + 1):
            sample = self._run_sample(case, number)
            samples.append(sample)
            passing += sample.passed
            if passing + (planned - number) < needed:
                log.info(
                    "Case %s: %d of %d samples passed with %d to go; %d needed, stopping.",
                    case.id, passing, number, planned - number, needed,
                )
                break

        # The first failure is what a reader of a failing case wants to see.
        shown = next((s for s in samples if not s.passed), samples[-1])
        passed = passing >= needed
        result = CaseResult(
            case_id=case.id,
            suite=case.suite,
            message=shown.message,
            score=round(sum(s.score for s in samples) / len(samples), 4),
            passed=passed,
            description=case.description,
            risk=case.risk,
            tags=list(case.tags),
            reply=shown.reply,
            route=shown.route,
            status=shown.status,
            latency_ms=shown.latency_ms,
            results=[r for s in samples for r in s.results],
            # A case that passed on K of N has no error, whatever the others hit.
            error=None if passed else shown.error,
            unmeasured=None if passed else shown.unmeasured,
            response=shown.response,
            samples=samples,
            samples_planned=planned,
            min_passing_samples=needed,
        )
        if self.on_case:
            self.on_case(result)
        return result

    def _run_sample(self, case: TestCase, number: int) -> SampleResult:
        """Send every turn of the case in one fresh session and grade the replies.

        A sample passes when every graded turn passes; its score is their mean. A
        turn the adapter cannot deliver ends the conversation there: the turns
        after it would be answered by a system that missed part of the history,
        so they are not sent, and the sample fails with that error and a score of
        zero, as a case that could not be run always has.
        """
        # One id per sample: it is what makes the turns a conversation, and what
        # keeps one sample's history out of the next. A case run once keeps the
        # bare id, so what it sends is what it always sent.
        session_id = f"{case.suite}:{case.id}"
        if case.samples > 1:
            session_id = f"{session_id}#{number}"
        sample = SampleResult(index=number, session_id=session_id, message=case.turns[0].user)

        for index, turn in enumerate(case.turns, start=1):
            outcome = TurnResult(index=index, user=turn.user, evaluated=turn.evaluate)
            sample.turns.append(outcome)
            try:
                response, state = self._converse(case, turn, index, session_id, outcome)
            except Exception as exc:  # noqa: BLE001 - report it, do not abort the run
                log.exception("Case %s sample %d turn %d could not be executed", case.id, number, index)
                outcome.error = f"{type(exc).__name__}: {exc}"
                sample.error = (
                    outcome.error
                    if len(case.turns) == 1
                    else f"turn {index} of {len(case.turns)}: {outcome.error}"
                )
                break

            outcome.reply = response.reply
            outcome.route = response.route
            outcome.status = response.status.value
            outcome.latency_ms = response.latency_ms
            if state.error is not None:
                # It was still asking after every round we were allowed: the agent did
                # not converge. A hard error, so it cannot read as a slow pass, and the
                # turns after it are not sent into a conversation that never resolved.
                outcome.error = state.error
                sample.error = _where(state.error, index, len(case.turns))
                break
            if state.gap is not None:
                # Our gap, not the agent's. Not graded (against a question nobody
                # answered), not passed, and not an error either.
                outcome.results = [gap_result(f"turn {index}: the agent asked and {state.gap}.")]
                # No credit for a turn nothing was measured on, and no pass.
                outcome.score, outcome.passed, outcome.unmeasured = 0.0, False, state.gap
                sample.results.extend(outcome.results)
                sample.unmeasured = f"turn {index}: {state.gap}"
                sample.message, sample.reply = turn.user, response.reply
                sample.route, sample.status = response.route, response.status.value
                break
            if not turn.evaluate:
                continue
            if state.policy_only is not None:
                outcome.results = [state.policy_only]
                outcome.score, outcome.passed = self.eval_engine.verdict(outcome.results)
            else:
                outcome.results, outcome.score, outcome.passed = self.eval_engine.evaluate(
                    case,
                    turn,
                    response,
                    rounds=outcome.rounds,
                    clarification=state.policy,
                )
            sample.results.extend(outcome.results)
            sample.message = turn.user
            sample.reply = response.reply
            sample.route = response.route
            sample.status = response.status.value
            sample.latency_ms = response.latency_ms
            sample.response = response_snapshot(response)

        scores = [t.score for t in sample.turns if t.score is not None]
        if sample.error is None and scores:
            sample.score = round(sum(scores) / len(scores), 4)
            sample.passed = sample.unmeasured is None and all(
                t.passed for t in sample.turns if t.evaluated
            )
        return sample

    def _converse(
        self, case: TestCase, turn: Turn, index: int, session_id: str, outcome: TurnResult
    ) -> tuple[ChatResponse, _TurnState]:
        """Send the turn, then answer the agent's questions until it stops asking.

        Every round is appended to `outcome.rounds` as it happens, so a failure
        part-way keeps the exchange so far. `max_rounds` of 0 answers nothing: that
        is how `forbid` and `require` assert on the first reply.
        """
        spec = turn.clarification
        # A turn that says nothing about questions answers none - unless the case
        # has a persona, whose whole purpose is to answer them.
        max_rounds = spec.max_rounds if spec else (DEFAULT_MAX_ROUNDS if case.persona else 0)
        policy = spec.policy if spec else "allow"

        def send(text: str) -> ChatResponse:
            return self.adapter.send(
                text,
                session_id=session_id,
                # The label only: the adapter owns turning it into an identity.
                principal=case.user_id,
            )

        response = send(turn.user)
        detection = self.detector.detect(response, user_message=turn.user)
        outcome.detection = detection
        asked_first = detection.is_question
        responder = TurnResponder(
            spec,
            PersonaResponder(
                self.persona_llm,
                case.persona,
                price_per_1k_prompt=self.persona_prices[0],
                price_per_1k_completion=self.persona_prices[1],
            )
            if case.persona is not None and self.persona_llm is not None
            else None,
            persona_declared=case.persona is not None,
        )
        nothing_to_say = False
        while detection.is_question and len(outcome.rounds) < max_rounds:
            answer = responder.next(response.reply, outcome.rounds)
            if answer is None:
                nothing_to_say = True
                break
            reply = send(answer.text)
            outcome.rounds.append(
                Round(
                    question=response.reply,
                    detection=detection,
                    answer=answer.text,
                    source=answer.source,
                    status=response.status.value,
                    latency_ms=response.latency_ms,
                    usage=answer.usage,
                )
            )
            response = reply
            # No `user_message` here: "the user asked for nothing" is about the opening
            # message. What we just sent is an answer to a question, which is content
            # whatever words it is made of ("a", "ok"), so it can never rule one out.
            detection = self.detector.detect(response)
            outcome.detection = detection

        state = _TurnState()
        if detection.is_question:
            if nothing_to_say:
                state.gap = responder.nothing_to_say()
            elif max_rounds > 0:
                state.error = (
                    f"the agent was still asking after {max_rounds} answer(s) and did not "
                    f"converge (signals: {', '.join(detection.signals) or 'none'})"
                )
            elif policy == "allow":
                state.gap = (
                    "this turn answers no questions"
                    + (" (`max_rounds: 0`). Raise `max_rounds` and supply answers or a persona, "
                       "or set `policy: require` to assert that it asks"
                       if spec else " and has no `clarification` block. Add one (schema_version: 3) "
                       "with answers, or give the case a `persona`")
                )
            else:
                # forbid / require with nothing answered: the policy is the assertion.
                state.policy_only = policy_result(
                    spec or ClarificationSpec(), asked_first=asked_first, rounds=0
                )
        elif spec is not None or outcome.rounds:
            state.policy = policy_result(
                spec or ClarificationSpec(max_rounds=max_rounds),
                asked_first=asked_first,
                rounds=len(outcome.rounds),
            )
        return response, state

    def _context(self) -> dict[str, Any]:
        """Recorded in the report so two runs can be judged comparable.

        The adapter says which system this was; the evaluation engine says how
        it was graded. The harness's keys go last so an adapter cannot
        overwrite the threshold the run was scored against.
        """
        return {
            **self.adapter.describe(),
            **self.eval_engine.describe(),
            **self.detector.provenance(),
        }


@dataclass(slots=True)
class _TurnState:
    """How a turn's conversation ended. At most one of the first three is set."""

    #: Still asking after max_rounds answers: the agent's failure, as a message.
    error: str | None = None
    #: Asked and we had nothing to say: the suite's gap, as the way to close it.
    gap: str | None = None
    #: Asked, and by policy nothing was answered: the policy is the whole verdict.
    policy_only: EvalResult | None = None
    #: The policy result to grade alongside the reply, when there is one.
    policy: EvalResult | None = None


def _where(message: str, index: int, total: int) -> str:
    return message if total == 1 else f"turn {index} of {total}: {message}"


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")
