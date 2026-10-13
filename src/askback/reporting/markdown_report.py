"""Markdown summary: the run in a screenful, for a CI job summary.

Not a fourth copy of the report. It carries the numbers a reviewer decides on -
pass rate, critical failures, what the judge cost - and names each failing
critical check by case. It quotes no reply and no check detail in any mode, so
it can be posted where the report itself would not be.

Gate verdicts are not here: the gates are evaluated after the reports are
written, and the command prints them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..evaluation.results import RunReport
from .redaction import DEFAULT_MODE, redacted

log = get_logger(__name__)

#: A run with more failing cases than this has a problem the list will not help with.
_MAX_LISTED = 40


def write_markdown(report: RunReport, path: str | Path, *, redaction: str = DEFAULT_MODE) -> Path:
    """Write the summary of the report, redacted to `redaction`, to `path`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(redacted(report, redaction)), encoding="utf-8")
    log.info("Markdown summary written to %s", target)
    return target


def render_markdown(report: RunReport) -> str:
    summary = report.summary()
    lines = [f"## Evaluation run `{report.run_id}`", ""]

    if report.incomplete is not None:
        stopped = report.incomplete
        lines += [
            f"**Incomplete: no verdict.** {stopped.get('cases_run')} of "
            f"{stopped.get('cases_planned')} cases ran before the run stopped "
            f"({stopped.get('reason')}). The numbers below describe the cases that "
            "happened to run first, not the suite.",
            "",
        ]

    lines += [
        "| | |",
        "|---|---|",
        f"| Pass rate | **{summary['pass_rate']:.0%}** ({summary['passed']} of {summary['total']}) |",
        f"| Critical failures | **{summary['critical_failures']}** |",
        f"| Mean score | {summary['mean_score']:.2f} |",
    ]
    if "flaky" in summary:
        lines.append(f"| Flaky cases | {summary['flaky']} |")
    lines += [
        f"| Judge | `{report.context.get('judge', 'not recorded')}` |",
        f"| Estimated judge cost | {_cost(summary)} |",
        f"| Redaction | `{report.redaction}` |",
        "",
        "| Suite | Cases | Passed | Pass rate | Mean score |",
        "|---|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {suite} | {row['total']} | {row['passed']} | {row['pass_rate']:.0%} | {row['mean_score']:.2f} |"
        for suite, row in report.by_suite().items()
    ]

    critical = [
        f"- `{case.suite}/{case.case_id}`: {', '.join(dict.fromkeys(c.name for c in case.critical_checks))}"
        for case in report.cases
        if case.critical_checks
    ]
    if critical:
        lines += ["", f"### Cases with a failing critical check ({len(critical)})", ""]
        lines += critical[:_MAX_LISTED]
        if len(critical) > _MAX_LISTED:
            lines.append(f"- and {len(critical) - _MAX_LISTED} more, in the JSON report")
    return "\n".join(lines) + "\n"


def _cost(summary: dict[str, Any]) -> str:
    # No metered judge is not a judge that cost $0.00: say which it was.
    if "judge_calls" not in summary:
        return "none: no metered judge graded this run"
    cached = f", {summary['judge_cache_hits']} cached" if summary["judge_cache_hits"] else ""
    degraded = (
        f", **{summary['judge_degraded']} degraded to the heuristic**"
        if summary["judge_degraded"]
        else ""
    )
    return f"${summary['judge_cost_usd']:.4f} ({summary['judge_calls']} calls{cached}{degraded})"
