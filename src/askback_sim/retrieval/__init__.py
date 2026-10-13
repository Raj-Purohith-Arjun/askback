"""Retrieval: corpus loading, chunking and the pluggable Retriever seam."""

from __future__ import annotations

from typing import Any

from askback.core.config import resolve_path

from ..registry import RETRIEVERS
from .base import Document, Retriever
from .bm25 import BM25Retriever
from .corpus import chunk_documents, corpus_stats, load_documents


def build_retriever(config: dict[str, Any] | None = None) -> Retriever:
    """Create and populate the retriever named in config.

    Example config section::

        retrieval:
          backend: bm25
          corpus_path: examples/hr/corpus
          top_k: 5
    """
    settings = dict(config or {})
    backend = str(settings.pop("backend", "bm25"))
    corpus_path = settings.pop("corpus_path", "examples/hr/corpus")
    settings.pop("top_k", None)  # consumed by the engine, not the retriever

    retriever: Retriever = RETRIEVERS.get(backend)(**settings)
    retriever.index(load_documents(resolve_path(corpus_path)))
    return retriever


__all__ = [
    "BM25Retriever",
    "Document",
    "Retriever",
    "build_retriever",
    "chunk_documents",
    "corpus_stats",
    "load_documents",
]
