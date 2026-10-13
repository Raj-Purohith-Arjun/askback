"""Phase 1 acceptance: one suite, two adapters, the same verdicts.

The claim the adapter seam makes is that everything upstream of it - datasets,
runner, evaluators - is indifferent to how the agent is reached. So the shipped
datasets are run twice against the same simulator: once in this process, once
over its own HTTP API, and the two reports must agree case by case.

A disagreement is never noise to be tolerated here. It means the HTTP mapping or
the Protocol lost something the evaluators read. Do not loosen the comparison to
make it pass; fix the seam, or record why it cannot be fixed.

No socket and no port: the app is called through httpx's ASGI transport. That
transport is async-only, and the adapter is synchronous by design, so a small
bridge below runs each request on a private event loop.

The endpoint is `/api/console/chat` rather than `/chat`: it returns the retrieved
chunks and tool calls the evaluators grade, where `/chat` returns an integrator's
summary. This test is that endpoint's contract.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from optional_deps import requires

requires("httpx", extra="http", test_module="tests/test_acceptance.py")
requires("fastapi", extra="server", test_module="tests/test_acceptance.py")
import httpx  # noqa: E402

from askback.adapters.factory import resolve_principals  # noqa: E402
from askback.adapters.http import HTTPAdapter  # noqa: E402
from askback.adapters.in_process import InProcessAdapter  # noqa: E402
from askback.api.server import create_app  # noqa: E402
from askback.core.config import PROJECT_ROOT, AppConfig, resolve_path  # noqa: E402
from askback.datasets.loader import load_cases  # noqa: E402
from askback.evaluation.engine import EvaluationEngine  # noqa: E402
from askback.evaluation.regression import BaselineCase, load_baseline  # noqa: E402
from askback.evaluation.results import CaseResult, RunReport  # noqa: E402
from askback.runner import TestRunner  # noqa: E402
from askback_sim.engine import ConversationEngine  # noqa: E402

#: What must match. Trace ids, latencies and ticket ids are random or timed per
#: call and are not compared; none of them feeds a check.
COMPARED = ("case_id", "passed", "score", "route", "status")


class SyncASGITransport(httpx.BaseTransport):
    """`httpx.ASGITransport` for a synchronous client.

    `asyncio.run` per request: no loop outlives a call, so there is nothing to
    share between the runner's worker threads and nothing to close.
    """

    def __init__(self, app: Any) -> None:
        self._asgi = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def call() -> httpx.Response:
            response = await self._asgi.handle_async_request(request)
            body = await response.aread()
            await response.aclose()
            return httpx.Response(
                response.status_code, headers=response.headers, content=body, request=request
            )

        return asyncio.run(call())


#: The HR example's config: the simulator, its identities, its suites and its baseline.
HR_CONFIG = AppConfig.load(PROJECT_ROOT / "examples" / "hr" / "config.yaml")


@pytest.fixture(scope="module")
def reports() -> tuple[RunReport, RunReport]:
    """The shipped datasets, run once through each adapter."""
    config = HR_CONFIG
    principals = resolve_principals(config)
    cases = load_cases(resolve_path(config.evaluation["dataset_dir"]), principals=principals)

    engine = ConversationEngine.from_config(config)

    def grader() -> EvaluationEngine:
        # Same config, so the same judge for both runs: only the adapter may differ.
        return EvaluationEngine.from_config(config.evaluation)

    in_process = TestRunner(InProcessAdapter(engine, principals), grader(), workers=1).run(cases)

    http_adapter = HTTPAdapter(
        "http://simulator.test/api/console/chat",
        request_template={"message": "{message}", "conversation_id": "{session_id}"},
        principal_headers={label: {} for label in principals},
        # The simulator takes identity in the body; same mapping as in-process.
        principal_fields={
            label: {"user_id": user_id, "tenant_id": tenant_id}
            for label, (user_id, tenant_id) in principals.items()
        },
        max_attempts=1,
        transport=SyncASGITransport(create_app(config)),
    )
    over_http = TestRunner(http_adapter, grader(), workers=1).run(cases)
    return in_process, over_http


def _compared(case: CaseResult | BaselineCase) -> dict[str, Any]:
    # Rounded as reports round it; a float's last bits are not a verdict.
    return {**{k: getattr(case, k) for k in COMPARED}, "score": round(case.score, 4)}


def test_both_runs_executed_every_shipped_case(reports: tuple[RunReport, RunReport]) -> None:
    """Guards the comparison below against agreeing about nothing."""
    in_process, over_http = reports

    assert len(in_process.cases) >= 83
    assert [c.case_id for c in over_http.cases] == [c.case_id for c in in_process.cases]
    errored = {c.case_id: c.error for c in (*in_process.cases, *over_http.cases) if c.error}
    assert not errored, f"case(s) could not be executed at all: {errored}"


def test_the_two_adapters_agree_on_every_case(reports: tuple[RunReport, RunReport]) -> None:
    in_process, over_http = reports

    disagreements = [
        f"  in_process {_compared(local)}\n  http       {_compared(remote)}"
        for local, remote in zip(in_process.cases, over_http.cases, strict=True)
        if _compared(local) != _compared(remote)
    ]

    assert not disagreements, (
        f"{len(disagreements)} of {len(in_process.cases)} case(s) differ between adapters:\n"
        + "\n".join(disagreements)
    )


def test_the_v2_suites_grade_as_the_v1_baseline_did(reports: tuple[RunReport, RunReport]) -> None:
    """Phase 2 acceptance: converting the suites to schema v2 moved no verdict.

    The committed baseline was saved from the v1 files, one message per case.
    Every case in it must still end with the same verdict, score, route and
    status now that it is a one-turn conversation. Cases newer than the
    baseline are not this test's business.

    If it fails because a suite or the simulator changed on purpose, that is a
    new baseline: save it in the same commit with `askback baseline save`.
    """
    baseline = load_baseline(resolve_path(HR_CONFIG.evaluation["baseline_path"]))
    current = {case.case_id: _compared(case) for case in reports[0].cases}

    assert len(baseline.cases) >= 83
    moved = [
        f"  baseline {_compared(was)}\n  now      {current.get(was.case_id, 'not run')}"
        for was in baseline.cases
        if current.get(was.case_id) != _compared(was)
    ]
    assert not moved, f"{len(moved)} case(s) no longer grade as the baseline:\n" + "\n".join(moved)
