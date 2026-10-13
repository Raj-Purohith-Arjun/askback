"""Citation existence: sources the agent names must exist in the document's manifest.

The prose is verbatim production text. The en-dash is deliberate and tested both ways: a
document pipeline emits "–", and a pattern written for "-" alone silently misses it, which
would read as "no citation to check" and pass.

What is NOT tested, because the check does not claim it: that a cited passage supports the
claim beside it. Resolving means the agent pointed somewhere real, nothing more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from test_clarification import ANSWER_1

from askback.adapters.scripted import ScriptedAdapter
from askback.core.contracts import ChatResponse
from askback.core.errors import ConfigError
from askback.datasets.schema import Expectations, TestCase, Turn
from askback.evaluation.citation_eval import (
    CitationExistenceEvaluator,
    Manifest,
    load_manifest,
    parse_citations,
    resolve,
)
from askback.evaluation.engine import DEFAULT_EVALUATORS, EvaluationEngine
from askback.evaluation.rule_based import RuleBasedEvaluator
from askback.runner import TestRunner

MANIFEST_YAML = """
name: Harborview Staff Handbook 2025
aliases: [Harborview Staff Handbook, the staff handbook]
total_pages: 300
chapters:
  - number: 1
    title: Introduction
    pages: [1, 9]
  - number: 3
    title: Leave Policy
    pages: [10, 27]
  - number: 5
    title: Workplace Safety, Conduct & Corrective Action
    aliases: [Conduct Rules, Corrective Action Procedure]
    pages: [28, 45]
  - number: 8
    title: Career Progression Policy
    pages: [60, 75]
  - number: 10
    title: Separation
    pages: [250, 300]
"""

CHAPTER_5 = "Chapter 5: Workplace Safety, Conduct & Corrective Action"


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.from_dict(yaml.safe_load(MANIFEST_YAML))


def _problems(reply: str, manifest: Manifest) -> dict[str, list[str]]:
    found = resolve(parse_citations(reply), manifest)
    return {k: v for k in ("chapter", "title", "page", "document") if (v := getattr(found, k))}


def _resolved(reply: str, manifest: Manifest) -> bool:
    found = resolve(parse_citations(reply), manifest)
    kinds = [found.chapter, found.title, found.page, found.document]
    return any(k is not None for k in kinds) and not any(kinds[i] for i in range(4))


class TestCorrectCitationsResolve:
    @pytest.mark.parametrize(
        "reply",
        [
            "- Harborview Staff Handbook 2025, pages 32-33",
            CHAPTER_5,
            "Chapter 8 – Career Progression Policy of the Harborview Staff Handbook 2025",  # en-dash
            "Chapter 8 - Career Progression Policy of the Harborview Staff Handbook 2025",  # hyphen
            "According to the Harborview Staff Handbook 2025:",
            "page 7",
            "Chapter 8",  # no title claimed, so none to contradict
            "Chapter 5: Conduct Rules",  # a chapter alias
            "Chapter 5 (Corrective Action Procedure)",
            "pages 32–33",  # en-dash page range
            "Chapter 5: Workplace Safety, Conduct & Corrective Action, pages 28-45",
            "the staff handbook, page 12",
        ],
    )
    def test_a_real_citation_resolves(self, reply: str, manifest: Manifest) -> None:
        assert _resolved(reply, manifest), _problems(reply, manifest)

    def test_the_production_answer_resolves_whole(self, manifest: Manifest) -> None:
        # Pages 32-33 sit in a block that cites only Chapter 5 (pages 28-45): attributed, in range.
        assert _resolved(ANSWER_1, manifest), _problems(ANSWER_1, manifest)
        assert parse_citations(ANSWER_1).pages[0].chapter == 5


class TestInventedCitationsAreCaught:
    def test_a_chapter_that_does_not_exist(self, manifest: Manifest) -> None:
        problems = _problems("Chapter 19: Remote Work", manifest)
        assert "Chapter 19 does not exist" in problems["chapter"][0]

    def test_a_page_outside_the_chapter_it_is_attributed_to(self, manifest: Manifest) -> None:
        problems = _problems(f"{CHAPTER_5}, page 95", manifest)
        assert "page 95 is outside Chapter 5" in problems["page"][0] and "28-45" in problems["page"][0]

    def test_a_page_beyond_the_document(self, manifest: Manifest) -> None:
        assert "beyond the document" in _problems("Harborview Staff Handbook 2025, page 450", manifest)["page"][0]

    def test_a_real_chapter_under_the_wrong_title(self, manifest: Manifest) -> None:
        problems = _problems("Chapter 5: Leave Policy", manifest)
        assert "not 'Leave Policy'" in problems["title"][0] and "chapter" not in problems

    @pytest.mark.parametrize(
        "reply",
        [
            "According to the Employee Handbook 2025:",
            "Harborview Staff Handbook 2024, pages 3-4",  # right document, wrong year
            "Chapter 8 – Career Progression Policy of the Staff Rulebook 2020",
        ],
    )
    def test_an_unknown_document(self, reply: str, manifest: Manifest) -> None:
        assert "is not 'Harborview Staff Handbook 2025'" in _problems(reply, manifest)["document"][0]

    def test_a_reversed_page_range(self, manifest: Manifest) -> None:
        assert "not a valid page range" in _problems("pages 33-32", manifest)["page"][0]

    def test_a_page_cited_for_two_chapters_is_not_pinned_on_the_wrong_one(self, manifest: Manifest) -> None:
        # Ambiguous: the page could belong to either. It is held to the document's length only,
        # because a wrong guess would fail a correct answer.
        reply = f"- {CHAPTER_5}\n- Chapter 8 – Career Progression Policy\n- pages 62-63"
        assert parse_citations(reply).pages[0].chapter is None
        assert _problems(reply, manifest) == {}


class TestSkippedNeverPassed:
    def _evaluate(self, evaluator: CitationExistenceEvaluator, reply: str, *, verify: bool = True) -> Any:
        case = TestCase(id="c", turns=[Turn(user="q", expect=Expectations(verify_citations=verify))])
        return evaluator.evaluate(case, case.turns[0], ChatResponse(reply=reply, conversation_id="c"))

    def test_with_no_manifest_it_skips_and_says_why(self) -> None:
        result = self._evaluate(CitationExistenceEvaluator(), "Chapter 5: Leave Policy")
        assert result.skipped and not result.passed
        assert "no citation manifest is configured" in result.checks[0].detail

    def test_a_reply_with_no_readable_citation_is_not_a_pass(self, manifest: Manifest) -> None:
        result = self._evaluate(CitationExistenceEvaluator(manifest=manifest), "Employees get 24 days.")
        assert result.skipped and result.checks[0].name == "citations_found"

    def test_a_turn_that_did_not_ask_is_left_alone(self, manifest: Manifest) -> None:
        result = self._evaluate(
            CitationExistenceEvaluator(manifest=manifest), "Chapter 19", verify=False
        )
        assert result.skipped and result.checks == []

    def test_the_engine_skips_visibly_when_a_turn_asks_and_no_evaluator_is_configured(self) -> None:
        engine = EvaluationEngine([RuleBasedEvaluator()])
        case = TestCase(id="c", turns=[Turn(user="q", expect=Expectations(verify_citations=True))])
        results, _, _ = engine.evaluate(case, case.turns[0], ChatResponse(reply="Chapter 19", conversation_id="c"))

        citation = next(r for r in results if r.evaluator == "citation_existence")
        assert citation.skipped and not citation.passed

    def test_a_real_failure_is_critical_and_fails_the_turn(self, manifest: Manifest) -> None:
        adapter = ScriptedAdapter(replies={}, default=ChatResponse(reply="See Chapter 19.", conversation_id="c"))
        case = TestCase(id="c", turns=[Turn(user="q", expect=Expectations(verify_citations=True))])
        engine = EvaluationEngine([CitationExistenceEvaluator(manifest=manifest)])
        report = TestRunner(adapter, engine, workers=1).run([case])

        (check,) = report.cases[0].critical_checks
        assert check.name == "citation_chapter" and not report.cases[0].passed


class TestConfiguration:
    def test_the_manifest_loads_from_a_file(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        path.write_text(MANIFEST_YAML, encoding="utf-8")
        loaded = load_manifest(path)
        assert loaded.total_pages == 300 and loaded.chapters[5].last_page == 45

    @pytest.mark.parametrize(
        ("edit", "message"),
        [
            ({"total_pages": 0}, "total_pages"),
            ({"chapters": []}, "non-empty list"),
            ({"colour": "red"}, "unknown key"),
            ({"chapters": [{"number": 1, "title": "A", "pages": [1, 999]}]}, "past total_pages"),
            (
                {"chapters": [{"number": 1, "title": "A", "pages": [1, 2]}] * 2},
                "listed twice",
            ),
        ],
    )
    def test_a_bad_manifest_is_refused_by_name(self, edit: dict[str, Any], message: str) -> None:
        data = {**yaml.safe_load(MANIFEST_YAML), **edit}
        with pytest.raises(ConfigError, match=message):
            Manifest.from_dict(data)

    def test_a_missing_file_is_an_error_not_an_empty_map(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="could not be read"):
            load_manifest(tmp_path / "nope.yaml")

    def test_it_joins_a_run_only_when_a_manifest_is_configured(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        path.write_text(MANIFEST_YAML, encoding="utf-8")

        assert "citation_existence" not in DEFAULT_EVALUATORS
        assert "citation_existence" not in EvaluationEngine.from_config({}).describe()["evaluators"]
        configured = EvaluationEngine.from_config({"citations": {"manifest": str(path)}}).describe()
        assert "citation_existence" in configured["evaluators"]
        assert configured["weights"]["citation_existence"] == 0.0, "a gate: it vetoes, it does not dilute"

    def test_the_expectation_is_a_strict_boolean(self) -> None:
        with pytest.raises(Exception, match="verify_citations"):
            Expectations.from_dict({"verify_citations": "yess"}, where="t")
