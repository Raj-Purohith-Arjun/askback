"""Calibration: the kappa arithmetic, the agreement run, the gate, and the file format.

The judges here are scripted. The seed file's records are synthetic and prove
nothing about any judge, so no test asserts what a real judge scores on them -
only that the file loads, and is honest about what it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from askback.cli import main
from askback.core.contracts import ChatResponse
from askback.core.errors import DatasetError
from askback.datasets.schema import Turn
from askback.evaluation.base import JudgeUsage
from askback.evaluation.calibration import (
    CalibrationSet,
    calibrate,
    calibration_verdict,
    cohens_kappa,
    load_calibration,
)
from askback.evaluation.gates import EXIT_ERROR, EXIT_FAILED, EXIT_OK
from askback.evaluation.llm_judge import DIMENSIONS, Judge, JudgeVerdict
from askback.providers.base import LLM_PROVIDERS, LLMProvider, LLMResponse

REPO = Path(__file__).resolve().parents[1]


def _ratings(a: int, b: int, c: int, d: int) -> tuple[list[bool], list[bool]]:
    """Two raters from a 2x2 table: a both yes, b human-only yes, c judge-only yes, d both no."""
    human = [True] * a + [True] * b + [False] * c + [False] * d
    judge = [True] * a + [False] * b + [True] * c + [False] * d
    return human, judge


class TestKappa:
    def test_a_hand_worked_example(self) -> None:
        # 50 items: both yes 20, human-only yes 5, judge-only yes 10, both no 15.
        # p_o = (20 + 15) / 50 = 0.70.  Human yes-rate 25/50, judge yes-rate 30/50, so
        # p_e = 0.5 * 0.6 + 0.5 * 0.4 = 0.50.  kappa = (0.70 - 0.50) / (1 - 0.50) = 0.4.
        assert cohens_kappa(*_ratings(20, 5, 10, 15)) == pytest.approx(0.4)

    def test_perfect_agreement_is_one(self) -> None:
        assert cohens_kappa(*_ratings(7, 0, 0, 3)) == pytest.approx(1.0)

    def test_agreement_no_better_than_chance_is_zero(self) -> None:
        assert cohens_kappa(*_ratings(25, 25, 25, 25)) == pytest.approx(0.0)

    def test_always_saying_yes_scores_zero_however_often_it_is_right(self) -> None:
        # 90% raw agreement, and kappa sees through it: that is why it is the measure.
        human, judge = _ratings(90, 0, 10, 0)
        assert sum(h == j for h, j in zip(human, judge, strict=True)) == 90
        assert cohens_kappa(human, judge) == pytest.approx(0.0)

    def test_it_is_undefined_not_perfect_when_everyone_used_one_label(self) -> None:
        assert cohens_kappa(*_ratings(5, 0, 0, 0)) is None
        assert cohens_kappa([], []) is None


class _Oracle(Judge):
    """Scores each record from a table the test wrote."""

    identity = "oracle"

    def __init__(self, scores: dict[str, float], *, degrade: set[str] | None = None) -> None:
        self.scores, self.degrade = scores, degrade or set()

    def score(self, turn: Turn, response: ChatResponse) -> JudgeVerdict:
        if response.conversation_id.removeprefix("calibration:") in self.degrade:
            return JudgeVerdict(dict.fromkeys(DIMENSIONS, 0.5), "x", JudgeUsage(calls=1), "timed out")
        value = self.scores[turn.user]
        return JudgeVerdict(dict.fromkeys(DIMENSIONS, value), "scripted", JudgeUsage(calls=1))


def _record(rid: str, user: str, labels: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "id": rid, "user": user, "reply": "A reply.", "reference_answer": "The answer.",
        "labels": labels, "labeller": "tester", "date": "2026-01-02", **extra,
    }


def _write(path: Path, *records: dict[str, Any]) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "labelled_against": {"judge": "oracle", "prompt_version": "1"},
                "records": list(records),
            }
        ),
        encoding="utf-8",
    )
    return path


def _set(tmp_path: Path, labels: list[str]) -> CalibrationSet:
    rows = [_record(f"r{i}", f"q{i}", {"correctness": label}) for i, label in enumerate(labels)]
    return load_calibration(_write(tmp_path / "set.yaml", *rows))


class TestAgreement:
    LABELS = ["acceptable", "unacceptable", "acceptable", "unacceptable"]

    def test_a_judge_that_agrees_with_every_label_scores_one(self, tmp_path: Path) -> None:
        judge = _Oracle({"q0": 0.9, "q1": 0.1, "q2": 0.9, "q3": 0.1})

        report = calibrate(_set(tmp_path, self.LABELS), judge, threshold=0.55)

        assert report.overall.kappa == pytest.approx(1.0) and report.overall.rate == 1.0
        assert report.disagreements == []
        assert list(report.by_dimension) == ["correctness"]  # unlabelled dimensions are absent

    def test_a_judge_right_by_chance_scores_zero_and_its_misses_are_listed(self, tmp_path: Path) -> None:
        judge = _Oracle(dict.fromkeys(("q0", "q1", "q2", "q3"), 0.9))  # always acceptable

        report = calibrate(_set(tmp_path, self.LABELS), judge, threshold=0.55)

        assert report.overall.rate == 0.5 and report.overall.kappa == pytest.approx(0.0)
        assert [(d.record_id, d.dimension) for d in report.disagreements] == [
            ("r1", "correctness"), ("r3", "correctness")
        ]

    def test_the_cut_is_the_one_passed_in(self, tmp_path: Path) -> None:
        judge = _Oracle({"q0": 0.6, "q1": 0.1, "q2": 0.6, "q3": 0.1})
        calibration = _set(tmp_path, self.LABELS)

        assert calibrate(calibration, judge, threshold=0.55).overall.kappa == pytest.approx(1.0)
        assert calibrate(calibration, judge, threshold=0.7).overall.kappa == pytest.approx(0.0)

    def test_a_degraded_verdict_is_left_out_not_counted_as_the_models(self, tmp_path: Path) -> None:
        judge = _Oracle({"q0": 0.9, "q1": 0.1, "q2": 0.9, "q3": 0.1}, degrade={"r3"})

        report = calibrate(_set(tmp_path, self.LABELS), judge, threshold=0.55)

        assert report.degraded == [("r3", "timed out")] and report.overall.n == 3
        refused = calibration_verdict(report, min_kappa=0.6, max_degraded=0)
        assert (refused.passed, refused.exit_code) == (False, EXIT_ERROR)
        assert calibration_verdict(report, min_kappa=0.6, max_degraded=1).exit_code == EXIT_OK


class TestGate:
    def test_kappa_below_min_kappa_fails_and_at_it_passes(self, tmp_path: Path) -> None:
        judge = _Oracle(dict.fromkeys(("q0", "q1", "q2", "q3"), 0.9))
        report = calibrate(_set(tmp_path, TestAgreement.LABELS), judge, threshold=0.55)  # kappa 0

        failed = calibration_verdict(report, min_kappa=0.6, max_degraded=0)
        assert (failed.passed, failed.exit_code) == (False, EXIT_FAILED)
        assert "not fit to gate a merge" in failed.reason
        assert calibration_verdict(report, min_kappa=0.0, max_degraded=0).exit_code == EXIT_OK

    def test_an_undefined_kappa_does_not_clear_the_gate(self, tmp_path: Path) -> None:
        judge = _Oracle({"q0": 0.9, "q1": 0.9})
        report = calibrate(_set(tmp_path, ["acceptable", "acceptable"]), judge, threshold=0.55)

        verdict = calibration_verdict(report, min_kappa=0.0, max_degraded=0)
        assert report.overall.kappa is None
        assert verdict.exit_code == EXIT_FAILED and "undefined" in verdict.reason


class _KeywordLLM(LLMProvider):
    """A judge model that rates a reply 0.9 if it says GOOD and 0.1 otherwise."""

    name = "keyword-judge"
    remote = True

    def __init__(self, model: str = "k-1", env_prefix: str = "", **_: Any) -> None:
        self.model = model

    def generate(self, messages: Any, *, context: Any = (), **options: Any) -> LLMResponse:
        value = 0.9 if "GOOD" in messages[1].content else 0.1
        scores = ", ".join(f'"{d}": {value}' for d in DIMENSIONS)
        return LLMResponse(text="{" + scores + ', "rationale": "keyword"}')

    def is_available(self) -> bool:
        return True


class TestCommand:
    @pytest.fixture(autouse=True)
    def provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(LLM_PROVIDERS._items, "keyword-judge", _KeywordLLM)

    @staticmethod
    def _run(tmp_path: Path, labels: list[str], min_kappa: float | None = None) -> int:
        judge: dict[str, Any] = {
            "llm": {"provider": "keyword-judge"}, "cache_path": str(tmp_path / "cache"),
        }
        if min_kappa is not None:
            judge["min_kappa"] = min_kappa
        config = tmp_path / "app.yaml"
        config.write_text(yaml.safe_dump({"evaluation": {"judge": judge}}), encoding="utf-8")
        rows = [
            _record(f"r{i}", f"q{i}", {"correctness": label}, reply="GOOD" if i % 2 == 0 else "BAD")
            for i, label in enumerate(labels)
        ]
        records = _write(tmp_path / "records.yaml", *rows)
        return main(["--config", str(config), "calibrate", "--records", str(records)])

    def test_exit_0_when_the_judge_agrees_with_the_humans(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert self._run(tmp_path, ["acceptable", "unacceptable"] * 2) == EXIT_OK
        assert "kappa 1.00 meets min_kappa 0.60" in capsys.readouterr().out

    def test_exit_1_below_min_kappa_and_the_disagreements_are_named(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The judge says GOOD, BAD, GOOD, BAD; these labels agree on half of them.
        assert self._run(tmp_path, ["acceptable", "acceptable", "unacceptable", "unacceptable"]) == EXIT_FAILED

        out = capsys.readouterr().out
        assert "kappa 0.00 is below min_kappa 0.60" in out
        assert "r1" in out and "r2" in out
        assert "GOOD" not in out and "The answer" not in out  # records may be HR data: ids only

    def test_min_kappa_is_configurable(self, tmp_path: Path) -> None:
        labels = ["acceptable", "acceptable", "unacceptable", "unacceptable"]  # kappa 0
        assert self._run(tmp_path, labels, min_kappa=0.0) == EXIT_OK

    def test_a_missing_records_file_is_an_operator_error(self, tmp_path: Path) -> None:
        assert main(["calibrate", "--records", str(tmp_path / "nope.yaml")]) == EXIT_ERROR


class TestFile:
    @pytest.mark.parametrize(
        ("record", "match"),
        [
            (_record("a", "q", {"correctness": "maybe"}), "must be one of"),
            (_record("a", "q", {"correctnes": "acceptable"}), "unknown dimension"),
            (_record("a", "q", {}), "at least one dimension"),
            (_record("a", "q", {"clarity": "acceptable"}, labler="x"), "unknown key"),
            (_record("a", "q", {"clarity": "acceptable"}, date="soon"), "ISO date"),
            ({**_record("a", "q", {"clarity": "acceptable"}), "reference_answer": " "}, "reference_answer"),
        ],
    )
    def test_a_malformed_record_is_refused_by_name(
        self, tmp_path: Path, record: dict[str, Any], match: str
    ) -> None:
        with pytest.raises(DatasetError, match=match):
            load_calibration(_write(tmp_path / "bad.yaml", record))

    def test_duplicate_ids_are_refused(self, tmp_path: Path) -> None:
        row = _record("a", "q", {"clarity": "acceptable"})
        with pytest.raises(DatasetError, match="duplicate record id"):
            load_calibration(_write(tmp_path / "dup.yaml", row, row))

    def test_the_seed_loads_and_says_it_is_synthetic(self) -> None:
        seed = load_calibration(REPO / "examples" / "hr" / "calibration" / "seed.yaml")

        assert len(seed.records) == 10
        assert seed.synthetic == len(seed.records)
        # Both classes are present, or no judge could be given a defined kappa from it.
        labels = {v for r in seed.records for v in r.labels.values()}
        assert labels == {True, False}
