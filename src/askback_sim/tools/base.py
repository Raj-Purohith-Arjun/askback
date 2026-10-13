"""The tool seam, plus the allow-list that makes tool misuse detectable.

Each agent declares which tools it may call. `ToolBox` enforces that list, so a
"tool misuse" finding in the safety report means a real policy breach, not a
missing check somewhere in agent code.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from askback.core.contracts import ToolCall
from askback.core.errors import ToolError
from askback.core.logging import get_logger

from ..registry import TOOLS

log = get_logger(__name__)


class Tool(ABC):
    """One callable capability exposed to agents."""

    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""
    #: JSON-schema-shaped parameter description. Used for docs today and for
    #: native tool-calling once a real model is wired in.
    parameters: ClassVar[dict[str, Any]] = {}
    #: True when the tool changes state. Read-only tools are safe to retry.
    side_effecting: ClassVar[bool] = False

    @abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Execute the tool. Raise `ToolError` for expected failures."""

    def spec(self) -> dict[str, Any]:
        """Vendor-neutral description, ready to convert to any tool-calling format."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "side_effecting": self.side_effecting,
        }


class ToolBox:
    """The tools one agent is allowed to use, with call recording built in."""

    def __init__(self, tools: Mapping[str, Tool], allowed: Iterable[str] | None = None) -> None:
        self._tools = dict(tools)
        # `None` means "everything available"; an explicit list is a hard limit.
        self._allowed = set(allowed) if allowed is not None else set(self._tools)

    def available(self) -> list[str]:
        return sorted(self._allowed & set(self._tools))

    def specs(self) -> list[dict[str, Any]]:
        return [self._tools[n].spec() for n in self.available()]

    def call(self, name: str, **kwargs: Any) -> ToolCall:
        """Invoke a tool, always returning a `ToolCall` record (never raising).

        Failures are data, not exceptions: a broken tool should degrade the
        answer and show up in the report, not abort the conversation.
        """
        started = time.perf_counter()

        def record(output: Any = None, *, ok: bool = True, error: str | None = None) -> ToolCall:
            return ToolCall(
                name=name,
                arguments=kwargs,
                output=output,
                ok=ok,
                error=error,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        if name not in self._tools:
            return record(ok=False, error=f"Unknown tool '{name}'.")
        if name not in self._allowed:
            log.warning("Blocked tool call outside allow-list: %s", name)
            return record(ok=False, error=f"Tool '{name}' is not approved for this agent.")

        try:
            return record(self._tools[name].run(**kwargs))
        except ToolError as exc:
            return record(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - a bad tool must not kill the turn
            log.exception("Tool %s raised", name)
            return record(ok=False, error=f"{type(exc).__name__}: {exc}")

    def scoped(self, allowed: Iterable[str] | None) -> ToolBox:
        """A narrower view of the same tools, for one agent."""
        return ToolBox(self._tools, allowed)


def build_toolbox(context: dict[str, Any] | None = None) -> ToolBox:
    """Instantiate every registered tool. `context` is passed to tools that want it."""
    context = context or {}
    tools: dict[str, Tool] = {}
    for name, cls in TOOLS:
        try:
            tools[name] = cls(**context) if getattr(cls, "wants_context", False) else cls()
        except Exception as exc:  # noqa: BLE001 - one bad tool must not break startup
            log.error("Skipping tool %r: %s", name, exc)
    return ToolBox(tools)
