"""On-disk cache of judge verdicts.

A verdict is a pure function of what the judge was asked and who was asked, so a
re-run of an unchanged suite should not pay for it twice. The key is the system
prompt, the user prompt, the model and the prompt version: change any of them and
the old verdict is, correctly, not found. The prompt version is there because the
system prompt is not the whole instruction - parsing and scale live in code, and
a change to them must be able to retire verdicts the prompt text alone would not.

One file per verdict, written by rename, so the runner's threads never read half a
file and a crash never leaves one. A file that cannot be used - unreadable,
corrupt, missing a dimension - is a miss and is logged, never an error: the cost
of a bad cache entry is one more call, and the call is honest about what it cost.

Only model verdicts are stored. A degraded (heuristic) result is a different
scoring regime and must never be served back as if the model had said it.

Of a verdict, only the scores are stored. The rationale is the judge's prose
about the reply it graded and quotes it freely, and this directory travels where
a report does not: CI restores it into pull-request workflows, forks included,
and no redaction layer stands in front of it. A hit therefore has no rationale to
give, and the judge says so rather than have one persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from ..core.logging import get_logger

log = get_logger(__name__)

Scores = dict[str, float]


class VerdictCache:
    """Stores the scores per (system, user, model, prompt_version)."""

    def __init__(self, directory: str | Path, dimensions: Sequence[str]) -> None:
        self.directory = Path(directory)
        self.dimensions = tuple(dimensions)

    @staticmethod
    def key(system: str, user: str, model: str, prompt_version: str) -> str:
        # JSON, not a joined string: no choice of separator can make two
        # different tuples serialise alike.
        payload = json.dumps([system, user, model, prompt_version], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Scores | None:
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("Judge cache entry %s is unreadable (%s); treating as a miss.", path, exc)
            return None

        scores = data.get("scores") if isinstance(data, dict) else None
        if not isinstance(scores, dict) or not all(
            isinstance(scores.get(d), (int, float)) and not isinstance(scores.get(d), bool)
            for d in self.dimensions
        ):
            log.warning("Judge cache entry %s is malformed; treating as a miss.", path)
            return None
        # Nothing else in the file is read: an entry from before the rationale was
        # dropped still carries one, and it stays on disk until the entry is deleted.
        return {d: float(scores[d]) for d in self.dimensions}

    def put(self, key: str, scores: Scores) -> None:
        path = self._path(key)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"scores": scores}, handle)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # The verdict was already paid for and is returned to the caller; only
            # the saving is lost.
            log.warning("Could not write judge cache entry %s (%s); it will be re-paid.", path, exc)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"
