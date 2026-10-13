"""Calibration: does the judge agree with a human?

A judge is a measuring instrument, and every score it produces is unvalidated
until someone has checked it against people who own the answer. This module
holds the labelled records, runs a judge over them, and measures agreement with
Cohen's kappa rather than raw agreement: when most answers are acceptable, a
judge that always says "acceptable" agrees with the humans most of the time and
knows nothing. Kappa discounts the agreement chance alone would produce, so that
judge scores 0.

The human label is binary per dimension. The judge's score is cut at the same
threshold that gates a real run (`evaluator_options.llm_judge.threshold`), so
what is validated is the decision that fails builds, not the raw number.

Three rules, each a case of "unmeasurable never passes":

  - A response the model judge could not grade is graded by the heuristic and
    marked degraded. It is left out of the statistics - counting it would
    validate the fallback and call it the model - and the run exits 3.
  - A label the human did not give is not a label. A record may label only some
    dimensions; the rest are excluded, never defaulted.
  - Kappa is undefined when the humans and the judge both used one and the same
    class throughout (there is nothing to agree *beyond* chance about). That is
    reported as undefined and does not clear the gate.

Records are test data and may be real agent output. Nothing here prints a reply or
a context passage; disagreements are listed by id, for a human to open.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.contracts import ChatResponse, ResponseStatus, RetrievedChunk
from ..core.errors import AskbackError, DatasetError
from ..datasets.schema import Expectations, Turn
from .base import JudgeUsage
from .gates import EXIT_ERROR, EXIT_FAILED, EXIT_OK
from .llm_judge import DIMENSIONS, Judge

ACCEPTABLE = "acceptable"
UNACCEPTABLE = "unacceptable"
LABELS = (ACCEPTABLE, UNACCEPTABLE)

SCHEMA_VERSION = 1

_FILE_KEYS = {"schema_version", "labelled_against", "records"}
_RECORD_KEYS = {
    "id", "user", "reply", "reference_answer", "retrieved", "status",
    "labels", "labeller", "date", "synthetic",
}


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    """One reply, and what a person who owns the answer made of it."""

    id: str
    user: str
    reply: str
    #: The judge scores against it, so a record without one cannot be judged.
    reference_answer: str
    retrieved: tuple[RetrievedChunk, ...]
    #: dimension -> True when the human found it acceptable. Only dimensions the
    #: human labelled appear here.
    labels: Mapping[str, bool]
    labeller: str
    date: dt.date
    status: ResponseStatus = ResponseStatus.ANSWERED
    synthetic: bool = False


@dataclass(frozen=True, slots=True)
class CalibrationSet:
    records: tuple[CalibrationRecord, ...]
    #: The judge (and prompt version) whose run output the records were drawn from.
    #: Records are labelled by people, so this does not stale them; it says whose
    #: borderline cases they are, which a different judge's agreement depends on.
    labelled_judge: str
    labelled_prompt_version: str
    source: str = ""

    @property
    def synthetic(self) -> int:
        return sum(r.synthetic for r in self.records)


def load_calibration(path: str | Path) -> CalibrationSet:
    """Read and strictly validate a calibration file.

    Strict like the nested dataset form: an unknown key is refused, because a
    label typo (`correctnes`) would otherwise be a label that was never read.
    """
    target = Path(path)
    if not target.is_file():
        raise DatasetError(f"calibration file not found: {target}")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DatasetError("Calibration files need PyYAML. Run: pip install pyyaml") from exc
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DatasetError(f"{target}: not valid YAML: {exc}") from exc

    try:
        return _parse(data, str(target))
    except DatasetError as exc:
        raise DatasetError(f"{target}: {exc}") from exc


def _parse(data: Any, source: str) -> CalibrationSet:
    if not isinstance(data, Mapping):
        raise DatasetError("must be a mapping with `labelled_against` and `records`")
    _no_stray(data, _FILE_KEYS, "file")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise DatasetError(f"schema_version must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}")

    against = data.get("labelled_against")
    if not isinstance(against, Mapping):
        raise DatasetError("`labelled_against` is required: the judge and prompt_version the records came from")
    _no_stray(against, {"judge", "prompt_version"}, "labelled_against")
    judge = _text(against, "judge", "labelled_against")
    prompt_version = _text(against, "prompt_version", "labelled_against")

    rows = data.get("records")
    if not isinstance(rows, list) or not rows:
        raise DatasetError("`records` must be a non-empty list")
    records = tuple(_record(row, index) for index, row in enumerate(rows, start=1))
    ids = [r.id for r in records]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise DatasetError(f"duplicate record id(s): {duplicated}")
    return CalibrationSet(records, judge, prompt_version, source)


def _record(row: Any, index: int) -> CalibrationRecord:
    where = f"record {index}"
    if not isinstance(row, Mapping):
        raise DatasetError(f"{where} must be a mapping")
    where = f"record {index} ({row.get('id')!r})"
    _no_stray(row, _RECORD_KEYS, where)

    raw_labels = row.get("labels")
    if not isinstance(raw_labels, Mapping) or not raw_labels:
        raise DatasetError(f"{where}: `labels` must name at least one dimension")
    labels: dict[str, bool] = {}
    for dimension, label in raw_labels.items():
        if dimension not in DIMENSIONS:
            raise DatasetError(f"{where}: unknown dimension {dimension!r}; known: {list(DIMENSIONS)}")
        if label not in LABELS:
            raise DatasetError(f"{where}: label for {dimension} must be one of {list(LABELS)}, got {label!r}")
        labels[str(dimension)] = label == ACCEPTABLE

    status = row.get("status", ResponseStatus.ANSWERED.value)
    try:
        parsed_status = ResponseStatus(status)
    except ValueError:
        raise DatasetError(f"{where}: unknown status {status!r}") from None

    chunks = row.get("retrieved", [])
    if not isinstance(chunks, list):
        raise DatasetError(f"{where}: `retrieved` must be a list")
    retrieved = tuple(_chunk(c, where, n) for n, c in enumerate(chunks, start=1))

    synthetic = row.get("synthetic", False)
    if not isinstance(synthetic, bool):
        raise DatasetError(f"{where}: `synthetic` must be true or false")

    return CalibrationRecord(
        id=_text(row, "id", where),
        user=_text(row, "user", where),
        reply=_text(row, "reply", where),
        reference_answer=_text(row, "reference_answer", where),
        retrieved=retrieved,
        labels=labels,
        labeller=_text(row, "labeller", where),
        date=_date(row.get("date"), where),
        status=parsed_status,
        synthetic=synthetic,
    )


def _chunk(raw: Any, where: str, number: int) -> RetrievedChunk:
    if not isinstance(raw, Mapping):
        raise DatasetError(f"{where}: retrieved[{number}] must be a mapping with doc_id and text")
    _no_stray(raw, {"doc_id", "title", "text"}, f"{where} retrieved[{number}]")
    doc_id = _text(raw, "doc_id", f"{where} retrieved[{number}]")
    return RetrievedChunk(
        doc_id=doc_id,
        chunk_id=f"{doc_id}#0",
        text=_text(raw, "text", f"{where} retrieved[{number}]"),
        title=str(raw.get("title", "")),
    )


def _text(data: Mapping[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{where}: `{key}` is required and must be non-empty text")
    return value.strip()


def _date(value: Any, where: str) -> dt.date:
    # YAML reads an unquoted 2026-03-01 as a date already.
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise DatasetError(f"{where}: `date` must be an ISO date (YYYY-MM-DD), got {value!r}") from None


def _no_stray(data: Mapping[str, Any], known: set[str], where: str) -> None:
    stray = sorted(str(k) for k in set(data) - known)
    if stray:
        raise DatasetError(f"{where}: unknown key(s) {stray}; known: {sorted(known)}")


# --------------------------------------------------------------------------- #
# Agreement
# --------------------------------------------------------------------------- #


def cohens_kappa(human: Sequence[bool], judge: Sequence[bool]) -> float | None:
    """Cohen's kappa for two binary raters over the same items.

        kappa = (p_o - p_e) / (1 - p_e)

    p_o is the observed agreement, p_e the agreement expected if each rater
    labelled independently at their own rate. None when p_e is 1, which is when
    both raters used the same single class throughout: chance alone explains all
    of it, and there is nothing to be better than. Callers must treat None as
    "not measured", never as perfect agreement.
    """
    if len(human) != len(judge):
        raise ValueError(f"raters labelled different numbers of items: {len(human)} vs {len(judge)}")
    n = len(human)
    if n == 0:
        return None
    observed = sum(h == j for h, j in zip(human, judge, strict=True)) / n
    h_yes, j_yes = sum(human) / n, sum(judge) / n
    expected = h_yes * j_yes + (1 - h_yes) * (1 - j_yes)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True, slots=True)
class Agreement:
    """How far the judge and the humans agree over some set of (record, dimension) labels."""

    n: int
    agreed: int
    kappa: float | None

    @property
    def rate(self) -> float:
        return self.agreed / self.n if self.n else 0.0


def _agreement(human: Sequence[bool], judge: Sequence[bool]) -> Agreement:
    return Agreement(
        n=len(human),
        agreed=sum(h == j for h, j in zip(human, judge, strict=True)),
        kappa=cohens_kappa(human, judge),
    )


@dataclass(frozen=True, slots=True)
class Disagreement:
    record_id: str
    dimension: str
    human: bool
    judge: bool
    score: float
    rationale: str


@dataclass(slots=True)
class CalibrationReport:
    judge: str
    #: What else pins the judge (prompt version), from `Judge.provenance()`.
    provenance: dict[str, str]
    threshold: float
    labelled_judge: str
    labelled_prompt_version: str
    source: str
    records: int
    synthetic: int
    overall: Agreement
    by_dimension: dict[str, Agreement]
    disagreements: list[Disagreement] = field(default_factory=list)
    #: (record id, reason) for each response the judge could not grade. Excluded above.
    degraded: list[tuple[str, str]] = field(default_factory=list)
    usage: JudgeUsage | None = None


def calibrate(calibration: CalibrationSet, judge: Judge, *, threshold: float) -> CalibrationReport:
    """Run `judge` over every record and measure its agreement with the human labels."""
    human: dict[str, list[bool]] = {d: [] for d in DIMENSIONS}
    machine: dict[str, list[bool]] = {d: [] for d in DIMENSIONS}
    disagreements: list[Disagreement] = []
    degraded: list[tuple[str, str]] = []
    usage: JudgeUsage | None = None

    for record in calibration.records:
        verdict = judge.score(
            Turn(user=record.user, expect=Expectations(reference_answer=record.reference_answer)),
            ChatResponse(
                reply=record.reply,
                conversation_id=f"calibration:{record.id}",
                status=record.status,
                retrieved=list(record.retrieved),
            ),
        )
        if verdict.usage is not None:
            usage = verdict.usage if usage is None else usage + verdict.usage
        if verdict.degraded is not None:
            degraded.append((record.id, verdict.degraded))
            continue

        for dimension, human_ok in record.labels.items():
            if dimension not in verdict.scores:
                raise AskbackError(f"judge {judge.identity!r} returned no score for {dimension!r}")
            score = verdict.scores[dimension]
            judge_ok = score >= threshold
            human[dimension].append(human_ok)
            machine[dimension].append(judge_ok)
            if judge_ok != human_ok:
                disagreements.append(
                    Disagreement(record.id, dimension, human_ok, judge_ok, score, verdict.rationale)
                )

    return CalibrationReport(
        judge=judge.identity,
        provenance=judge.provenance(),
        threshold=threshold,
        labelled_judge=calibration.labelled_judge,
        labelled_prompt_version=calibration.labelled_prompt_version,
        source=calibration.source,
        records=len(calibration.records),
        synthetic=calibration.synthetic,
        overall=_agreement(
            [h for d in DIMENSIONS for h in human[d]], [m for d in DIMENSIONS for m in machine[d]]
        ),
        by_dimension={d: _agreement(human[d], machine[d]) for d in DIMENSIONS if human[d]},
        disagreements=disagreements,
        degraded=degraded,
        usage=usage,
    )


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CalibrationVerdict:
    passed: bool
    exit_code: int
    reason: str


def calibration_verdict(
    report: CalibrationReport, *, min_kappa: float, max_degraded: int
) -> CalibrationVerdict:
    """Decide the exit code. Exit codes as `eval`: 1 the judge is not fit, 3 it could not be measured."""
    if len(report.degraded) > max_degraded:
        return CalibrationVerdict(
            False,
            EXIT_ERROR,
            f"{len(report.degraded)} record(s) could not be graded by the model judge "
            f"(max_degraded is {max_degraded}), so its agreement was not measured.",
        )
    kappa = report.overall.kappa
    if kappa is None:
        return CalibrationVerdict(
            False,
            EXIT_FAILED,
            "kappa is undefined: the humans and the judge all used the same single label, so "
            "there is no agreement beyond chance to measure. The set needs both acceptable "
            "and unacceptable cases. An unmeasured judge does not clear the gate.",
        )
    if kappa < min_kappa:
        return CalibrationVerdict(
            False,
            EXIT_FAILED,
            f"kappa {kappa:.2f} is below min_kappa {min_kappa:.2f}: this judge is not fit to gate a merge.",
        )
    return CalibrationVerdict(
        True, EXIT_OK, f"kappa {kappa:.2f} meets min_kappa {min_kappa:.2f}."
    )
