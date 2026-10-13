"""Holds the live engine so the dashboard can rebuild it without a restart.

The engine is expensive to build (it indexes the whole corpus), so the service
builds one at startup and shares it. The corpus and the intent catalogue are
edited as files on disk, which means something has to swap the engine
afterwards; that is this.

Swapping is atomic from a request's point of view: a new engine is built first
and only assigned once it succeeds, so a broken edit leaves the running engine
untouched and the error goes back to whoever asked for the reload.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from ..adapters import AgentAdapter
from ..adapters.factory import build_adapter
from ..core.config import AppConfig
from ..core.logging import get_logger

if TYPE_CHECKING:
    from askback_sim.engine import ConversationEngine

log = get_logger(__name__)


def _build_engine(config: AppConfig) -> ConversationEngine:
    """Build the simulator's engine, importing it only when one is built.

    The adapter factory will not import the simulator, so this is where the
    server - which is allowed to know it exists - does, and hands the engine on.
    """
    from askback_sim.engine import ConversationEngine

    return ConversationEngine.from_config(config)


class EngineHolder:
    """One mutable reference to the current engine."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._engine = _build_engine(config)
        self._adapter = build_adapter(config, engine=self._engine)
        self.reload_count = 0

    @property
    def engine(self) -> ConversationEngine:
        """The engine to serve this request with."""
        return self._engine

    @property
    def adapter(self) -> AgentAdapter:
        """The current engine as the evaluation runner reaches it.

        Shared by every run: adapters count their entries, so concurrent runs
        can each hold it open.
        """
        return self._adapter

    def reload(self) -> ConversationEngine:
        """Rebuild from the files on disk.

        Raises whatever the build raises (bad YAML, unreadable corpus) *before*
        replacing the running engine, so a failed reload is a no-op.
        """
        with self._lock:
            rebuilt = _build_engine(self.config)
            # Built before either is assigned: a bad `agent:` section fails the
            # reload like any other bad edit, leaving the running pair alone.
            adapter = build_adapter(self.config, engine=rebuilt)
            self._engine = rebuilt
            self._adapter = adapter
            self.reload_count += 1
        log.info("Engine reloaded (%d chunks indexed)", len(rebuilt.retriever))
        return rebuilt

    def describe(self) -> dict[str, Any]:
        return {**self._engine.describe(), "reload_count": self.reload_count}
