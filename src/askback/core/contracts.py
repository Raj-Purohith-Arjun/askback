"""Stable data contracts shared by every layer of the framework.

These types are the *fixed core*: agents, retrievers, tools, LLM providers and
evaluators are all swappable, but they all speak these structures. Treat this
module as a public API — additive changes are fine, renames are breaking.

Stdlib only, on purpose: the core must import cleanly anywhere.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Role(str, Enum):
    """Author of a conversation message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ResponseStatus(str, Enum):
    """Terminal outcome of a turn. Evaluators assert on this."""

    ANSWERED = "answered"          # Question resolved with an answer.
    NO_ANSWER = "no_answer"        # Honest "I don't know" — no grounding found.
    REFUSED = "refused"            # Blocked by policy or safety guard.
    ESCALATED = "escalated"        # Handed off to a human / another queue.
    ERROR = "error"                # Something broke; see ChatResponse.error.
    # An adapter could not map the agent's payload to any status above. Not a
    # default: nothing may assume "answered" about a turn it cannot read.
    UNKNOWN = "unknown"


class Severity(str, Enum):
    """How much a failed check matters."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# --------------------------------------------------------------------------- #
# Conversation primitives
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Message:
    """One entry in a conversation transcript."""

    role: Role
    content: str
    name: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role.value, "content": self.content, "name": self.name}


@dataclass(slots=True)
class Intent:
    """Result of intent classification, produced by the FrontLine agent."""

    name: str
    confidence: float = 0.0
    rationale: str = ""
    scores: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievedChunk:
    """A passage returned by a Retriever, with provenance for citation checks."""

    doc_id: str
    chunk_id: str
    text: str
    score: float = 0.0
    title: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Citation:
    """A claim-to-source link surfaced in the reply."""

    doc_id: str
    chunk_id: str = ""
    snippet: str = ""
    title: str = ""
    source: str = ""


@dataclass(slots=True)
class ToolCall:
    """Record of one tool invocation. Safety checks assert on `name`."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    ok: bool = True
    error: str | None = None
    latency_ms: float = 0.0


@dataclass(slots=True)
class SafetyReport:
    """Verdict from the safety guards, attached to every response."""

    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    injection_detected: bool = False
    redactions: list[str] = field(default_factory=list)
    risk_score: float = 0.0


# --------------------------------------------------------------------------- #
# Request / response envelope
# --------------------------------------------------------------------------- #


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(slots=True)
class ChatRequest:
    """Everything the engine needs to answer one user turn."""

    message: str
    conversation_id: str = field(default_factory=lambda: _new_id("conv"))
    user_id: str = "anonymous"
    # `tenant_id` scopes retrieval so one customer can never read another's data.
    tenant_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentReply:
    """What a specialized agent returns. The engine wraps this in a ChatResponse."""

    text: str
    status: ResponseStatus = ResponseStatus.ANSWERED
    citations: list[Citation] = field(default_factory=list)
    retrieved: list[RetrievedChunk] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatResponse:
    """The single object the API, the CLI and every evaluator consume."""

    reply: str
    conversation_id: str
    route: str = "unknown"
    status: ResponseStatus = ResponseStatus.ANSWERED
    intent: Intent = field(default_factory=lambda: Intent(name="unknown"))
    citations: list[Citation] = field(default_factory=list)
    retrieved: list[RetrievedChunk] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    safety: SafetyReport = field(default_factory=SafetyReport)
    model: str = ""
    latency_ms: float = 0.0
    trace_id: str = field(default_factory=lambda: _new_id("trace"))
    error: str | None = None

    # Convenience views used repeatedly by evaluators and reports. ------------ #

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tool_calls]

    @property
    def cited_doc_ids(self) -> list[str]:
        return [c.doc_id for c in self.citations]

    @property
    def retrieved_doc_ids(self) -> list[str]:
        return [c.doc_id for c in self.retrieved]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data
