"""Specialized agents: the ones that actually answer.

`KnowledgeAgent` is deliberately generic and configured entirely from YAML, so
"policy agent" and "learning agent" are two config entries backed by one class.
Subclass it only when an agent needs genuinely different behaviour; see
`EscalationAgent` for what that looks like.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, ClassVar

from askback.core.contracts import (
    AgentReply,
    Citation,
    Message,
    ResponseStatus,
    RetrievedChunk,
    Role,
)
from askback.core.logging import get_logger
from askback.core.text import normalize, sentences

from ..llm.prompts import NO_ANSWER_TEMPLATE, build_system_prompt
from ..registry import AGENTS
from .base import Agent, Turn

log = get_logger(__name__)

#: Matches the [DOC-ID] markers the system prompt asks the model to emit.
_CITATION_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_.-]{0,40})\]")


@AGENTS.register("knowledge")
class KnowledgeAgent(Agent):
    """Retrieve, then answer strictly from what was retrieved.

    Config keys (all optional):
        filters         metadata filter passed to the retriever, e.g. {domain: policy}
        top_k           passages to retrieve
        max_sentences   answer length budget
        min_score       ignore passages weaker than this
        escalate_when_unanswered  raise a ticket instead of saying "I do not know"
    """

    name: ClassVar[str] = "knowledge"
    description: ClassVar[str] = "Answers questions from the internal knowledge base."

    def __init__(
        self,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
        max_sentences: int = 3,
        min_score: float = 0.0,
        escalate_when_unanswered: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.filters = filters or {}
        self.top_k = top_k
        self.max_sentences = max_sentences
        self.min_score = min_score
        self.escalate_when_unanswered = escalate_when_unanswered

    def handle(self, turn: Turn) -> AgentReply:
        tools = self.toolbox(turn)

        # Retrieval goes through the tool layer so it is recorded as a tool call
        # and subject to the same allow-list as everything else.
        call = tools.call(
            "knowledge_search",
            query=turn.message,
            top_k=self.top_k,
            tenant=turn.tenant,
        )
        chunks = self._retrieve(turn, call)

        if not chunks:
            return self._unanswered(turn, tools, [call])

        response = turn.llm.generate(
            self._build_messages(turn, chunks),
            context=chunks,
            max_sentences=self.max_sentences,
        )
        text = response.text.strip()

        if not text or normalize(text) == normalize(NO_ANSWER_TEMPLATE):
            return self._unanswered(turn, tools, [call], retrieved=chunks)

        return AgentReply(
            text=text,
            status=ResponseStatus.ANSWERED,
            citations=extract_citations(text, chunks),
            retrieved=chunks,
            tool_calls=[call],
            facts=sentences(text),
            metadata={"model": response.model, "agent": self.name},
        )

    # ---------------------------------------------------------------- helpers #

    def _retrieve(self, turn: Turn, call: Any) -> list[RetrievedChunk]:
        """Prefer the tool result; fall back to the retriever if the tool is barred.

        An agent without `knowledge_search` in its allow-list still needs an
        answer path, and failing closed to "no context" is the safe default.
        """
        if call.ok and isinstance(call.output, list):
            return turn.retriever.search(
                turn.message,
                top_k=self.top_k,
                tenant=turn.tenant,
                filters=self.filters or None,
            )
        log.debug("knowledge_search unavailable for %s: %s", self.name, call.error)
        return []

    def _build_messages(self, turn: Turn, chunks: Sequence[RetrievedChunk]) -> list[Message]:
        system = build_system_prompt(self.name, max_sentences=self.max_sentences)
        if self.description:
            system = f"{system}\n\nYour remit: {self.description}"
        return [
            Message(Role.SYSTEM, system),
            *turn.history[-6:],  # short window keeps prompts small and predictable
            Message(Role.USER, turn.message),
        ]

    def _unanswered(
        self,
        turn: Turn,
        tools: Any,
        calls: list[Any],
        retrieved: Sequence[RetrievedChunk] = (),
    ) -> AgentReply:
        """No grounding found. Say so honestly, or escalate if configured to."""
        if self.escalate_when_unanswered and "create_ticket" in tools.available():
            ticket = tools.call(
                "create_ticket", summary=turn.message[:200], category=self.name
            )
            calls.append(ticket)
            if ticket.ok:
                ref = ticket.output["ticket_id"]
                return AgentReply(
                    text=(
                        f"{NO_ANSWER_TEMPLATE} I have raised ticket {ref} so a "
                        "specialist can follow up."
                    ),
                    status=ResponseStatus.ESCALATED,
                    retrieved=list(retrieved),
                    tool_calls=calls,
                    metadata={"ticket_id": ref, "agent": self.name},
                )

        return AgentReply(
            text=NO_ANSWER_TEMPLATE,
            status=ResponseStatus.NO_ANSWER,
            retrieved=list(retrieved),
            tool_calls=calls,
            metadata={"agent": self.name},
        )


@AGENTS.register("smalltalk")
class SmallTalkAgent(Agent):
    """Greetings, thanks and goodbyes. No retrieval, no tools, no citations."""

    name: ClassVar[str] = "smalltalk"
    description: ClassVar[str] = "Handles greetings and conversational filler."

    DEFAULT_REPLIES: ClassVar[dict[str, str]] = {
        "greeting": (
            "Hello. I can help with company policy, learning and development, and "
            "IT support questions. What do you need?"
        ),
        "thanks": "Happy to help. Anything else?",
        "goodbye": "Goodbye. Reach out any time.",
        "default": "I am here to help with policy, learning and IT support questions.",
    }

    def __init__(self, *, replies: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.replies = {**self.DEFAULT_REPLIES, **(replies or {})}

    def handle(self, turn: Turn) -> AgentReply:
        text = self.replies.get(turn.intent.name, self.replies["default"])
        return AgentReply(
            text=text, status=ResponseStatus.ANSWERED, metadata={"agent": self.name}
        )


@AGENTS.register("fallback")
class FallbackAgent(Agent):
    """Nothing matched. Say what this assistant can do instead of guessing."""

    name: ClassVar[str] = "fallback"
    description: ClassVar[str] = "Handles messages no specialized agent claims."

    def __init__(self, *, capabilities: Sequence[str] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.capabilities = list(capabilities) or [
            "company policy (leave, expenses, travel, security)",
            "learning and certification programmes",
            "IT support and access requests",
        ]

    def handle(self, turn: Turn) -> AgentReply:
        options = "; ".join(self.capabilities)
        return AgentReply(
            text=(
                f"{NO_ANSWER_TEMPLATE} I can help with: {options}. "
                "Could you rephrase your question in one of those areas?"
            ),
            status=ResponseStatus.NO_ANSWER,
            metadata={"agent": self.name},
        )


@AGENTS.register("escalation")
class EscalationAgent(Agent):
    """Hands the request to a human by raising a ticket.

    Kept as its own class because its terminal status is ESCALATED, not
    ANSWERED, and because it always performs a side effect.
    """

    name: ClassVar[str] = "escalation"
    description: ClassVar[str] = "Routes requests that need a human to a ticket queue."

    def handle(self, turn: Turn) -> AgentReply:
        call = self.toolbox(turn).call(
            "create_ticket",
            summary=turn.message[:200],
            category=turn.intent.name,
            priority=str(self.options.get("priority", "normal")),
        )
        if not call.ok:
            return AgentReply(
                text="I could not raise a ticket just now. Please contact the service desk.",
                status=ResponseStatus.ERROR,
                tool_calls=[call],
                metadata={"agent": self.name, "error": call.error},
            )

        ref = call.output["ticket_id"]
        return AgentReply(
            text=f"I have raised ticket {ref}. A specialist will follow up with you.",
            status=ResponseStatus.ESCALATED,
            tool_calls=[call],
            metadata={"agent": self.name, "ticket_id": ref},
        )


def extract_citations(text: str, chunks: Sequence[RetrievedChunk]) -> list[Citation]:
    """Turn [DOC-ID] markers in the reply into `Citation` records.

    Only markers that match a retrieved chunk become citations; an invented id
    is silently dropped here and caught by the RAG evaluator's support check.
    """
    by_doc = {c.doc_id: c for c in chunks}
    seen: set[str] = set()
    citations: list[Citation] = []

    for doc_id in _CITATION_RE.findall(text):
        chunk = by_doc.get(doc_id)
        if chunk is None or doc_id in seen:
            continue
        seen.add(doc_id)
        citations.append(
            Citation(
                doc_id=chunk.doc_id,
                chunk_id=chunk.chunk_id,
                snippet=chunk.text[:240],
                title=chunk.title,
                source=chunk.source,
            )
        )
    return citations
