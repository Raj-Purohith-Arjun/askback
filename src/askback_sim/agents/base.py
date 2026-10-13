"""The agent seam.

An agent receives a `Turn` (the request plus every runtime service it is allowed
to touch) and returns an `AgentReply`. It never reaches for globals, which is
what makes agents unit-testable in isolation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from askback.core.contracts import AgentReply, ChatRequest, Intent, Message

from ..llm.base import LLMProvider
from ..retrieval.base import Retriever
from ..tools.base import ToolBox


@dataclass(slots=True)
class Turn:
    """Everything an agent may use to answer one message.

    Explicit dependency injection: swap any field for a fake in a test and the
    agent behaves identically.
    """

    request: ChatRequest
    llm: LLMProvider
    retriever: Retriever
    tools: ToolBox
    history: list[Message] = field(default_factory=list)
    intent: Intent = field(default_factory=lambda: Intent(name="unknown"))
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> str:
        return self.request.message

    @property
    def tenant(self) -> str:
        return self.request.tenant_id


class Agent(ABC):
    """Base class for every agent, specialized or otherwise."""

    #: Registry key; also the value asserted by the `expected_route` rule check.
    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""

    def __init__(
        self,
        *,
        name: str | None = None,
        description: str = "",
        tools: Sequence[str] | None = None,
        **options: Any,
    ) -> None:
        # Instance name overrides the class name so one class can back several
        # configured agents (policy_agent and learning_agent share KnowledgeAgent).
        self.name = name or type(self).name  # type: ignore[misc]  # shadows the class default, as above
        self.description = description or type(self).description  # type: ignore[misc]  # likewise
        self.allowed_tools: list[str] | None = list(tools) if tools is not None else None
        self.options = options

    @abstractmethod
    def handle(self, turn: Turn) -> AgentReply:
        """Produce a reply for `turn`."""

    def toolbox(self, turn: Turn) -> ToolBox:
        """The turn's tools, narrowed to this agent's allow-list."""
        return turn.tools.scoped(self.allowed_tools)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"
