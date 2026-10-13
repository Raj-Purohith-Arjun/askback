"""Conversation memory.

In-process by default so the framework has no infrastructure dependency. Swap
in Redis or a database by implementing `ConversationStore`; the engine only
uses the three methods below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Iterable

from askback.core.contracts import Message


class ConversationStore(ABC):
    """Where conversation history lives between turns."""

    @abstractmethod
    def history(self, conversation_id: str) -> list[Message]:
        """Messages for a conversation, oldest first."""

    @abstractmethod
    def append(self, conversation_id: str, messages: Iterable[Message]) -> None:
        """Add messages to a conversation."""

    @abstractmethod
    def reset(self, conversation_id: str | None = None) -> None:
        """Clear one conversation, or all of them when `conversation_id` is None."""


class InMemoryConversationStore(ConversationStore):
    """Bounded, thread-safe-enough for a single process. Not for multi-worker servers.

    `max_messages` caps each conversation so a long-running process cannot grow
    without limit; the oldest turns fall off first.
    """

    def __init__(self, max_messages: int = 40) -> None:
        self.max_messages = max_messages
        self._store: dict[str, deque[Message]] = defaultdict(
            lambda: deque(maxlen=max_messages)
        )

    def history(self, conversation_id: str) -> list[Message]:
        return list(self._store[conversation_id])

    def append(self, conversation_id: str, messages: Iterable[Message]) -> None:
        self._store[conversation_id].extend(messages)

    def reset(self, conversation_id: str | None = None) -> None:
        if conversation_id is None:
            self._store.clear()
        else:
            self._store.pop(conversation_id, None)

    def __len__(self) -> int:
        return len(self._store)
