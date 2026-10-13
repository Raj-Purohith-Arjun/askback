"""Config loading: YAML (or JSON) + ${ENV_VAR:default} expansion.

The same config file works locally and on a server; only environment variables
change. That is the whole local -> server migration story.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

# Matches ${NAME} and ${NAME:fallback value}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")

def _detect_project_root() -> Path:
    """The directory relative paths in a config file are read from.

    Three cases, checked in order:

    1. `ASKBACK_HOME` is set. Always wins.
    2. A source checkout, where this file is at <root>/src/askback/core/ and the
       root holds `pyproject.toml`.
    3. Anything else - notably the package installed into site-packages, where
       walking up from `__file__` lands in the virtualenv rather than the
       project. Fall back to the working directory, where your suites and config are.
    """
    explicit = os.getenv("ASKBACK_HOME")
    if explicit:
        return Path(explicit).resolve()

    checkout = Path(__file__).resolve().parents[3]
    if (checkout / "pyproject.toml").is_file():
        return checkout

    return Path.cwd()


PROJECT_ROOT = _detect_project_root()


def _expand(value: Any) -> Any:
    """Recursively substitute ${ENV_VAR:default} inside strings."""
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            return os.getenv(m.group(1), m.group(2) if m.group(2) is not None else "")

        expanded = _ENV_PATTERN.sub(sub, value)
        return _coerce(expanded) if expanded != value else value
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _coerce(text: str) -> Any:
    """Turn env-var strings into bool/int/float where the intent is unambiguous."""
    low = text.strip().lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"null", "none"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def load_mapping(path: str | Path) -> dict[str, Any]:
    """Read a YAML or JSON file into a dict, with env expansion applied."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    text = p.read_text(encoding="utf-8")

    if p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ConfigError(
                "Reading YAML needs PyYAML. Run: pip install pyyaml "
                "(or supply a .json file instead)."
            ) from exc
        data = yaml.safe_load(text) or {}

    if not isinstance(data, dict):
        raise ConfigError(f"{p} must contain a mapping at the top level.")
    expanded: dict[str, Any] = _expand(data)
    return expanded


@dataclass(slots=True)
class AppConfig:
    """Typed view over a config file. Unrecognised keys stay available in `raw`."""

    llm: dict[str, Any] = field(default_factory=lambda: {"provider": "extractive"})
    retrieval: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    evaluation: dict[str, Any] = field(default_factory=dict)
    server: dict[str, Any] = field(default_factory=dict)
    logging: dict[str, Any] = field(default_factory=lambda: {"level": "INFO"})
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AppConfig:
        """Build from an already-parsed mapping, keeping every key in `raw`."""
        known = {f for f in cls.__slots__ if f != "raw"}
        return cls(**{k: v for k, v in data.items() if k in known}, raw=dict(data))

    @classmethod
    def load(cls, path: str | Path | None = None) -> AppConfig:
        """Load the config at `path`, or at `ASKBACK_CONFIG`; with neither, the built-in demo.

        A path that is given and does not exist is an error. It used to fall back to
        defaults, which turned a typo in `--config` into a run of something else.
        """
        if not path:
            path = os.environ.get("ASKBACK_CONFIG") or None
        if not path:
            from ..demo import demo_config

            return cls.from_mapping(demo_config())
        return cls.from_mapping(load_mapping(path))

    @staticmethod
    def is_builtin(path: str | Path | None = None) -> bool:
        """True when `load(path)` would return the built-in demo rather than a file."""
        return not (path or os.environ.get("ASKBACK_CONFIG"))

    def section(self, name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch any section by name, including ones not modelled as fields above."""
        value = getattr(self, name, None)
        if value is None:
            value = self.raw.get(name, default or {})
        return value if isinstance(value, dict) else (default or {})


def default_samples(evaluation: Mapping[str, Any]) -> int:
    """`evaluation.default_samples`: how often a case that does not say is run.

    Refused rather than coerced when it is not a whole number of at least 1: a
    `0` or `"three"` read as 1 would switch sampling off with no word, and a
    flake measurement that was asked for would never happen.
    """
    value = evaluation.get("default_samples", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            f"evaluation.default_samples must be a whole number of at least 1, got {value!r}."
        )
    return value


def judge_number(judge: Mapping[str, Any], key: str) -> float | None:
    """A non-negative number from `evaluation.judge`, or None when it is not set.

    Refused rather than coerced: a price read as 0 from a typo would make a
    metered judge look free, and a negative budget would stop a run before it began.
    """
    value = judge.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"evaluation.judge.{key} must be a number >= 0, got {value!r}.")
    return float(value)


def _judge_block(evaluation: Mapping[str, Any]) -> Mapping[str, Any]:
    block = evaluation.get("judge")
    return block if isinstance(block, Mapping) else {}


def max_degraded(evaluation: Mapping[str, Any]) -> int:
    """`evaluation.judge.max_degraded`: judged responses allowed to fall back to the heuristic.

    Zero unless raised, so a judge outage fails the run by default. Refused when
    it is not a whole number >= 0: a value read as "unlimited" would let a run
    graded mostly by the wrong judge pass.
    """
    value = _judge_block(evaluation).get("max_degraded", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"evaluation.judge.max_degraded must be a whole number >= 0, got {value!r}."
        )
    return value


def judge_budget(evaluation: Mapping[str, Any]) -> float | None:
    """`evaluation.judge.budget_usd`: the most a run may spend on the judge. None means no ceiling."""
    return judge_number(_judge_block(evaluation), "budget_usd")


def min_kappa(evaluation: Mapping[str, Any]) -> float:
    """`evaluation.judge.min_kappa`: the agreement with humans below which the judge is not fit to gate.

    0.6 unless set. Refused outside [0, 1]: a negative bar passes every judge,
    and one above 1 fails them all, and neither is what anyone meant to write.
    """
    value = judge_number(_judge_block(evaluation), "min_kappa")
    if value is None:
        return 0.6
    if value > 1:
        raise ConfigError(f"evaluation.judge.min_kappa must be between 0 and 1, got {value!r}.")
    return value


def resolve_path(value: str | Path) -> Path:
    """Resolve a config path against the project root unless it is absolute."""
    p = Path(value)
    return p if p.is_absolute() else (PROJECT_ROOT / p)
