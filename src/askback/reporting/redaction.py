"""Redaction: what a report may carry off the machine that produced it.

The runner keeps everything - whole replies, the passages the agent retrieved -
because evaluators need it. A report file is a different thing: it is uploaded
as a CI artifact, attached to a ticket, committed as a baseline. So every writer
in this package renders a redacted copy, and the mode says how much is left:

  full          nothing withheld. For the machine the run happened on; refused
                when the `CI` environment variable is set.
  ci            the default. PII and credential matches are replaced by their
                category, replies are cut to `REPLY_LIMIT` characters, and
                retrieved passage text is withheld entirely: those are internal
                source documents, and a report is not where they are published.
                Check details are cut like replies, because some quote them.
  metrics_only  verdicts, scores, check names and ids. No text from the agent,
                the dataset or an evaluator's detail.

What `ci` guarantees is narrower than "no internal content", and the difference
matters. It guarantees no PII match, no credential match and no retrieved chunk.
It does NOT guarantee the reply excerpt is free of internal content: an agent
that answers from its corpus puts corpus-derived sentences in its reply, and up
to `REPLY_LIMIT` characters of that reply are kept so a failure can be
recognised. Where that excerpt is itself too much - a regulated deployment, or a
health or finance one, where the answer to a customer's question is the sensitive
thing - the setting is `metrics_only`.

This closes a gap the PII gate could not: the evaluator records categories and
never the matched value, but the reply it matched in was copied into the JSON
and HTML whole, so a detected leak was reproduced in the artifact reporting it.

Redaction works on the JSON form and walks all of it. A field nobody listed is
scrubbed in `ci` and withheld in `metrics_only`, so a new free-text field is
redacted until someone decides otherwise, not published until someone notices.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from ..core.errors import ConfigError
from ..core.text import tolerate_line_breaks
from ..evaluation.patterns import SECRET_PATTERNS
from ..evaluation.pii_eval import redact_pii
from ..evaluation.results import RunReport

#: Weakest first. A report's mode only ever moves right.
MODES = ("full", "ci", "metrics_only")
DEFAULT_MODE = "ci"

#: Enough of a reply to recognise which failure it was; not a transcript.
REPLY_LIMIT = 200
WITHHELD = "[withheld]"

_SECRET_RULES = [
    (label, re.compile(tolerate_line_breaks(pattern))) for label, pattern in SECRET_PATTERNS
]

# What joins a run to its baseline. Authored in the dataset, never agent output,
# and a pattern that happened to match one would silently unpair the two runs.
_IDENTITY_KEYS = frozenset({"run_id", "case_id", "suite"})
# Cut to REPLY_LIMIT in `ci`. A detail is here because evaluators quote the reply
# in it (groundedness lists every unsupported sentence), so cutting only the
# reply would leave most of it one field further down.
_CUT_KEYS = frozenset({"reply", "detail"})
# Passage text, wherever it appears: a retrieved chunk's body, a citation's quote.
_SOURCE_TEXT_KEYS = frozenset({"text", "snippet"})
# The only case fields `metrics_only` lets through. Containers are walked;
# anything not named here is withheld.
_METRIC_KEYS = frozenset({
    "case_id", "suite", "risk", "tags", "score", "passed", "route", "status", "latency_ms",
    "samples_planned", "min_passing_samples", "passing_samples", "flaky", "index",
    "session_id", "evaluated", "evaluator", "skipped", "judge", "name", "severity",
})
_METRIC_CONTAINERS = frozenset({"results", "checks", "turns", "samples"})


def resolve_mode(evaluation: Mapping[str, Any], override: str | None = None) -> str:
    """The mode a run writes its reports in: the flag, else `evaluation.redaction`, else `ci`.

    Refused rather than coerced when it is not a known mode: a typo read as
    `full` would publish transcripts, and one read as `ci` would hide that the
    setting was never applied. `full` is refused under CI whoever asked for it,
    because there the report is an artifact other people can download.
    """
    mode = override if override is not None else evaluation.get("redaction", DEFAULT_MODE)
    if mode not in MODES:
        raise ConfigError(f"redaction must be one of {', '.join(MODES)}; got {mode!r}.")
    if mode == "full" and os.environ.get("CI", "").strip().lower() not in {"", "0", "false"}:
        raise ConfigError(
            "redaction 'full' is for local runs only and CI is set: the report would be "
            "uploaded with complete replies and source passages in it. Use 'ci'."
        )
    return str(mode)


def redacted(report: RunReport, mode: str = DEFAULT_MODE) -> RunReport:
    """`report` as a file may carry it: a copy, read back out of its redacted JSON.

    Every writer renders this and never the report it was handed, which is also
    what makes the JSON canonical: the HTML and the workbook show what a reader
    of the JSON would get from `RunReport.from_dict`, because that is how they
    got it.
    """
    return RunReport.from_dict(redact_report(report.to_dict(), mode))


def redact_report(data: Mapping[str, Any], mode: str = DEFAULT_MODE) -> dict[str, Any]:
    """A redacted copy of a report's JSON form, labelled with the mode it now meets.

    The label only strengthens. Asking for `full` of a `ci` report returns it as
    it is, still labelled `ci`: what was withheld cannot be put back, and the
    file must not claim it was.
    """
    if mode not in MODES:
        raise ConfigError(f"redaction must be one of {', '.join(MODES)}; got {mode!r}.")
    current = str(data.get("redaction", "full"))
    if current not in MODES:
        raise ConfigError(f"report is labelled with an unknown redaction mode {current!r}.")

    out: dict[str, Any] = {**data, "redaction": current}
    # Steps a report already went through are not repeated: cutting a cut reply
    # again would stack a second marker on it.
    for step in MODES[MODES.index(current) + 1 : MODES.index(mode) + 1]:
        if step == "ci":
            out = {key: _scrub(value, key) for key, value in out.items()}
        else:
            out["cases"] = [_metrics(case) for case in out.get("cases", [])]
        out["redaction"] = step
    return out


def _scrub_text(text: str) -> str:
    text = redact_pii(text)
    for label, rule in _SECRET_RULES:
        text = rule.sub(f"[REDACTED:{label}]", text)
    return text


def _scrub(value: Any, key: str = "") -> Any:
    """The `ci` pass over one value. `key` is the field it was found under."""
    if key in _IDENTITY_KEYS:
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, key) for v in value]
    if not isinstance(value, str) or not value:
        return value
    if key in _SOURCE_TEXT_KEYS:
        return WITHHELD
    text = _scrub_text(value)
    # Cut after scrubbing: a cut that landed inside an address would leave a
    # fragment no pattern matches any more.
    if key in _CUT_KEYS and len(text) > REPLY_LIMIT:
        return f"{text[:REPLY_LIMIT].rstrip()} [+{len(text) - REPLY_LIMIT} chars withheld]"
    return text


def _metrics(value: Any, key: str = "") -> Any:
    """The `metrics_only` pass. Schema-preserving: fields are emptied, not removed.

    A withheld string becomes a marker rather than "", so a reader cannot take
    "no reply was shown" for "there was no reply", and a case that errored still
    counts as one.
    """
    if key in _METRIC_KEYS or value is None:
        return value
    if isinstance(value, dict):
        return {k: _metrics(v, k) for k, v in value.items()} if _walked(key) else {}
    if isinstance(value, list):
        return [_metrics(v, key) for v in value] if _walked(key) else []
    return (WITHHELD if value else "") if isinstance(value, str) else None


def _walked(key: str) -> bool:
    # "" is a case, turn, sample, result or check itself, reached through a list.
    return key == "" or key in _METRIC_CONTAINERS


__all__ = [
    "DEFAULT_MODE", "MODES", "REPLY_LIMIT", "WITHHELD", "redact_report", "redacted", "resolve_mode",
]
