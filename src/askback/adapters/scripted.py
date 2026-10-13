"""The scripted adapter: canned answers behind the adapter seam.

This is how a user of the library tests their own evaluators, gates and reports
without standing up an agent. The response for each message is decided by the
test, so a failing check points at the evaluator rather than at a model's mood.
It is a real implementation of `AgentAdapter`, not a mock: the runner cannot tell
it from any other adapter.

It imports nothing from the simulator on purpose. It has to keep working after
the simulator is split out into its own package.

Unlike `InProcessAdapter` it does not resolve principals. The label is recorded
verbatim on `.calls`; turning a label into an identity is what a real adapter
does, and a script has no identity to give.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping

from ..core.contracts import ChatResponse
from ..core.errors import ConfigError
from .base import AdapterLifecycle

#: (message, prior user messages in this session, oldest first) -> response.
Responder = Callable[[str, list[str]], ChatResponse]


class ScriptedAdapter(AdapterLifecycle):
    """`AgentAdapter` that answers from a script.

    Give it either `replies` (exact message -> response) or a `responder`
    (computes the response, and sees the session's earlier messages), never both.
    `raises` is checked first, so a message can be forced to fail whichever mode
    is in use. A message nothing matches gets `default`; with no `default` it
    raises, because inventing a reply would hand an evaluator something the
    script never said.

    A stored reply is handed out as a deep copy. Evaluators receive the response
    object itself, so one that mutated it would otherwise rewrite the script for
    every later call. A `responder` builds its reply per call and is trusted to
    return a fresh one.
    """

    def __init__(
        self,
        *,
        replies: Mapping[str, ChatResponse] | None = None,
        responder: Responder | None = None,
        default: ChatResponse | None = None,
        raises: Mapping[str, Exception] | None = None,
    ) -> None:
        super().__init__()
        if (replies is None) == (responder is None):
            raise ConfigError(
                "ScriptedAdapter needs exactly one of `replies` or `responder`."
            )
        # Copied so a test mutating its dicts mid-run cannot rewrite the script.
        self._replies = dict(replies) if replies is not None else None
        self._responder = responder
        self._default = default
        self._raises = dict(raises or {})

        #: Every call, in arrival order, as (message, session_id, principal).
        self.calls: list[tuple[str, str, str]] = []
        self._history: dict[str, list[str]] = {}
        # Guards `calls` and `_history`, which workers share. The responder runs
        # outside it so a slow one does not serialise the sessions.
        self._state_lock = threading.Lock()

    def send(
        self, message: str, *, session_id: str, principal: str = "default"
    ) -> ChatResponse:
        """Record the call, then answer it from the script.

        A forced failure still counts as a call, but not as a turn the session
        remembers: the system never answered it, so later messages do not see it.
        """
        self._require_open("send")
        with self._state_lock:
            self.calls.append((message, session_id, principal))
            prior = list(self._history.get(session_id, ()))

        if message in self._raises:
            raise self._raises[message]

        response = self._respond(message, prior)
        with self._state_lock:
            self._history.setdefault(session_id, []).append(message)
        return response

    def describe(self) -> dict[str, str]:
        """Provenance for the run report.

        Only the adapter name. A script has no model, retriever or prompt, and
        claiming a version for one would be provenance nobody can check.
        """
        self._require_open("describe")
        return {"adapter": "scripted"}

    def _respond(self, message: str, prior: list[str]) -> ChatResponse:
        if self._responder is not None:
            return self._responder(message, prior)
        assert self._replies is not None  # the constructor guarantees one mode
        if message in self._replies:
            return copy.deepcopy(self._replies[message])
        if self._default is not None:
            return copy.deepcopy(self._default)
        raise ConfigError(
            f"ScriptedAdapter has no reply for {message!r} and no default; "
            "script it in `replies` or pass `default`."
        )


__all__ = ["Responder", "ScriptedAdapter"]
