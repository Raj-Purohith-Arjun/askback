"""The in-process adapter: the simulator, reached through the adapter seam.

This wraps an engine living in this process - the simulator's
`ConversationEngine` - so the harness can drive it the same way it will drive a
deployed agent. The adapter knows the engine only as a shape (`InProcessEngine`)
and imports nothing from the simulator: whoever has one passes it in. Nothing
here is transport:
there is no client to open and no payload to map, because the engine already
speaks `ChatResponse`. What the adapter does own is identity - turning the
principal label a suite names into the user and tenant the engine scopes
retrieval by.

It stands in for the real system until the HTTP adapter exists. Numbers produced
through it are results about the simulator, and `describe()` says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..core.contracts import ChatRequest, ChatResponse
from ..core.errors import ConfigError
from .base import AdapterLifecycle


class InProcessEngine(Protocol):
    """What this adapter needs of an engine. The simulator's fits; so can a fake."""

    def chat(self, request: ChatRequest) -> ChatResponse:
        """Answer one turn."""
        ...

    def provenance(self) -> dict[str, str]:
        """Which engine this is, for the run report. Strings, stable keys."""
        ...


#: principal label -> (user_id, tenant_id). "default" is the Protocol's own
#: default for `send`, so it has to resolve; it is the same identity suites have
#: always run as.
DEFAULT_PRINCIPALS: Mapping[str, tuple[str, str]] = {
    "default": ("test_user", "default"),
    "test_user": ("test_user", "default"),
    "outsider": ("outsider", "default"),
    "acme_user": ("acme_user", "acme"),
}


class InProcessAdapter(AdapterLifecycle):
    """`AgentAdapter` over a `ConversationEngine` living in this process."""

    def __init__(
        self,
        engine: InProcessEngine,
        principals: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._principals = _validated(
            DEFAULT_PRINCIPALS if principals is None else principals
        )

    def send(
        self, message: str, *, session_id: str, principal: str = "default"
    ) -> ChatResponse:
        """Send one turn to the engine and return its response untouched.

        No lock: the runner's thread pool already calls `engine.chat` from
        several workers at once, so the adapter shares nothing that was not
        shared before it existed.
        """
        self._require_open("send")
        user_id, tenant_id = self._resolve(principal)
        return self._engine.chat(
            ChatRequest(
                message=message,
                conversation_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
        )

    def describe(self) -> dict[str, str]:
        """Provenance for the run report.

        The engine states its own keys - the ones the runner has always
        recorded, so a baseline taken before the adapter existed still lines up.
        `adapter` is what stops a simulator baseline being read as comparable to
        a run against a deployed agent, so the engine cannot overwrite it.
        """
        self._require_open("describe")
        info = {"adapter": "in_process", **self._engine.provenance()}
        info["adapter"] = "in_process"
        return info

    def _resolve(self, principal: str) -> tuple[str, str]:
        """Map a principal label to (user_id, tenant_id), or refuse.

        An unknown label never falls back to a default identity: an isolation
        case that quietly ran as the wrong user would pass for the wrong reason.
        """
        try:
            return self._principals[principal]
        except KeyError:
            raise ConfigError(
                f"unknown principal {principal!r}; this adapter knows: "
                f"{sorted(self._principals)}"
            ) from None


def _validated(principals: Mapping[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """Copy the mapping, refusing any value that is not a (user_id, tenant_id) pair.

    Checked when the adapter is built so a malformed entry stops the run before
    it starts, rather than failing the first case sent as that principal. The
    copy means a caller mutating its mapping mid-run cannot change who a later
    turn is sent as.
    """
    for label, identity in principals.items():
        # `tuple` is checked by type: a two-character string would otherwise
        # unpack into a user and a tenant without complaint.
        if not (
            isinstance(identity, tuple)
            and len(identity) == 2
            and all(isinstance(part, str) for part in identity)
        ):
            raise ConfigError(
                f"principal {label!r} must map to a (user_id, tenant_id) tuple of "
                f"two strings, got {identity!r}"
            )
    return dict(principals)


__all__ = ["DEFAULT_PRINCIPALS", "InProcessAdapter", "InProcessEngine"]
