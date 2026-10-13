"""The adapter seam: how the harness reaches the system under test.

Everything upstream of this - suites, the runner, evaluators, gates - should be
indifferent to whether the agent is a simulator in this process or a deployed
service behind HTTP. An adapter is the one place that difference lives: it takes
a user turn and hands back a `ChatResponse`, the vocabulary the evaluators
already grade.

`AgentAdapter` is a Protocol, not a base class. An adapter implements a shape; it
does not inherit from the framework, so a team can write one around their own
client without subclassing anything here.

To add an adapter:

    class MyAdapter:
        def __enter__(self) -> MyAdapter:
            self._client = open_client()        # once per run, not per call
            return self

        def __exit__(self, *exc: object) -> None:
            self._client.close()

        def send(self, message, *, session_id, principal="default") -> ChatResponse:
            payload = self._client.ask(message, session=session_id)
            return ChatResponse(reply=payload["text"], conversation_id=session_id)

        def describe(self) -> dict[str, str]:
            return {"adapter": "my-adapter", "model": self._model}
"""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Protocol, TypeVar, runtime_checkable

from ..core.contracts import ChatResponse

_T = TypeVar("_T")


@runtime_checkable
class AgentAdapter(Protocol):
    """One system under test, as the runner sees it.

    Synchronous on purpose. The runner is a thread pool and the work is I/O
    bound, so threads already give the concurrency; an async seam would push an
    event loop into every caller and every adapter author for no measured gain.

    `isinstance(x, AgentAdapter)` only confirms the four methods exist. It says
    nothing about their signatures - that is what the type checker is for.
    """

    def __enter__(self) -> AgentAdapter:
        """Open whatever the adapter needs for a whole run, and return it.

        The lifecycle belongs to the adapter so that an HTTP implementation can
        hold one pooled client for every case instead of paying a connection per
        call. `send` is only valid between `__enter__` and `__exit__`.
        """
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        """Release the transport. Runs whether the run finished or blew up.

        Returns None rather than bool: an adapter has no business suppressing an
        exception raised by the run it was carrying.
        """
        ...

    def send(
        self, message: str, *, session_id: str, principal: str = "default"
    ) -> ChatResponse:
        """Deliver one user turn and return what the system said.

        `session_id` is chosen by the caller. Turns sharing one belong to the
        same conversation and must see each other's history; turns with
        different ids must not, which is what keeps test cases from
        contaminating one another.

        `principal` names the identity the turn is sent as. The adapter resolves
        the name to whatever the system needs - a token, headers, a user and
        tenant. It is a label, never the credential itself: it is recorded in
        reports.

        MUST be safe to call concurrently from multiple threads. The runner
        sends different sessions from different workers at the same time, so
        anything shared across calls - a client, a token cache, a session map -
        has to tolerate that. Turns within one session arrive in order, never
        interleaved. Nothing in this module can enforce this; it is a contract
        on whoever implements it, and breaking it shows up as cross-case
        contamination rather than as an exception.

        Report only what the system returned. A field it does not expose stays
        empty - no retrieved chunks, no citations - so the evaluators that need
        it call the case unmeasurable instead of grading an invention. Raise
        when the turn could not be delivered or its reply could not be read; the
        runner records that as a failed case. Do not synthesise a response to
        stand in for one.
        """
        ...

    def describe(self) -> dict[str, str]:
        """Provenance for the run report: which system this was.

        The runner cannot reach inside an adapter for a model or retriever name
        the way it could with an in-process engine, so the adapter states its
        own. The values land in `RunReport.context`, are written into every
        report, and are what the regression comparison reads to decide whether
        two runs are comparable at all.

        The keys are the implementer's choice. Include whatever would make two
        runs differ for reasons other than the agent's behaviour - endpoint,
        model, deployed version, corpus revision - and keep the keys stable from
        run to run: a renamed key compares as nothing. Strings only, because the
        mapping is serialised and compared by equality. Never a secret; reports
        are artifacts.

        Must not depend on a turn having been sent: the runner records it when
        the run starts.
        """
        ...


class AdapterLifecycle:
    """Reentrant `with` support shared by adapters, so each does not re-derive it.

    A depth, not a flag: one adapter can be entered by several runs, and the
    first to finish must not close it under the others. A subclass calls
    `super().__init__()`, then guards `send` and `describe` with
    `_require_open`.

    An adapter that owns a transport (an HTTP client) overrides `_open` and
    `_close`. They run under the counter's lock on the first entry and the
    last exit, so the transport is built once per run, never per `with` or per
    call, and a racing second entry cannot see it half-built. If `_open`
    raises, the adapter stays closed.
    """

    def __init__(self) -> None:
        self._depth = 0
        # Guards the counter only. `send` is never serialised behind it.
        self._depth_lock = threading.Lock()

    def __enter__(self: _T) -> _T:
        assert isinstance(self, AdapterLifecycle)
        with self._depth_lock:
            if self._depth == 0:
                self._open()
            self._depth += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        with self._depth_lock:
            # Floored so a stray manual __exit__ cannot leave the next `with`
            # block closed from the inside.
            if self._depth == 0:
                return
            self._depth -= 1
            if self._depth == 0:
                self._close()

    def _open(self) -> None:
        """Acquire the transport. Default: there is none."""

    def _close(self) -> None:
        """Release the transport. Must tolerate being called after a failed run."""

    def _require_open(self, method: str) -> None:
        if self._depth == 0:
            raise RuntimeError(
                f"{type(self).__name__}.{method}() called outside its `with` block; "
                "enter the adapter before using it."
            )


__all__ = ["AdapterLifecycle", "AgentAdapter"]
