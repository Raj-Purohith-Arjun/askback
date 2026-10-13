"""Self-contained HTML report.

No external CSS, fonts or scripts: the file opens from disk, from a build
artefact store or from an email attachment with no network. Everything is
inlined, and the only interactivity uses native <details> elements.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..evaluation.base import EvalResult
from ..evaluation.results import CaseResult, RunReport, SampleResult, TurnResult
from .redaction import DEFAULT_MODE, redacted

log = get_logger(__name__)

_STYLE = """
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #14181f; --muted: #5b6472;
  --line: #e2e6ec; --pass: #17794a; --fail: #c02a2a; --warn: #9a6700;
  --pass-bg: #e8f5ee; --fail-bg: #fdeaea; --accent: #2c5fd4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c; --panel: #1b1f26; --ink: #e8ecf2; --muted: #9aa4b2;
    --line: #2a303a; --pass: #4ec98a; --fail: #ff7a7a; --warn: #e0b341;
    --pass-bg: #16281f; --fail-bg: #2a1a1c; --accent: #7aa2ff;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: var(--bg); color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); margin: 32px 0 12px; }
.sub { color: var(--muted); margin: 0 0 24px; font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.card .k { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.card .v { font-size: 26px; font-weight: 650; margin-top: 4px; }
.v.pass { color: var(--pass); } .v.fail { color: var(--fail); }
table { width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); font-weight: 600; }
tr:last-child td { border-bottom: none; }
.scroll { overflow-x: auto; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.pill.pass { background: var(--pass-bg); color: var(--pass); }
.pill.fail { background: var(--fail-bg); color: var(--fail); }
details { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  margin-bottom: 8px; padding: 10px 14px; }
details[open] { border-color: var(--accent); }
summary { cursor: pointer; display: flex; gap: 12px; align-items: baseline; font-weight: 600; }
summary::-webkit-details-marker { display: none; }
summary .id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--accent); min-width: 150px; }
summary .msg { font-weight: 400; flex: 1; }
.body { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 12px; }
.reply { background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 12px; margin: 8px 0 14px; white-space: pre-wrap; }
.meta { color: var(--muted); font-size: 12px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.chk-fail td { background: var(--fail-bg); }
footer { color: var(--muted); font-size: 12px; margin-top: 32px; }
"""

#: Only for a run that sampled, so a report of single draws is the file it always was.
_SAMPLING_STYLE = """
.v.warn { color: var(--warn); }
.pill.flaky { background: rgba(224, 179, 65, .18); color: var(--warn); }
"""


def write_html(
    report: RunReport,
    path: str | Path,
    *,
    title: str = "Evaluation Report",
    redaction: str = DEFAULT_MODE,
) -> Path:
    """Render the report, redacted to `redaction`, to a standalone HTML file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(redacted(report, redaction), title=title), encoding="utf-8")
    log.info("HTML report written to %s", target)
    return target


def render_html(report: RunReport, *, title: str = "Evaluation Report") -> str:
    """Render the report exactly as given. Redacting it is `write_html`'s job."""
    summary = report.summary()
    verdict = "pass" if summary["failed"] == 0 else "fail"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{_STYLE}{_SAMPLING_STYLE if report.sampled else ""}</style></head>
<body><div class="wrap">
<h1>{escape(title)}</h1>
<p class="sub">Run <code>{escape(report.run_id)}</code> &middot; {escape(report.finished_at or report.started_at)}
 &middot; {report.duration_s:.1f}s &middot; {escape(str(report.context.get('llm_provider', 'n/a')))} provider
 &middot; redaction <b>{escape(report.redaction)}</b></p>

<div class="cards">
  {_card("Cases", summary["total"])}
  {_card("Passed", summary["passed"], "pass")}
  {_card("Failed", summary["failed"], "fail" if summary["failed"] else "")}
  {_card("Pass rate", f"{summary['pass_rate'] * 100:.0f}%", verdict)}
  {_card("Mean score", f"{summary['mean_score']:.2f}")}
  {_card("Critical fails", summary["critical_failures"], "fail" if summary["critical_failures"] else "")}{_flaky_card(summary)}
  {_card("Mean latency", f"{summary['mean_latency_ms']:.0f} ms")}
</div>

<h2>By suite</h2>
<div class="scroll">{_table(
    ["Suite", "Cases", "Passed", "Pass rate", "Mean score"],
    [[s, d["total"], d["passed"], f"{d['pass_rate'] * 100:.0f}%", f"{d['mean_score']:.2f}"]
     for s, d in report.by_suite().items()])}</div>

<h2>By evaluator</h2>
<div class="scroll">{_table(
    ["Evaluator", "Cases scored", "Mean score", "Failed"],
    [[n, d["cases"], f"{d['mean_score']:.2f}", d["failed"]]
     for n, d in report.by_evaluator().items()])}</div>

{_failing_checks_section(report)}

<h2>Cases</h2>
{"".join(_case_block(c) for c in report.cases)}

<footer>Generated by askback &middot;
context: {escape(_context_line(report.context))}</footer>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# Fragments
# --------------------------------------------------------------------------- #


def _card(label: str, value: Any, tone: str = "") -> str:
    return (
        f'<div class="card"><div class="k">{escape(label)}</div>'
        f'<div class="v {tone}">{escape(str(value))}</div></div>'
    )


def _flaky_card(summary: dict[str, Any]) -> str:
    """Present only for a run that sampled: otherwise there is nothing it measured."""
    if "flaky" not in summary:
        return ""
    return "\n  " + _card("Flaky", summary["flaky"], "warn" if summary["flaky"] else "")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return '<p class="meta">Nothing to show.</p>'
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _failing_checks_section(report: RunReport) -> str:
    failing = report.failing_checks()
    if not failing:
        return ""
    return (
        "<h2>Most frequent failing checks</h2>"
        f'<div class="scroll">{_table(["Check", "Failures"], [[k, v] for k, v in failing.items()])}</div>'
    )


def _case_block(case: CaseResult) -> str:
    tone = "pass" if case.passed else "fail"
    label = "PASS" if case.passed else "FAIL"

    # A case run once is its case-level fields, exactly as before samples existed,
    # and a one-turn case exactly as before turns did. Only a real conversation is
    # laid out turn by turn, and only a sampled case sample by sample.
    if case.sampled:
        exchange = "\n  ".join(_sample_block(case, sample) for sample in case.samples)
    elif len(case.turns) > 1:
        exchange = "\n  ".join(_turn_block(turn) for turn in case.turns)
    else:
        exchange = (
            f'<div class="reply">{escape(case.reply or "(no reply)")}</div>\n'
            f"  {_checks_table(case.results)}"
        )

    flaky = (
        f' <span class="pill flaky">FLAKY {case.passing_samples}/{len(case.samples)}</span>'
        if case.flaky
        else ""
    )
    error = f'<p class="meta" style="color:var(--fail)">Execution error: {escape(case.error)}</p>' if case.error else ""
    return f"""<details{" open" if not case.passed else ""}>
<summary><span class="id">{escape(case.case_id)}</span>
<span class="pill {tone}">{label} {case.score:.2f}</span>{flaky}
<span class="msg">{escape(case.message)}</span></summary>
<div class="body">
  <p class="meta">suite <b>{escape(case.suite)}</b> &middot; route <b>{escape(case.route or "-")}</b>
   &middot; status <b>{escape(case.status or "-")}</b> &middot; risk {escape(case.risk)}
   &middot; {case.latency_ms:.0f} ms{" &middot; tags: " + escape(", ".join(case.tags)) if case.tags else ""}</p>
  {error}
  {exchange}
</div></details>"""


def _sample_block(case: CaseResult, sample: SampleResult) -> str:
    """One run of a sampled case, headed by its own verdict and the gate it counts toward."""
    tone = "pass" if sample.passed else "fail"
    ran = len(case.samples)
    stopped = (
        f" &middot; stopped after {ran} of {case.samples_planned}: the gate could not be met"
        if sample.index == ran and ran < case.samples_planned
        else ""
    )
    head = (
        f'<p class="meta"><b>Sample {sample.index} of {case.samples_planned}</b> &middot; '
        f'<span class="pill {tone}">{tone.upper()} {sample.score:.2f}</span> &middot; '
        f"needs {case.min_passing_samples} passing{stopped}</p>"
    )
    return head + "\n  " + "\n  ".join(_turn_block(turn) for turn in sample.turns)


def _turn_block(turn: TurnResult) -> str:
    """One turn of a multi-turn case: what was asked, the reply, and its checks."""
    if turn.error:
        verdict = '<span class="pill fail">ERROR</span>'
    elif turn.passed is None:
        verdict = "setup turn, not graded"
    else:
        tone = "pass" if turn.passed else "fail"
        verdict = f'<span class="pill {tone}">{tone.upper()} {turn.score or 0.0:.2f}</span>'

    if turn.error:
        outcome = f'<p class="meta" style="color:var(--fail)">{escape(turn.error)}</p>'
    else:
        outcome = f'<div class="reply">{escape(turn.reply or "(no reply)")}</div>'
        if turn.evaluated:
            outcome += _checks_table(turn.results)

    return (
        f'<p class="meta"><b>Turn {turn.index}</b> &middot; {verdict} &middot; '
        f'route <b>{escape(turn.route or "-")}</b> &middot; status <b>{escape(turn.status or "-")}</b>'
        f" &middot; {turn.latency_ms:.0f} ms</p>"
        f"<p><b>User:</b> {escape(turn.user)}</p>{outcome}"
    )


def _checks_table(results: Sequence[EvalResult]) -> str:
    rows: list[str] = []
    for result in results:
        if result.skipped:
            rows.append(
                f'<tr><td><code>{escape(result.evaluator)}</code></td><td colspan="3" class="meta">'
                "skipped &mdash; the case declares nothing for this evaluator</td></tr>"
            )
            continue
        for check in result.checks:
            css = "" if check.passed else ' class="chk-fail"'
            rows.append(
                f"<tr{css}><td><code>{escape(result.evaluator)}.{escape(check.name)}</code></td>"
                f'<td><span class="pill {"pass" if check.passed else "fail"}">'
                f'{"pass" if check.passed else "fail"}</span></td>'
                f"<td>{check.score:.2f}</td><td>{escape(check.detail)}</td></tr>"
            )
    return f"""<div class="scroll"><table><thead><tr><th>Check</th><th>Result</th><th>Score</th><th>Detail</th></tr></thead>
  <tbody>{"".join(rows) or '<tr><td colspan="4" class="meta">No checks ran.</td></tr>'}</tbody></table></div>"""


def _context_line(context: dict[str, Any]) -> str:
    keys = ("llm_provider", "retriever", "corpus_chunks", "prompt_version", "pass_threshold")
    return ", ".join(f"{k}={context[k]}" for k in keys if k in context)
