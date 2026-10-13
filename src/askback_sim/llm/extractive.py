"""Offline default provider: builds answers by extracting from retrieved text.

Why this exists: the project has no LLM wired up yet, but the pipeline, the
datasets and all five evaluators still need to be exercised end to end. This
provider is deterministic (same input, same output), needs no network and
cannot hallucinate, because every sentence it emits is copied verbatim from a
source document.

Swap it for a real model by setting `llm.provider` in the config file.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from askback.core.contracts import Message, RetrievedChunk, Role
from askback.core.text import cosine, tokenize
from askback.core.text import sentences as split_sentences
from askback.providers.base import LLM_PROVIDERS

from .base import LLMProvider, LLMResponse
from .prompts import NO_ANSWER_TEMPLATE

#: Questions that ask for a quantity. Their answer almost always contains a
#: figure, and without this cue the shortest loosely-matching sentence wins.
_QUANTITY_QUESTION = re.compile(
    r"\b(?:how (?:many|much|long|often|soon|quickly|frequently)"
    r"|(?:within|after|by) how (?:many|much|long)"
    r"|what (?:is|are) the (?:limit|rate|budget|amount|deadline|threshold|"
    r"maximum|minimum|cost|fee|duration|allowance|entitlement|notice period))\b",
    re.IGNORECASE,
)
_HAS_DIGIT = re.compile(r"\d")


@dataclass(slots=True)
class _Candidate:
    """One sentence considered for the answer."""

    score: float
    text: str
    doc_id: str
    #: Position across all retrieved text, used to restore reading order.
    order: int


@LLM_PROVIDERS.register("extractive")
class ExtractiveProvider(LLMProvider):
    """Selects the sentences from `context` that best answer the question."""

    name = "extractive"
    remote = False

    #: Sentences shorter than this are fragments, not answers.
    MIN_SENTENCE_CHARS = 15

    def __init__(
        self,
        *,
        max_sentences: int = 3,
        min_relevance: float = 0.08,
        min_coverage: float = 0.30,
        min_answerable_terms: int = 2,
        coverage_ratio: float = 0.5,
        lead_bonus: float = 0.15,
        numeric_bonus: float = 1.25,
        cite: bool = True,
        **_: Any,
    ) -> None:
        self.max_sentences = max_sentences
        self.min_relevance = min_relevance
        # Coverage is measured over the question's *answerable* terms: the ones
        # that appear somewhere in the retrieved passages. Measuring against the
        # whole question punishes normal phrasing ("I paid for a client dinner
        # 45 days ago, can I still claim it?") where most words are scenario,
        # not subject.
        self.min_coverage = min_coverage
        # ...but if the evidence shares only one word with the question, that
        # word is a coincidence, not a topic. This is the gate that keeps the
        # assistant honest about questions the corpus does not cover.
        self.min_answerable_terms = min_answerable_terms
        # Supporting sentences need only reach this fraction of the best
        # sentence's coverage, so a good answer can carry its qualifying clause.
        self.coverage_ratio = coverage_ratio
        # Policy documents state the headline fact first and qualify it after,
        # so an earlier sentence is more likely to be the direct answer. Without
        # this, cosine quietly favours short qualifying clauses over the
        # entitlement sentence the user actually asked about.
        self.lead_bonus = lead_bonus
        # "How many days of leave" is answered by the sentence with the number
        # in it. Without this, every sentence about leave scores alike and the
        # shortest one wins, which is how a stub answers beside the point.
        self.numeric_bonus = numeric_bonus
        self.cite = cite

    def generate(
        self,
        messages: Sequence[Message],
        *,
        context: Sequence[RetrievedChunk] = (),
        **options: Any,
    ) -> LLMResponse:
        question = _last_user_message(messages)
        if not context:
            return LLMResponse(
                text=NO_ANSWER_TEMPLATE, model=self.name, finish_reason="no_context"
            )

        limit = int(options.get("max_sentences", self.max_sentences))
        candidates = self._rank(question, context)
        keep = candidates[:limit]

        if not keep:
            return LLMResponse(
                text=NO_ANSWER_TEMPLATE, model=self.name, finish_reason="below_threshold"
            )

        # Restore document order so a multi-sentence answer still reads naturally.
        keep.sort(key=lambda c: c.order)
        text = " ".join(
            f"{c.text} [{c.doc_id}]" if self.cite else c.text for c in keep
        )
        return LLMResponse(
            text=text,
            model=self.name,
            prompt_tokens=len(tokenize(question)),
            completion_tokens=len(tokenize(text)),
            raw={"selected": len(keep), "candidates": len(candidates)},
        )

    # ---------------------------------------------------------------- helpers #

    def _rank(
        self, question: str, context: Sequence[RetrievedChunk]
    ) -> list[_Candidate]:
        """Score every sentence that clears the gates, best first.

        Three stages: work out what the evidence could possibly answer, score
        each sentence, then keep only those close enough to the best one.
        """
        question_tokens = tokenize(question)
        answerable = self._answerable_terms(question_tokens, context)
        if len(answerable) < self.min_answerable_terms:
            return []

        # Binary term vectors, not term counts. In a single sentence a repeated
        # word says nothing about how well that sentence answers the question,
        # and counting it lets an incidental repetition outrank the sentence
        # that actually states the fact.
        question_vector = Counter(set(question_tokens))
        weights = self._term_weights(answerable, context)
        total_weight = sum(weights.values())
        wants_number = bool(_QUANTITY_QUESTION.search(question))
        scored: list[tuple[_Candidate, float]] = []
        order = 0

        for chunk_rank, chunk in enumerate(context):
            for sentence_index, sentence in enumerate(split_sentences(chunk.text)):
                order += 1
                if len(sentence) < self.MIN_SENTENCE_CHARS:
                    continue

                sentence_tokens = set(tokenize(sentence))
                sentence_coverage = (
                    sum(weights[t] for t in answerable if t in sentence_tokens)
                    / total_weight
                )
                if sentence_coverage <= 0.0:
                    continue

                similarity = cosine(question_vector, Counter(sentence_tokens))
                if similarity < self.min_relevance:
                    continue

                # Weighted coverage leads the ranking, because it answers the
                # question that matters: how much of what the user asked about
                # does this sentence actually address? Cosine only nudges ties;
                # on its own it favours short sentences over informative ones.
                # Then three adjustments: prefer better-ranked chunks, sentences
                # nearer the top of their passage, and sentences carrying a
                # figure when the question asks for one.
                chunk_weight = 1.0 - 0.05 * chunk_rank
                lead_weight = 1.0 + self.lead_bonus / (1 + sentence_index)
                numeric_weight = (
                    self.numeric_bonus
                    if wants_number and _HAS_DIGIT.search(sentence)
                    else 1.0
                )
                scored.append((
                    _Candidate(
                        score=sentence_coverage
                        * (1.0 + 0.25 * similarity)
                        * chunk_weight
                        * lead_weight
                        * numeric_weight,
                        text=sentence,
                        doc_id=chunk.doc_id,
                        order=order,
                    ),
                    sentence_coverage,
                ))

        if not scored:
            return []

        # The best sentence decides whether we answer at all; the rest only need
        # to stay within `coverage_ratio` of it to join the answer.
        best_coverage = max(c for _, c in scored)
        if best_coverage < self.min_coverage:
            return []

        floor = best_coverage * self.coverage_ratio
        candidates = [cand for cand, cov in scored if cov >= floor]
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    @staticmethod
    def _answerable_terms(
        question_tokens: Sequence[str], context: Sequence[RetrievedChunk]
    ) -> list[str]:
        """Question terms that appear somewhere in the retrieved passages.

        These are the only terms the evidence has any chance of addressing, so
        they are what coverage is measured against.
        """
        context_tokens: set[str] = set()
        for chunk in context:
            context_tokens.update(tokenize(chunk.text))
        # dict.fromkeys de-duplicates while keeping the question's word order,
        # which keeps the behaviour readable when debugging a bad answer.
        return [t for t in dict.fromkeys(question_tokens) if t in context_tokens]

    @staticmethod
    def _term_weights(
        answerable: Sequence[str], context: Sequence[RetrievedChunk]
    ) -> dict[str, float]:
        """Inverse document frequency of each term across the retrieved passages.

        A term appearing in every passage ("leave" in a set of leave documents)
        separates nothing; one appearing in a single passage ("reachable") is
        the term the question actually turns on. Weighting by rarity is what
        lets the specific sentence outrank the generic one.
        """
        chunk_tokens = [set(tokenize(chunk.text)) for chunk in context]
        total = len(chunk_tokens) or 1
        weights: dict[str, float] = {}
        for term in answerable:
            frequency = sum(1 for tokens in chunk_tokens if term in tokens) or 1
            weights[term] = math.log(1 + total / frequency)
        return weights


def _last_user_message(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role is Role.USER:
            return message.content
    return messages[-1].content if messages else ""
