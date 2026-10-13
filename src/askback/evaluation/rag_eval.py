"""RAG evaluator: did retrieval find the right evidence, and is the answer built on it?

Four measures, deliberately separated so a failure points at one stage:

  retrieval_recall   the expected documents were found at all
  retrieval_mrr      they were found near the top, not buried at rank 5
  citation_support   every citation points at something actually retrieved
  groundedness       every sentence of the reply is supported by retrieved text
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import ClassVar

from ..core.contracts import ChatResponse, ResponseStatus, RetrievedChunk, Severity
from ..core.registry import EVALUATORS
from ..core.text import coverage, normalize, sentences, tokenize
from ..datasets.schema import TestCase, Turn
from .base import Check, EvalResult, Evaluator

#: Citation markers are scaffolding, not claims; strip them before grading text.
_CITATION_MARKER = re.compile(r"\[[A-Za-z][A-Za-z0-9_.-]{0,40}\]")


@EVALUATORS.register("rag")
class RagEvaluator(Evaluator):
    """Scores retrieval quality and how well the answer is grounded in it."""

    name: ClassVar[str] = "rag"
    description: ClassVar[str] = "Expected documents, retrieval rank, citation support, groundedness."

    def __init__(
        self,
        *,
        groundedness_threshold: float = 0.6,
        sentence_support_threshold: float = 0.55,
        **options: object,
    ) -> None:
        super().__init__(**options)
        # A sentence counts as supported when this share of its content words
        # appears in the retrieved text.
        self.sentence_support_threshold = sentence_support_threshold
        # The reply passes when this share of its sentences are supported.
        self.groundedness_threshold = groundedness_threshold

    def evaluate(self, case: TestCase, turn: Turn, response: ChatResponse) -> EvalResult:
        expect = turn.expect
        # Nothing to say about a turn that never retrieved and never claimed to.
        if not expect.expected_docs and not response.retrieved:
            return EvalResult(evaluator=self.name, score=1.0, passed=True, skipped=True)

        checks: list[Check] = []
        retrieved_ids = response.retrieved_doc_ids

        if expect.expected_docs:
            found = [d for d in expect.expected_docs if d in retrieved_ids]
            recall = len(found) / len(expect.expected_docs)
            checks.append(
                self.check(
                    "retrieval_recall",
                    recall == 1.0,
                    score=recall,
                    detail=f"Found {len(found)}/{len(expect.expected_docs)} expected documents. "
                    f"Retrieved: {retrieved_ids}.",
                    severity=Severity.CRITICAL,
                    expected=expect.expected_docs,
                    actual=retrieved_ids,
                )
            )

            rank = next(
                (i + 1 for i, doc in enumerate(retrieved_ids) if doc in expect.expected_docs),
                0,
            )
            mrr = 1.0 / rank if rank else 0.0
            checks.append(
                self.check(
                    "retrieval_mrr",
                    mrr >= 0.5,
                    score=mrr,
                    detail=f"First expected document at rank {rank or 'not found'}.",
                    expected="rank 1 or 2",
                    actual=rank,
                )
            )

        if response.citations:
            unsupported = unsupported_citations(response)
            supported = len(response.citations) - len(unsupported)
            checks.append(
                self.check(
                    "citation_support",
                    not unsupported,
                    score=supported / len(response.citations),
                    detail=f"{supported}/{len(response.citations)} citations trace to retrieved text."
                    + (f" Unsupported: {unsupported}." if unsupported else ""),
                    severity=Severity.CRITICAL,
                    expected="every citation traceable",
                    actual=unsupported,
                )
            )

        # Groundedness only applies to an actual answer. A refusal or an honest
        # "I do not have that information" is a fixed template, not a claim about
        # the corpus; grading it here would fail the behaviour we want most.
        if (
            response.status is ResponseStatus.ANSWERED
            and response.retrieved
            and response.reply.strip()
        ):
            score, unsupported = groundedness(
                response.reply, response.retrieved, self.sentence_support_threshold
            )
            checks.append(
                self.check(
                    "groundedness",
                    score >= self.groundedness_threshold,
                    score=score,
                    detail=f"{score:.0%} of sentences supported by retrieved text."
                    + (f" Unsupported: {unsupported}." if unsupported else ""),
                    severity=Severity.CRITICAL,
                    expected=f">= {self.groundedness_threshold:.0%}",
                    actual=round(score, 3),
                )
            )

        return self.build(checks)


def unsupported_citations(response: ChatResponse) -> list[str]:
    """Citations whose snippet does not appear in the text that was retrieved."""
    by_doc: dict[str, str] = {}
    for chunk in response.retrieved:
        by_doc[chunk.doc_id] = by_doc.get(chunk.doc_id, "") + " " + chunk.text

    bad: list[str] = []
    for citation in response.citations:
        source = by_doc.get(citation.doc_id)
        if source is None:
            bad.append(f"{citation.doc_id} (not retrieved)")
        elif citation.snippet and normalize(citation.snippet) not in normalize(source):
            bad.append(f"{citation.doc_id} (snippet not in source)")
    return bad


def groundedness(
    reply: str, chunks: Sequence[RetrievedChunk], sentence_threshold: float
) -> tuple[float, list[str]]:
    """Share of reply sentences supported by the retrieved text.

    Token-overlap based, which is a proxy rather than entailment: it reliably
    catches invented content, and can be fooled by a paraphrase that reuses the
    source vocabulary. Swap in an NLI model here when one is available.
    """
    source_tokens = set()
    for chunk in chunks:
        source_tokens.update(tokenize(chunk.text))

    claims = [s for s in sentences(_CITATION_MARKER.sub("", reply)) if len(s) > 15]
    if not claims:
        return 1.0, []

    unsupported = [
        s for s in claims if coverage(tokenize(s), source_tokens) < sentence_threshold
    ]
    return (len(claims) - len(unsupported)) / len(claims), [s[:80] for s in unsupported]
