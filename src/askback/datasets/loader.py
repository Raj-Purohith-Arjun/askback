"""Dataset loading for YAML, JSON, CSV and Excel.

One reader per format, one public function. The suite name defaults to the file
stem, so `suites/adversarial.yaml` produces cases in the `adversarial`
suite without repeating it on every row.

A YAML or JSON suite file declares its `schema_version`. A version this loader
does not know is refused, never guessed at: read under the wrong schema, a
case's expectations land in keys nobody looks at and the case passes empty.
Tabular files carry no version. A row is one turn, and that cannot change.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import DatasetError
from ..core.logging import get_logger
from .schema import READABLE_VERSIONS, SCHEMA_VERSION, Expectations, TestCase

log = get_logger(__name__)

SUPPORTED_SUFFIXES = (".yaml", ".yml", ".json", ".csv", ".xlsx")

#: Older suite-file version -> the script that converts it to the next one.
MIGRATIONS = {1: "scripts/migrate_v1_v2.py"}

_SUITE_KEYS = {"schema_version", "defaults", "cases"}


@dataclass(frozen=True, slots=True)
class _SuiteFile:
    """What a reader got out of one file, before any case is built."""

    rows: list[dict[str, Any]]
    #: Flat single-turn rows: a tabular file.
    flat: bool
    #: The suite's `defaults`, merged under each turn's own expectations.
    defaults: dict[str, Any] = field(default_factory=dict)
    #: The `schema_version` the file declared; a tabular file declares none.
    version: int = 2


def load_cases(
    path: str | Path,
    *,
    suite: str | None = None,
    principals: Mapping[str, tuple[str, str]] | None = None,
    default_samples: int = 1,
) -> list[TestCase]:
    """Load test cases from one file, or from every dataset file in a directory.

    With `principals` (label -> (user_id, tenant_id), as the adapter resolves
    them) a case that declares a `tenant_id` the mapping does not give its
    `user_id` is refused. Without it the check is skipped: the loader cannot
    know who a case will run as.

    `default_samples` is the N of a case that does not declare `samples`.
    """
    target = Path(path)
    if target.is_dir():
        files = sorted(
            f for f in target.iterdir() if f.suffix.lower() in SUPPORTED_SUFFIXES
        )
        if not files:
            raise DatasetError(f"No dataset files ({', '.join(SUPPORTED_SUFFIXES)}) in {target}")
        cases: list[TestCase] = []
        for file in files:
            cases.extend(
                load_cases(file, principals=principals, default_samples=default_samples)
            )
        return cases

    if not target.exists():
        raise DatasetError(f"Dataset not found: {target}")

    readers: dict[str, Callable[[Path], _SuiteFile]] = {
        ".yaml": _read_yaml,
        ".yml": _read_yaml,
        ".json": _read_json,
        ".csv": _read_csv,
        ".xlsx": _read_xlsx,
    }
    reader = readers.get(target.suffix.lower())
    if reader is None:
        raise DatasetError(
            f"Unsupported dataset format {target.suffix!r}. "
            f"Use one of: {', '.join(SUPPORTED_SUFFIXES)}"
        )

    default_suite = suite or target.stem
    suite_file = reader(target)
    rows = suite_file.rows
    try:
        cases = [
            TestCase.from_row(row, suite=default_suite, default_samples=default_samples)
            if suite_file.flat
            else TestCase.from_dict(
                row,
                suite=default_suite,
                defaults=suite_file.defaults,
                default_samples=default_samples,
                schema_version=suite_file.version,
            )
            for row in rows
        ]
    except DatasetError as exc:
        raise DatasetError(f"{target}: {exc}") from exc
    _assert_unique_ids(cases, target)
    if principals is not None:
        _assert_tenants_match(cases, rows, principals, target)
    log.info("Loaded %d cases from %s", len(cases), target.name)
    return cases


# --------------------------------------------------------------------------- #
# Format readers. Each returns plain dicts; TestCase does the parsing.
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> _SuiteFile:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DatasetError("YAML datasets need PyYAML. Run: pip install pyyaml") from exc
    return _suite_from(yaml.safe_load(path.read_text(encoding="utf-8")) or [], path)


def _read_json(path: Path) -> _SuiteFile:
    return _suite_from(json.loads(path.read_text(encoding="utf-8")), path)


def _read_csv(path: Path) -> _SuiteFile:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return _SuiteFile([dict(row) for row in csv.DictReader(handle)], flat=True)


def _read_xlsx(path: Path) -> _SuiteFile:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DatasetError("Excel datasets need openpyxl. Run: pip install openpyxl") from exc

    sheet = load_workbook(path, read_only=True, data_only=True).active
    rows = sheet.iter_rows(values_only=True)
    try:
        header = [str(c).strip() if c is not None else "" for c in next(rows)]
    except StopIteration:
        return _SuiteFile([], flat=True)
    return _SuiteFile(
        [
            {key: value for key, value in zip(header, values, strict=False) if key}
            for values in rows
            if any(v is not None for v in values)
        ],
        flat=True,
    )


def _suite_from(data: Any, path: Path) -> _SuiteFile:
    """Read a parsed YAML/JSON document under the schema version it declares."""
    # A bare list has nowhere to declare a version, so it can only be v1: refused below.
    declared = data.get("schema_version", 1) if isinstance(data, dict) else 1
    # `True == 1` and `2.0 == 2`, and neither is a version.
    version = declared if type(declared) is int else None

    if version in READABLE_VERSIONS:
        return _suite_v2(data, path, version)
    if version is not None and version > SCHEMA_VERSION:
        advice = (
            "It was written for a newer askback: upgrade the package. "
            "No migration goes backwards."
        )
    else:
        advice = (
            "Suite files from before versioning are v1; convert one with: "
            f"python {MIGRATIONS[1]} {path}"
        )
    raise DatasetError(
        f"{path}: schema_version {declared!r} is not one this loader understands "
        f"(it reads {SCHEMA_VERSION}). {advice}"
    )


def _suite_v2(data: dict[str, Any], path: Path, version: int) -> _SuiteFile:
    """A v2 or v3 suite: the same top level, and v3 only adds optional case and turn keys."""
    # A leading underscore is the only way a JSON file can carry a comment.
    unknown = sorted(k for k in data if k not in _SUITE_KEYS and not str(k).startswith("_"))
    if unknown:
        raise DatasetError(
            f"{path}: unknown top-level key(s) {unknown}. "
            f"A suite file has {', '.join(sorted(_SUITE_KEYS))}."
        )

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise DatasetError(f"{path}: `defaults` must be a mapping of expectations, got {defaults!r}")
    # Checked once here: left to the merge, a typo would be blamed on every turn.
    Expectations.from_dict(defaults, where=f"{path}: defaults")

    cases = data.get("cases")
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise DatasetError(f"{path}: `cases` must be a list of case mappings.")
    return _SuiteFile(cases, flat=False, defaults=defaults, version=version)


def _assert_unique_ids(cases: Sequence[TestCase], path: Path) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise DatasetError(f"{path}: duplicate case id {case.id!r}")
        seen.add(case.id)


def _assert_tenants_match(
    cases: Sequence[TestCase],
    rows: Sequence[dict[str, Any]],
    principals: Mapping[str, tuple[str, str]],
    path: Path,
) -> None:
    """Refuse a declared `tenant_id` that is not the one the case will run as.

    The runner sends only `user_id`; the tenant comes from the principal
    mapping. A case written as `user_id: test_user, tenant_id: acme` would
    therefore run as tenant `default` and pass or fail for the wrong reason.
    Checked against the raw row because `TestCase.tenant_id` defaults to
    "default" and cannot say whether the author wrote it. A `user_id` the
    mapping does not know is left alone: nothing to compare, and the run
    reports that case as unrunnable.
    """
    for case, row in zip(cases, rows, strict=True):
        declared = row.get("tenant_id")
        if declared is None or declared == "" or case.user_id not in principals:
            continue
        resolved = principals[case.user_id][1]
        if str(declared) != resolved:
            raise DatasetError(
                f"{path}: case {case.id!r} declares tenant_id {str(declared)!r}, but "
                f"user_id {case.user_id!r} resolves to tenant {resolved!r} in "
                "agent.principals. Cases run as the principal's tenant; fix the case "
                "or the mapping."
            )


def filter_cases(
    cases: Sequence[TestCase],
    *,
    suites: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    ids: Sequence[str] | None = None,
) -> list[TestCase]:
    """Narrow a case list. Empty or None filters mean "keep everything"."""
    selected = list(cases)
    if suites:
        wanted = {s.lower() for s in suites}
        selected = [c for c in selected if c.suite.lower() in wanted]
    if tags:
        wanted = {t.lower() for t in tags}
        selected = [c for c in selected if wanted & {t.lower() for t in c.tags}]
    if ids:
        wanted = set(ids)
        selected = [c for c in selected if c.id in wanted]
    return selected
