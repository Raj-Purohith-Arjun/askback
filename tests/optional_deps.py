"""Optional test dependencies, declared rather than silently skipped.

`pytest.importorskip` is how `tests/test_api.py` dropped 40 tests out of a green
run: a module-level skip produces no failing dot, and the summary line still
reads as if everything was tested. This framework exists to stop exactly that -
something unmeasured reported as a pass - so its own suite is held to the rule.

A missing optional dependency is reported loudly at the end of every run. Under
`--strict-env` (or `ASKBACK_STRICT_ENV=1`, which is what CI sets) it is a hard
collection error instead, because in CI there is nobody reading the tail of the
output to notice.
"""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

#: Recorded collection baseline. Minimums, not exact counts: adding tests must
#: never break the gate, losing them must always break it.
BASELINE_PATH = Path(__file__).with_name("expected_tests.json")

INSTALL_HINT = 'pip install -e ".[all]"'


@dataclass(frozen=True, slots=True)
class MissingDependency:
    """One test module that could not be imported, and what it wanted."""

    test_module: str
    dependency: str
    extra: str


#: Populated during collection by `requires()`, read by the terminal summary and
#: by the strict-mode environment tests.
MISSING: list[MissingDependency] = []

#: Test counts per module, populated from the final collected item list.
COLLECTED: dict[str, int] = {}

#: True when the invocation narrowed the run - explicit paths, -k or -m. The
#: collection baseline cannot apply then: a subset is meant to collect less.
#: Same carve-out as the per-suite floors in evaluation gates, for the same
#: reason - an assertion that cannot be measured must say so, not fail or pass.
SELECTION: dict[str, bool] = {"subset": False}

_strict = False


def set_strict(strict: bool) -> None:
    global _strict
    _strict = strict


def is_strict() -> bool:
    return _strict


def subset_selected() -> bool:
    return SELECTION["subset"]


def resolve_strict(flag: bool) -> bool:
    """`--strict-env`, or `ASKBACK_STRICT_ENV` set to anything but a falsy word."""
    if flag:
        return True
    return os.getenv("ASKBACK_STRICT_ENV", "").strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }


def load_baseline() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return data


def requires(dependency: str, *, extra: str, test_module: str) -> ModuleType:
    """Import an optional dependency, or refuse to pretend this module ran.

    Returns the imported module so the call reads like `importorskip` at the top
    of a test file. Raises in strict mode; skips the module and records why
    otherwise.
    """
    try:
        return importlib.import_module(dependency)
    except ImportError as exc:
        MISSING.append(MissingDependency(test_module, dependency, extra))
        message = (
            f"{test_module} needs '{dependency}' from the [{extra}] extra. "
            f"Install it with: {INSTALL_HINT}"
        )
        if is_strict():
            raise RuntimeError(
                f"strict-env: {message} Strict mode refuses to skip a test module, "
                f"because a skipped module is indistinguishable from a passing one."
            ) from exc
        pytest.skip(message, allow_module_level=True)
