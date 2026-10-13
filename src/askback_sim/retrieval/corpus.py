"""Load and chunk the knowledge base.

Corpus format is JSON Lines, one document per line:

    {"doc_id": "HR-001", "title": "Annual Leave", "text": "...",
     "source": "hr_handbook.pdf", "metadata": {"tenant": "default", "topic": "leave"}}

JSONL is chosen because it diffs cleanly in git and streams for large corpora.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from askback.core.errors import DatasetError
from askback.core.logging import get_logger

from .base import Document

log = get_logger(__name__)


def load_documents(path: str | Path) -> list[Document]:
    """Read a .jsonl corpus file (or every .jsonl file in a directory)."""
    target = Path(path)
    files = sorted(target.glob("*.jsonl")) if target.is_dir() else [target]
    if not files or not all(f.exists() for f in files):
        raise DatasetError(f"No corpus found at {target}")

    documents: list[Document] = []
    seen: set[str] = set()

    for file in files:
        for line_no, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{file}:{line_no} is not valid JSON: {exc}") from exc

            doc_id = str(record.get("doc_id") or "").strip()
            text = str(record.get("text") or "").strip()
            if not doc_id or not text:
                raise DatasetError(f"{file}:{line_no} needs both 'doc_id' and 'text'.")
            if doc_id in seen:
                raise DatasetError(f"{file}:{line_no} duplicates doc_id '{doc_id}'.")
            seen.add(doc_id)

            documents.append(
                Document(
                    doc_id=doc_id,
                    text=text,
                    title=str(record.get("title") or doc_id),
                    source=str(record.get("source") or file.name),
                    metadata=dict(record.get("metadata") or {}),
                )
            )

    log.info("Loaded %d documents from %s", len(documents), target)
    return documents


def chunk_document(
    document: Document, *, max_chars: int = 900, overlap_chars: int = 120
) -> Iterator[tuple[str, str]]:
    """Split one document into (chunk_id, text) pairs on paragraph boundaries.

    Paragraph-aware so a chunk rarely cuts a sentence in half, which keeps
    citation snippets readable and groundedness scores honest.
    """
    paragraphs = [p.strip() for p in document.text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [document.text]

    buffer = ""
    index = 0
    for paragraph in paragraphs:
        if buffer and len(buffer) + len(paragraph) + 2 > max_chars:
            yield f"{document.doc_id}#{index}", buffer
            index += 1
            buffer = (buffer[-overlap_chars:] + "\n\n" + paragraph) if overlap_chars else paragraph
        else:
            buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph

    if buffer.strip():
        yield f"{document.doc_id}#{index}", buffer.strip()


def chunk_documents(
    documents: Iterable[Document], *, max_chars: int = 900, overlap_chars: int = 120
) -> list[tuple[Document, str, str]]:
    """Chunk a whole corpus into (document, chunk_id, text) triples."""
    chunks: list[tuple[Document, str, str]] = []
    for document in documents:
        for chunk_id, text in chunk_document(
            document, max_chars=max_chars, overlap_chars=overlap_chars
        ):
            chunks.append((document, chunk_id, text))
    return chunks


def corpus_stats(documents: Sequence[Document]) -> dict[str, int]:
    """Quick sanity numbers for the CLI and reports."""
    return {
        "documents": len(documents),
        "characters": sum(len(d.text) for d in documents),
        "tenants": len({d.tenant for d in documents}),
    }
