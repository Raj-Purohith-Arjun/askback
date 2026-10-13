"""The retrieval seam.

Swap BM25 for a vector store (FAISS, pgvector, Azure AI Search) by implementing
this one class. Agents and evaluators only ever see `RetrievedChunk`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from askback.core.contracts import RetrievedChunk


@dataclass(slots=True)
class Document:
    """A source document before chunking."""

    doc_id: str
    text: str
    title: str = ""
    source: str = ""
    #: Free-form labels. `tenant` and `audience` are honoured by access filtering.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tenant(self) -> str:
        return str(self.metadata.get("tenant", "default"))


class Retriever(ABC):
    """Contract for finding passages relevant to a query."""

    name: ClassVar[str] = "base"

    @abstractmethod
    def index(self, documents: Sequence[Document]) -> None:
        """Build (or rebuild) the search structure from `documents`."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        tenant: str = "default",
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """Return the best `top_k` passages the caller is allowed to see.

        `tenant` is enforced by the retriever, not the caller. Access control
        that lives in the agent is access control that gets forgotten.
        """

    def get_document(self, doc_id: str) -> Document | None:
        """Look up a source document. Used by evaluators to verify citations."""
        return None

    def __len__(self) -> int:  # pragma: no cover - trivial
        return 0
