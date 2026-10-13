"""File-aware read access to the JSONL corpus, for the dashboard's Knowledge panel.

`askback_sim.retrieval.corpus` loads documents for *searching* and deliberately
forgets which file each came from. The panel shows that, so rather than
complicate the retrieval path with bookkeeping it does not need, this module
reads the files directly.

There is no write path, on purpose. The corpus is the thing every answer depends
on, and it is edited as files, under review, not from a web panel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import DatasetError


@dataclass(slots=True)
class CorpusEntry:
    """One document plus the file it lives in."""

    doc_id: str
    record: dict[str, Any]
    file: Path

    def summary(self) -> dict[str, Any]:
        """The compact form the dashboard list view needs."""
        return {
            "doc_id": self.doc_id,
            "title": str(self.record.get("title", "")),
            "source": str(self.record.get("source", "")),
            "metadata": dict(self.record.get("metadata") or {}),
            "characters": len(str(self.record.get("text", ""))),
            "file": self.file.name,
        }


class CorpusStore:
    """The .jsonl files in the corpus directory, read as documents."""

    def __init__(self, corpus_path: str | Path) -> None:
        self.root = Path(corpus_path)

    def files(self) -> list[Path]:
        if self.root.is_file():
            return [self.root]
        return sorted(self.root.glob("*.jsonl")) if self.root.is_dir() else []

    def load(self) -> list[CorpusEntry]:
        """Every document, in file order. Raises on malformed or duplicated ids."""
        entries: list[CorpusEntry] = []
        seen: set[str] = set()

        for file in self.files():
            for line_no, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("//"):
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise DatasetError(f"{file.name}:{line_no} is not valid JSON: {exc}") from exc

                doc_id = str(record.get("doc_id") or "").strip()
                if not doc_id:
                    raise DatasetError(f"{file.name}:{line_no} has no doc_id.")
                if doc_id in seen:
                    raise DatasetError(f"{file.name}:{line_no} duplicates doc_id {doc_id!r}.")
                seen.add(doc_id)
                entries.append(CorpusEntry(doc_id=doc_id, record=record, file=file))

        return entries

    def list(self) -> list[dict[str, Any]]:
        return [entry.summary() for entry in self.load()]

    def get(self, doc_id: str) -> dict[str, Any] | None:
        for entry in self.load():
            if entry.doc_id == doc_id:
                return {**entry.record, "_file": entry.file.name}
        return None
