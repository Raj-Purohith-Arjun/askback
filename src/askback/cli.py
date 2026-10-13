"""Command line interface.

    askback eval                    run the test suites and write reports
    askback calibrate               measure the judge's agreement with human labels
    askback baseline save           promote the latest run to the baseline

and, for the HR example simulator in a source checkout (they need --config):

    askback --config examples/hr/config.yaml info
    askback --config examples/hr/config.yaml ask "question"
    askback --config examples/hr/config.yaml chat
    askback --config examples/hr/config.yaml serve

`eval` exits 1 when the run fails any of its gates - global pass rate, critical
severity, or a per-suite floor - so it works as a CI gate. Exit 2 is a regression
against the baseline. Exit 3 means the run could not be judged (bad config, a floor
on a suite that did not run, an exhausted judge budget), which is an operator
problem, not a failing agent.
Built on argparse to keep the core dependency-free.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .adapters.factory import adapter_name, build_adapter, resolve_principals
from .core.config import (
    AppConfig,
    default_samples,
    judge_budget,
    max_degraded,
    min_kappa,
    resolve_path,
)
from .core.contracts import ChatRequest
from .core.errors import AskbackError
from .core.logging import get_logger, setup_logging
from .datasets import filter_cases, load_cases
from .evaluation.calibration import (
    CalibrationReport,
    calibrate,
    calibration_verdict,
    load_calibration,
)
from .evaluation.engine import EvaluationEngine
from .evaluation.gates import (
    EXIT_ERROR,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REGRESSION,
    RunVerdict,
    evaluate_gates,
)
from .evaluation.llm_judge import DEFAULT_PASS_THRESHOLD, build_judge
from .evaluation.regression import (
    CaseDelta,
    RegressionComparator,
    SuiteDelta,
    load_baseline,
    save_baseline,
)
from .evaluation.results import CaseResult, RunReport
from .reporting import MODES, redacted, resolve_mode, write_reports
from .runner import TestRunner
from .runner.responders import build_clarification

if TYPE_CHECKING:
    from askback_sim.engine import ConversationEngine

log = get_logger(__name__)

#: Re-exported from the gate module, which owns them: the exit code is part of
#: the verdict, not of the terminal.
__all__ = ["EXIT_ERROR", "EXIT_FAILED", "EXIT_OK", "EXIT_REGRESSION", "build_parser", "main"]


# --------------------------------------------------------------------------- #
# The simulator: optional, and imported only by the commands that run it
# --------------------------------------------------------------------------- #


def _simulator_missing(exc: ModuleNotFoundError) -> AskbackError:
    """Turn "the simulator is not installed" into an operator error that says so.

    Only when it is the simulator package that is missing. Any other missing
    module - a third-party dependency, or one of our own - is a different
    problem and is re-raised untouched, not reported as a missing extra.
    """
    if (exc.name or "").split(".")[0] != "askback_sim":
        raise exc
    return AskbackError(
        f"this command runs the example simulator, which is not part of the installed package "
        f"({exc.name} not found). Run it from a source checkout: pip install -e . "
        "and pass --config examples/hr/config.yaml"
    )


def _require_simulator_config(config: AppConfig | str | Path | None) -> None:
    """The simulator needs its own config. With none given there is nothing to run, and no
    default: it used to be the HR simulator, and a bare command must not silently run that."""
    if config is None and AppConfig.is_builtin(None):
        raise AskbackError(
            "this command drives the example HR simulator and needs its config: "
            "pass --config examples/hr/config.yaml (from a source checkout). "
            "`askback eval` with no --config grades the built-in demo agent instead."
        )


def _load_simulator(config: AppConfig | str | Path | None) -> ConversationEngine:
    """Build the simulator's engine, importing it here and nowhere at module level."""
    _require_simulator_config(config)
    try:
        from askback_sim.engine import ConversationEngine
    except ModuleNotFoundError as exc:
        raise _simulator_missing(exc) from exc
    return ConversationEngine.from_config(config)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_info(args: argparse.Namespace) -> int:
    engine = _load_simulator(args.config)
    print(json.dumps(engine.describe(), indent=2))
    return EXIT_OK


def cmd_ask(args: argparse.Namespace) -> int:
    engine = _load_simulator(args.config)
    response = engine.chat(
        ChatRequest(message=args.question, user_id=args.user, tenant_id=args.tenant)
    )
    if args.json:
        print(json.dumps(response.to_dict(), indent=2, default=str))
        return EXIT_OK

    print(f"\n{response.reply}\n")
    print(
        f"  route={response.route}  status={response.status.value}  "
        f"intent={response.intent.name} ({response.intent.confidence:.2f})  "
        f"{response.latency_ms:.0f}ms"
    )
    if response.citations:
        print(f"  sources: {', '.join(c.doc_id for c in response.citations)}")
    return EXIT_OK


def cmd_chat(args: argparse.Namespace) -> int:
    engine = _load_simulator(args.config)
    info = engine.describe()
    print(f"simulator ready  |  llm={info['llm']}  retriever={info['retriever']}")
    print("Type your question. Commands: /reset to clear history, /quit to exit.\n")

    request = ChatRequest(message="", user_id=args.user, tenant_id=args.tenant)
    while True:
        try:
            message = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return EXIT_OK

        if not message:
            continue
        if message in {"/quit", "/exit"}:
            return EXIT_OK
        if message == "/reset":
            engine.reset(request.conversation_id)
            print("bot > history cleared.\n")
            continue

        request.message = message
        response = engine.chat(request)
        print(f"bot > {response.reply}")
        print(
            f"       [{response.route} | {response.status.value} | "
            f"{response.latency_ms:.0f}ms"
            + (f" | {', '.join(c.doc_id for c in response.citations)}" if response.citations else "")
            + "]\n"
        )


def cmd_eval(args: argparse.Namespace) -> int:
    config = AppConfig.load(args.config)
    settings = config.evaluation
    if AppConfig.is_builtin(args.config):
        from .demo import NOTICE

        print(f"{NOTICE}\n")

    dataset_path = resolve_path(args.dataset or settings.get("dataset_dir", "suites"))
    cases = filter_cases(
        load_cases(
            dataset_path,
            principals=resolve_principals(config),
            default_samples=default_samples(settings),
        ),
        suites=args.suite,
        tags=args.tag,
        ids=args.case,
    )
    if not cases:
        print(f"No cases matched in {dataset_path}.", file=sys.stderr)
        return EXIT_ERROR

    # The factory never imports the simulator, so the one adapter that needs
    # its engine gets it from here. An HTTP run loads no simulator at all.
    engine = _load_simulator(config) if adapter_name(config) == "in_process" else None
    adapter = build_adapter(config, engine=engine)
    # Read before anything runs: a bad limit found after the judge was paid is too late.
    max_degraded(settings)
    budget = judge_budget(settings)
    redaction = resolve_mode(settings, args.redaction)
    eval_engine = EvaluationEngine.from_config(settings, safety=config.safety)
    clarification = build_clarification(settings.get("clarification"))
    workers = int(args.workers or settings.get("parallel_workers", 4))

    print(
        f"Running {len(cases)} case(s) across {len({c.suite for c in cases})} suite(s) "
        f"with {workers} worker(s)..."
    )
    # Printed, not only logged: the default log level hides INFO, and which
    # judge graded a run must never be something you have to go looking for.
    print(f"Judge: {eval_engine.describe().get('judge', 'none')}\n")
    runner = TestRunner(
        adapter,
        eval_engine,
        workers=workers,
        on_case=_progress if not args.quiet else None,
        judge_budget_usd=budget,
        detector=clarification.detector,
        persona_llm=clarification.persona_llm,
        persona_prices=(clarification.price_per_1k_prompt, clarification.price_per_1k_completion),
    )
    report = runner.run(cases)

    # An incomplete run has no pass rate worth printing: the cases that ran are
    # not a sample of the suite. The verdict below says why it stopped.
    if report.incomplete is None:
        _print_summary(report)

    written = write_reports(
        report,
        resolve_path(args.reports_dir or settings.get("reports_dir", "reports")),
        formats=[f.strip() for f in args.format.split(",") if f.strip()],
        stem=args.name,
        redaction=redaction,
    )
    for fmt, path in written.items():
        print(f"  {fmt:<5} {path}")
    if redaction == "full":
        print("  redaction 'full': these files hold complete replies and source passages. Keep them local.")

    # Gated on the redacted view, not the run: the verdict names each offending
    # check with its detail, and a CI log is as downloadable as the artifact.
    # Redaction keeps every verdict and severity, so the gates decide the same.
    verdict = evaluate_gates(
        redacted(report, redaction),
        settings,
        selection_filtered=bool(args.suite or args.tag or args.case),
    )
    _print_verdict(verdict)
    exit_code = verdict.exit_code

    if args.compare_baseline and report.incomplete is None:
        exit_code = max(exit_code, _run_regression(report, settings, args))

    return exit_code


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Run the configured judge over the labelled records and gate on its kappa.

    Needs no agent: the replies are in the records. The threshold is the one
    `eval` gates with, so the agreement measured is the agreement that matters.
    """
    settings = AppConfig.load(args.config).evaluation
    bar, allowed = min_kappa(settings), max_degraded(settings)
    records = args.records or settings.get("calibration_records")
    if not records:
        raise AskbackError(
            "no labelled records: pass --records PATH, or set evaluation.calibration_records "
            "in your config (examples/hr/calibration/seed.yaml is a SYNTHETIC set for the HR example)"
        )
    calibration = load_calibration(resolve_path(records))
    judge = build_judge(settings.get("judge"))
    threshold = float(
        (settings.get("evaluator_options") or {})
        .get("llm_judge", {})
        .get("threshold", DEFAULT_PASS_THRESHOLD)
    )

    report = calibrate(calibration, judge, threshold=threshold)
    verdict = calibration_verdict(report, min_kappa=bar, max_degraded=allowed)
    _print_calibration(report)
    print(f"{'PASSED' if verdict.passed else 'FAILED'}: {verdict.reason}")
    return verdict.exit_code


def cmd_baseline(args: argparse.Namespace) -> int:
    config = AppConfig.load(args.config)
    settings = config.evaluation
    baseline_path = resolve_path(
        args.output or settings.get("baseline_path", "baselines/baseline.json")
    )

    if args.report:
        source = Path(args.report)
    else:
        reports_dir = resolve_path(settings.get("reports_dir", "reports"))
        candidates = sorted(reports_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            print(f"No JSON reports found in {reports_dir}. Run `askback eval` first.", file=sys.stderr)
            return EXIT_ERROR
        source = candidates[-1]

    report = RunReport.from_dict(json.loads(source.read_text(encoding="utf-8")))
    if report.incomplete is not None:
        print(
            f"{source.name} is an incomplete run ({report.incomplete.get('cases_run')} of "
            f"{report.incomplete.get('cases_planned')} cases) and cannot be a baseline.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    save_baseline(report, baseline_path)
    print(f"Baseline saved from {source.name} -> {baseline_path} ({len(report.cases)} cases)")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    _require_simulator_config(args.config)
    config = AppConfig.load(args.config)
    server = config.server
    host = args.host or server.get("host", "127.0.0.1")
    port = int(args.port or server.get("port", 8000))

    try:
        import uvicorn
    except ImportError:
        print("Serving needs FastAPI and uvicorn. Run: pip install -e .[server]", file=sys.stderr)
        return EXIT_ERROR

    # The app is built inside the guard too: the server imports the simulator
    # when it builds its engine, not when its module is imported.
    try:
        from .api.server import create_app

        app = create_app(config)
    except ModuleNotFoundError as exc:
        raise _simulator_missing(exc) from exc

    print(f"Starting API on http://{host}:{port}  (docs at /docs)")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def _progress(result: CaseResult) -> None:
    mark = "PASS" if result.passed else "FAIL"
    line = f"  [{mark}] {result.case_id:<24} {result.score:.2f}  {result.message[:58]}"
    if result.sampled:
        flaky = "  FLAKY" if result.flaky else ""
        line += f"  ({result.passing_samples}/{len(result.samples)} samples{flaky})"
    print(line)


def _print_summary(report: RunReport) -> None:
    summary = report.summary()
    # Named only when something was sampled: for a run of single draws there is
    # nothing to be flaky about, and a bare "0" would claim it had been checked.
    flaky = f"  flaky {summary['flaky']}" if "flaky" in summary else ""
    # Likewise only for a run a metered judge graded: a free judge has no calls
    # to count, and "0 calls, $0.00" would say one had been priced.
    judge = ""
    if "judge_calls" in summary:
        cached = f" ({summary['judge_cache_hits']} cached)" if summary["judge_cache_hits"] else ""
        degraded = f"  degraded {summary['judge_degraded']}" if summary["judge_degraded"] else ""
        judge = (
            f"  judge calls {summary['judge_calls']}{cached} "
            f"est. ${summary['judge_cost_usd']:.4f}{degraded}"
        )
    print(
        f"\n{summary['passed']}/{summary['total']} passed "
        f"({summary['pass_rate'] * 100:.0f}%)  mean score {summary['mean_score']:.2f}  "
        f"critical failures {summary['critical_failures']}{flaky}{judge}  in {report.duration_s:.1f}s"
    )

    print("\n  suite            cases  pass  rate   score")
    for suite, data in report.by_suite().items():
        print(
            f"  {suite:<16} {data['total']:>5} {data['passed']:>5} "
            f"{data['pass_rate'] * 100:>4.0f}%  {data['mean_score']:>6.2f}"
        )

    failing = report.failing_checks()
    if failing:
        print("\n  top failing checks:")
        for name, count in list(failing.items())[:8]:
            print(f"    {count:>3}x {name}")
    print()


def _print_calibration(report: CalibrationReport) -> None:
    """Render agreement and list disagreements by id. No reply text: it may be personal data."""
    pinned = ", ".join(f"{k}={v}" for k, v in report.provenance.items())
    print(f"Calibrating judge: {pinned}  (cut at {report.threshold:.2f})")
    print(f"Records: {report.records} from {report.source}")
    if report.synthetic:
        print(
            f"*** {report.synthetic} of {report.records} records are SYNTHETIC: they make this "
            "command testable and validate nothing about the judge. ***"
        )
    if report.labelled_judge != report.judge:
        print(
            f"Note: the records were drawn from {report.labelled_judge!r} "
            f"(prompt_version {report.labelled_prompt_version}); this judge is {report.judge!r}."
        )
    if report.usage is not None:
        cached = f" ({report.usage.cache_hits} cached)" if report.usage.cache_hits else ""
        print(f"Judge calls {report.usage.calls}{cached}  est. ${report.usage.cost_usd:.4f}")
    for record_id, reason in report.degraded:
        print(f"  DEGRADED {record_id}: {reason} (excluded from the statistics)")

    def row(name: str, n: int, rate: float, kappa: float | None) -> str:
        shown = "undefined" if kappa is None else f"{kappa:>9.2f}"
        return f"  {name:<14} {n:>4}  {rate * 100:>6.0f}%  {shown:>9}"

    print("\n  dimension         n   agree      kappa")
    for dimension, agreement in report.by_dimension.items():
        print(row(dimension, agreement.n, agreement.rate, agreement.kappa))
    overall = report.overall
    print(row("overall", overall.n, overall.rate, overall.kappa))

    if report.disagreements:
        print(f"\n  {len(report.disagreements)} disagreement(s):")
        for d in report.disagreements:
            print(
                f"    {d.record_id:<24} {d.dimension:<13} human "
                f"{'acceptable' if d.human else 'unacceptable':<12} judge "
                f"{'acceptable' if d.judge else 'unacceptable':<12} (score {d.score:.2f})"
            )
    print()


def _print_verdict(verdict: RunVerdict) -> None:
    """Render each gate's verdict, naming the cases behind every failure.

    Offenders are printed under the gate, not left to the per-case lines: CI runs
    with --quiet, which suppresses those, and a breach that names nothing sends
    the reader to the HTML report. A case failing a critical check and a floor
    appears under both gates; each answers a different question.
    """
    for gate in verdict.gates:
        print(f"{gate.status}: {gate.reason}")
        for offender in gate.offenders:
            print(f"    {offender.label}  {offender.check}: {offender.detail}")


def _run_regression(report: RunReport, settings: dict[str, Any], args: argparse.Namespace) -> int:
    baseline_path = resolve_path(
        args.baseline or settings.get("baseline_path", "baselines/baseline.json")
    )
    try:
        baseline = load_baseline(baseline_path)
    except AskbackError as exc:
        print(f"\nRegression check skipped: {exc}")
        return EXIT_OK

    comparator = RegressionComparator(
        tolerance=float(settings.get("regression_tolerance", 0.05))
    )
    result = comparator.compare(baseline, report)

    print(f"\nRegression vs baseline ({baseline_path.name}):")
    print(
        f"  mean {result.baseline_mean:.3f} -> {result.candidate_mean:.3f} "
        f"({result.mean_delta:+.3f})   "
        f"pass rate {result.baseline_pass_rate:.0%} -> {result.candidate_pass_rate:.0%}"
    )
    for warning in result.context_warnings:
        print(f"  warning: {warning}")
    for suite in result.suites:
        if suite.moved:
            print(f"  {_suite_line(suite)}")
    # Every flipped verdict is named, uncapped: this is the diff a red build is read for.
    dropped = [d for d in result.regressed if d.candidate_passed == d.baseline_passed]
    for label, deltas in (
        ("newly failing", result.newly_failing),
        ("newly passing", result.newly_passing),
        ("score dropped", dropped[:10]),
    ):
        for delta in deltas:
            print(f"  {label}: {_case_line(delta)}")
    for reason in result.reasons:
        print(f"  {reason}")

    return EXIT_OK if result.passed else EXIT_REGRESSION


def _case_line(delta: CaseDelta) -> str:
    return f"{delta.suite}/{delta.case_id} {delta.baseline_score:.2f} -> {delta.candidate_score:.2f}"


def _suite_line(delta: SuiteDelta) -> str:
    def side(row: dict[str, Any] | None) -> str:
        if row is None:
            return "not run"
        return f"{row['pass_rate']:.0%} ({row['passed']}/{row['total']})"

    base, cand = delta.baseline, delta.candidate
    line = f"suite {delta.suite}: {side(base)} -> {side(cand)}"
    if base is None or cand is None:
        return line
    return (
        f"{line}  mean {base['mean_score']:.3f} -> {cand['mean_score']:.3f}  "
        f"{delta.newly_failing} newly failing, {delta.newly_passing} newly passing"
    )


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="askback", description=__doc__.split("\n")[0])
    parser.add_argument(
        "--config",
        help="Path to your config file. With none (and no ASKBACK_CONFIG), `eval` grades the "
        "built-in demo agent against the bundled example suite",
    )
    parser.add_argument("--log-level", default="WARNING", help="DEBUG, INFO, WARNING, ERROR")
    # The one place the version is written is askback.__version__; the distribution
    # metadata reads it from there too, so the two cannot disagree.
    parser.add_argument("--version", action="version", version=f"askback {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="Show how the engine is wired").set_defaults(func=cmd_info)

    ask = sub.add_parser("ask", help="Ask one question")
    ask.add_argument("question")
    ask.add_argument("--user", default="cli_user")
    ask.add_argument("--tenant", default="default")
    ask.add_argument("--json", action="store_true", help="Print the full response object")
    ask.set_defaults(func=cmd_ask)

    chat = sub.add_parser("chat", help="Interactive session")
    chat.add_argument("--user", default="cli_user")
    chat.add_argument("--tenant", default="default")
    chat.set_defaults(func=cmd_chat)

    ev = sub.add_parser("eval", help="Run test suites and write reports")
    ev.add_argument("--dataset", help="Dataset file or directory")
    ev.add_argument("--suite", action="append", help="Suite to run (repeatable)")
    ev.add_argument("--tag", action="append", help="Only cases with this tag (repeatable)")
    ev.add_argument("--case", action="append", help="Only this case id (repeatable)")
    ev.add_argument("--format", default="json,html", help="json,html,xlsx,md (md: a one-page summary)")
    ev.add_argument("--reports-dir", help="Where to write reports")
    ev.add_argument("--name", help="Report file stem (default: run id)")
    ev.add_argument(
        "--redaction",
        choices=MODES,
        help="How much text reports carry (default: evaluation.redaction, else ci). "
        "'full' is for local runs and is refused when CI is set",
    )
    ev.add_argument("--workers", type=int, help="Parallel workers")
    ev.add_argument("--quiet", action="store_true", help="Do not print per-case lines")
    ev.add_argument("--compare-baseline", action="store_true", help="Also run the regression gate")
    ev.add_argument("--baseline", help="Baseline report path")
    ev.set_defaults(func=cmd_eval)

    cal = sub.add_parser("calibrate", help="Measure the judge's agreement with human labels")
    cal.add_argument(
        "--records",
        help="Labelled records (default: evaluation.calibration_records in the config)",
    )
    cal.set_defaults(func=cmd_calibrate)

    base = sub.add_parser("baseline", help="Manage the regression baseline")
    base_sub = base.add_subparsers(dest="baseline_command", required=True)
    save = base_sub.add_parser("save", help="Promote a run report to the baseline")
    save.add_argument("--report", help="Report JSON to promote (default: newest in reports/)")
    save.add_argument("--output", help="Baseline path")
    save.set_defaults(func=cmd_baseline)

    serve = sub.add_parser("serve", help="Start the HTTP API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return int(args.func(args))
    except AskbackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
