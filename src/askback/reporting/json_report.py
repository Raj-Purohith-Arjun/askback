"""JSON report writer. The canonical report.

The other formats are rendered from a report read back out of this one, so the
JSON is never behind them and never says something they do not.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.logging import get_logger
from ..evaluation.results import RunReport
from .redaction import DEFAULT_MODE, redacted

log = get_logger(__name__)


def write_json(report: RunReport, path: str | Path, *, redaction: str = DEFAULT_MODE) -> Path:
    """Write the report, every check included, to `path`, redacted to `redaction`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(redacted(report, redaction).to_dict(), indent=2), encoding="utf-8"
    )
    log.info("JSON report written to %s", target)
    return target
