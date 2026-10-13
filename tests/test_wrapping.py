"""Where a reply wraps must not change what it is graded as.

A renderer wraps at whatever width it was given, and nothing about the agent changes with
it. Every detector in the harness reads phrases out of that text, so a pattern written with
single spaces quietly stops matching when a line break lands inside one of its phrases:
nothing fails, a signal is simply absent, and the miss reads as the agent's wording rather
than as our bug. That is how `closing_offer` was lost from a wrapped answer (-0.6 to -0.3)
and `purpose_clause` from a wrapped question (1.15 to 0.90).

So the invariant is not "this pattern has a `\\s+`": it is that a fixture re-wrapped at any
width is classified identically. These tests re-wrap every fixture we have at four widths
and compare the whole verdict - score and signals, not just the boolean.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml
from test_citations import CHAPTER_5, MANIFEST_YAML
from test_clarification import ANSWER_1, ANSWER_2, GREETING, QUESTION_1, QUESTION_2

from askback.core.contracts import ChatResponse
from askback.core.text import tolerate_line_breaks
from askback.evaluation.citation_eval import Manifest, parse_citations, resolve
from askback.evaluation.clarification import QuestionDetector
from askback.evaluation.pii_eval import redact_pii, scan
from askback.evaluation.rule_based import fact_present
from askback.evaluation.safety_eval import _SECRET_RULES

WIDTHS = (32, 40, 60, 80)


def rewrap(text: str, width: int) -> str:
    """Re-wrap each line to `width`, keeping blank lines: what a renderer does to a reply."""
    return "\n".join(
        "\n".join(textwrap.wrap(line, width)) if line.strip() else "" for line in text.splitlines()
    )


REPLIES = {
    "question-1": QUESTION_1,
    "question-2": QUESTION_2,
    "answer-cited": ANSWER_1,
    "answer-ends-with-offer": ANSWER_2,
    "greeting": GREETING,
}


class TestTheDetectorIsIndifferentToWrapping:
    @pytest.mark.parametrize("width", WIDTHS)
    @pytest.mark.parametrize("name", sorted(REPLIES))
    def test_a_rewrapped_reply_scores_the_same(self, name: str, width: int) -> None:
        detector = QuestionDetector()
        whole = detector.detect(ChatResponse(reply=REPLIES[name], conversation_id="c"))
        wrapped = detector.detect(ChatResponse(reply=rewrap(REPLIES[name], width), conversation_id="c"))

        assert (wrapped.is_question, wrapped.signals) == (whole.is_question, whole.signals)
        assert wrapped.score == pytest.approx(whole.score)

    def test_a_break_inside_a_phrase_keeps_that_phrases_signal(self) -> None:
        # The case that found the bug: "so I can check" wrapped as "so I / can check".
        wrapped = QUESTION_1.replace("plan so I can check", "plan so I\ncan check")
        detection = QuestionDetector().detect(ChatResponse(reply=wrapped, conversation_id="c"))

        assert "purpose_clause" in detection.signals and detection.score == pytest.approx(1.15)

    def test_a_pattern_added_in_config_gets_the_same_treatment(self) -> None:
        detector = QuestionDetector(patterns={"ask_for_id": (0.4, r"\byour employee number\b")})
        wrapped = ChatResponse(reply="Please send your employee\nnumber.", conversation_id="c")

        assert "ask_for_id" in detector.detect(wrapped).signals


class TestCitationsAreReadAcrossAWrap:
    @pytest.fixture
    def manifest(self) -> Manifest:
        return Manifest.from_dict(yaml.safe_load(MANIFEST_YAML))

    @pytest.mark.parametrize("width", WIDTHS)
    @pytest.mark.parametrize(
        "reply",
        [
            ANSWER_1,
            CHAPTER_5,
            "Chapter 8 - Career Progression Policy of the Harborview Staff Handbook 2025",
            "Harborview Staff Handbook 2025, pages 32-33",
            f"- {CHAPTER_5}\n- Chapter 8 - Career Progression Policy\n- pages 62-63",
        ],
        ids=["production-answer", "chapter-and-title", "chapter-of-document", "document-and-pages", "two-chapters"],
    )
    def test_the_same_problems_are_found_at_any_width(self, reply: str, width: int, manifest: Manifest) -> None:
        whole = resolve(parse_citations(reply), manifest)
        wrapped = resolve(parse_citations(rewrap(reply, width)), manifest)

        assert wrapped == whole

    def test_a_wrong_title_is_still_caught_when_the_colon_ends_a_line(self, manifest: Manifest) -> None:
        problems = resolve(parse_citations("Chapter 5:\nGeneral Conduct"), manifest).title
        assert problems and "not 'General Conduct'" in problems[0]

    def test_a_colon_that_introduces_prose_is_not_a_claimed_title(self, manifest: Manifest) -> None:
        # The false positive that undoing wraps would otherwise create: a lead-in read as a
        # title would fail a correct answer for naming its chapter wrongly.
        reply = "According to Chapter 5:\nStaff must not remove safety equipment."
        assert parse_citations(reply).chapters[0].title is None
        assert resolve(parse_citations(reply), manifest).title is None, "no title was claimed"

    def test_a_list_is_not_run_together_by_undoing_wraps(self, manifest: Manifest) -> None:
        # Two chapters in one bulleted block leave a page unattributable, at any width.
        reply = f"- {CHAPTER_5}\n- Chapter 8 - Career Progression Policy\n- pages 62-63"
        for width in WIDTHS:
            assert parse_citations(rewrap(reply, width)).pages[0].chapter is None


class TestSensitiveValuesSurviveAWrap:
    @pytest.mark.parametrize(
        ("category", "text"),
        [
            ("email", "Write to test-user@example.org\nabout it."),
            ("phone", "Call 020 7946\n0958 and ask."),
            ("iban", "The account is GB29 NWBK 6016\n1331 9268 19 exactly."),
            ("credit_card", "The card was 4111 1111 1111\n1111 on file."),
            ("us_ssn", "Their number is\n123-45-6789 on record."),
            ("salary_figure", "Their salary is\n$85,000 a year."),
        ],
    )
    def test_pii_broken_by_a_line_break_is_still_found(self, category: str, text: str) -> None:
        found, _ = scan(text, categories=None)
        assert found[category] == 1, found
        assert f"[REDACTED:{category}]" in redact_pii(text)

    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("assigned_secret", "Use api\nkey: 7f3b9c2d1e4a5b6c in the header."),
            ("private_key", "-----BEGIN RSA PRIVATE\nKEY-----"),
            ("bearer_token", "Send Authorization: Bearer\nabcdefghijklmnopqrstuvwxyz012345"),
        ],
    )
    def test_a_credential_broken_by_a_line_break_is_still_a_leak(self, label: str, text: str) -> None:
        assert label in [name for name, rule in _SECRET_RULES if rule.search(text)]


class TestExpectationsIgnoreWrapping:
    @pytest.mark.parametrize("fact", ["two equal instalments", "/two equal instal+ments/"])
    def test_a_required_fact_matches_across_a_break(self, fact: str) -> None:
        assert fact_present(fact, "recovered in two equal\ninstalments, and the same")


class TestTheRewrite:
    @pytest.mark.parametrize(
        ("pattern", "rewritten"),
        [
            (r"\bpolicy manual\b", r"\bpolicy\s+manual\b"),
            (r"api[_ -]?key", r"api[_\s-]?key"),
            (r"[A-Z ]*PRIVATE KEY", r"[A-Z\s]*PRIVATE\s+KEY"),
            (r"a ?b", r"a\s*b"),  # not `a\s+?b`, which would make the space required
            (r"a +b", r"a\s+b"),
            (r"[^ ]x", r"[^\s]x"),
            (r"[] ]y", r"[]\s]y"),  # a `]` first in a class is a literal, not the end
            (r"a\ b", r"a\ b"),  # an escaped space asks for one real space
            (r"a {2,3}b", r"a {2,3}b"),  # the count is of spaces; rewriting changes what it counts
        ],
    )
    def test_spaces_become_whitespace_except_where_they_are_meant(
        self, pattern: str, rewritten: str
    ) -> None:
        assert tolerate_line_breaks(pattern) == rewritten
