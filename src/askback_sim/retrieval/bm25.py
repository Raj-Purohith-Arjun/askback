"""BM25 keyword retriever: no dependencies, no model download, fully deterministic.

BM25 is the standard lexical ranking function. It scores a passage by how often
the query terms appear in it (term frequency), discounted by how common those
terms are across the corpus (inverse document frequency), and normalised by
passage length so long passages do not win by default.

Good enough to build and evaluate the whole pipeline today; replace with a
vector retriever when embeddings are available, by implementing `Retriever`.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from askback.core.contracts import RetrievedChunk
from askback.core.text import tokenize

from ..registry import RETRIEVERS
from .base import Document, Retriever


@dataclass(slots=True)
class _Entry:
    """One indexed passage."""

    doc: Document
    chunk_id: str
    text: str
    tokens: Counter[str]
    length: int


@RETRIEVERS.register("bm25")
class BM25Retriever(Retriever):
    """Okapi BM25 over paragraph chunks."""

    name: ClassVar[str] = "bm25"

    def __init__(
        self,
        *,
        k1: float = 1.5,
        b: float = 0.75,
        max_chars: int = 900,
        overlap_chars: int = 120,
        min_score: float = 0.0,
        **_: Any,
    ) -> None:
        # k1 controls term-frequency saturation; b controls length normalisation.
        # The 1.5 / 0.75 pair is the standard default and a sane starting point.
        self.k1 = k1
        self.b = b
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.min_score = min_score

        self._entries: list[_Entry] = []
        self._postings: dict[str, list[int]] = defaultdict(list)
        self._idf: dict[str, float] = {}
        self._avg_length: float = 0.0
        self._by_doc_id: dict[str, Document] = {}

    # ------------------------------------------------------------- indexing #

    def index(self, documents: Sequence[Document]) -> None:
        from .corpus import chunk_documents

        self._entries.clear()
        self._postings.clear()
        self._by_doc_id = {d.doc_id: d for d in documents}

        for document, chunk_id, text in chunk_documents(
            documents, max_chars=self.max_chars, overlap_chars=self.overlap_chars
        ):
            # Titles are strong signal, so they are indexed with the body.
            tokens = Counter(tokenize(f"{document.title} {text}"))
            position = len(self._entries)
            self._entries.append(
                _Entry(document, chunk_id, text, tokens, sum(tokens.values()))
            )
            for term in tokens:
                self._postings[term].append(position)

        total = len(self._entries)
        self._avg_length = (
            sum(e.length for e in self._entries) / total if total else 0.0
        )
        self._idf = {
            term: math.log(1 + (total - len(posts) + 0.5) / (len(posts) + 0.5))
            for term, posts in self._postings.items()
        }

    # ------------------------------------------------------------ searching #

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        tenant: str = "default",
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        terms = tokenize(query)
        if not terms or not self._entries:
            return []

        scores: dict[int, float] = defaultdict(float)
        for term in terms:
            idf = self._idf.get(term)
            if idf is None:
                continue
            for position in self._postings[term]:
                entry = self._entries[position]
                freq = entry.tokens[term]
                norm = 1 - self.b + self.b * (entry.length / (self._avg_length or 1))
                scores[position] += idf * (freq * (self.k1 + 1)) / (
                    freq + self.k1 * norm
                )

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        results: list[RetrievedChunk] = []

        for position, score in ranked:
            entry = self._entries[position]
            if not self._allowed(entry.doc, tenant, filters):
                continue
            if score < self.min_score:
                break
            results.append(
                RetrievedChunk(
                    doc_id=entry.doc.doc_id,
                    chunk_id=entry.chunk_id,
                    text=entry.text,
                    score=round(score, 4),
                    title=entry.doc.title,
                    source=entry.doc.source,
                    metadata=dict(entry.doc.metadata),
                )
            )
            if len(results) >= top_k:
                break

        return results

    # ---------------------------------------------------------------- utils #

    @staticmethod
    def _allowed(
        document: Document, tenant: str, filters: dict[str, Any] | None
    ) -> bool:
        """Tenant isolation plus optional metadata filters.

        A document tagged `tenant: default` is shared; anything else is visible
        only to its own tenant. This is what the cross-user safety test probes.
        """
        doc_tenant = document.tenant
        if doc_tenant not in {"default", tenant}:
            return False
        for key, expected in (filters or {}).items():
            actual = document.metadata.get(key)
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def get_document(self, doc_id: str) -> Document | None:
        return self._by_doc_id.get(doc_id)

    def __len__(self) -> int:
        return len(self._entries)
