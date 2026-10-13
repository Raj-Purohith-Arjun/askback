"""Retrieval: BM25 ranking, chunking and tenant isolation."""

from __future__ import annotations

import pytest

from askback.core.errors import DatasetError
from askback_sim.retrieval.base import Document
from askback_sim.retrieval.bm25 import BM25Retriever
from askback_sim.retrieval.corpus import chunk_document, load_documents


class TestBM25:
    def test_ranks_the_relevant_document_first(self, retriever: BM25Retriever) -> None:
        hits = retriever.search("annual leave days", top_k=3)
        assert hits, "expected at least one hit"
        assert hits[0].doc_id == "POL-1"
        assert hits[0].score > 0

    def test_returns_nothing_for_unknown_vocabulary(self, retriever: BM25Retriever) -> None:
        assert retriever.search("xylophone quokka", top_k=3) == []

    def test_respects_top_k(self, retriever: BM25Retriever) -> None:
        assert len(retriever.search("leave expense claims", top_k=1)) == 1

    def test_metadata_filters_narrow_results(self, retriever: BM25Retriever) -> None:
        assert retriever.search("leave", filters={"domain": "learning"}) == []
        assert retriever.search("leave", filters={"domain": "policy"})


class TestTenantIsolation:
    def test_default_tenant_cannot_see_another_tenants_document(
        self, retriever: BM25Retriever
    ) -> None:
        hits = retriever.search("Acme grade 5 engineers paid", tenant="default")
        assert "SECRET-1" not in [h.doc_id for h in hits]

    def test_owning_tenant_can_see_its_own_document(self, retriever: BM25Retriever) -> None:
        hits = retriever.search("Acme grade 5 engineers paid", tenant="acme")
        assert "SECRET-1" in [h.doc_id for h in hits]

    def test_shared_documents_are_visible_to_every_tenant(
        self, retriever: BM25Retriever
    ) -> None:
        assert retriever.search("annual leave", tenant="acme")


class TestCorpus:
    def test_chunk_ids_are_stable_and_prefixed(self) -> None:
        document = Document(doc_id="D1", text="Para one.\n\nPara two.")
        chunks = list(chunk_document(document, max_chars=12, overlap_chars=0))
        assert [chunk_id for chunk_id, _ in chunks] == ["D1#0", "D1#1"]

    def test_short_document_is_one_chunk(self) -> None:
        document = Document(doc_id="D1", text="Only a short line.")
        assert len(list(chunk_document(document))) == 1

    def test_duplicate_doc_ids_are_rejected(self, tmp_path) -> None:
        path = tmp_path / "corpus.jsonl"
        path.write_text(
            '{"doc_id": "A", "text": "one"}\n{"doc_id": "A", "text": "two"}\n',
            encoding="utf-8",
        )
        with pytest.raises(DatasetError, match="duplicate"):
            load_documents(path)

    def test_missing_required_field_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "corpus.jsonl"
        path.write_text('{"doc_id": "A"}\n', encoding="utf-8")
        with pytest.raises(DatasetError, match="doc_id"):
            load_documents(path)
