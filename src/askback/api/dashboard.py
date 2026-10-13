"""The dashboard API: everything the built-in UI needs that the public API does not.

Kept separate from `server.py` on purpose. `/chat` is a stable contract other
systems integrate against; these `/api/*` routes are developer tooling and are
free to change shape as the UI does.

Four groups, matching the four panels:

    /api/console   ask a question and see why the engine answered that way
    /api/eval      run suites, browse past runs, compare against the baseline
    /api/corpus    read the knowledge base
    /api/routing   read the intent catalogue

The corpus and the intent catalogue are read-only here, with no switch that makes
them otherwise: they decide what the agent answers, and a route that rewrites them
is live on every instance anyone can reach. Edit the files and reload.

Nor can a run be promoted to the baseline from here. The baseline is what every
later regression comparison trusts, and a reachable instance that could replace it
would make those comparisons lie with nothing looking wrong afterwards. That is
`askback baseline save`, on the CLI, where it leaves a file to commit.

The one route that changes server state is `POST /api/system/reload`, and
`server.allow_runtime_reload` (default false) is what permits it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..adapters.factory import resolve_principals
from ..core.config import AppConfig, default_samples, resolve_path
from ..core.contracts import ChatRequest
from ..core.logging import get_logger
from ..datasets import filter_cases, load_cases
from ..evaluation.engine import EvaluationEngine
from ..evaluation.regression import Baseline, RegressionComparator
from ..evaluation.results import RunReport
from ..reporting import redacted, resolve_mode, write_reports
from ..runner import TestRunner
from .corpus_store import CorpusStore
from .state import EngineHolder

log = get_logger(__name__)

#: Report and run names become file paths, so they are restricted to characters
#: that cannot escape the reports directory.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

#: Sentinel accepted wherever a run name is expected, meaning "the saved baseline".
BASELINE_ALIAS = "baseline"


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class ConsoleChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = Field(None, max_length=128)
    user_id: str = Field("dashboard", max_length=128)
    tenant_id: str = Field("default", max_length=128)


class EvalRunIn(BaseModel):
    suites: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    name: str | None = Field(None, max_length=80)
    workers: int | None = Field(None, ge=1, le=32)


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


def build_dashboard_router(
    holder: EngineHolder,
    cfg: AppConfig,
    require_api_key: Callable[..., Any],
) -> APIRouter:
    """Assemble the dashboard routes around a live engine."""
    router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])

    evaluation = cfg.evaluation
    reports_dir = resolve_path(evaluation.get("reports_dir", "reports"))
    dataset_dir = resolve_path(evaluation.get("dataset_dir", "suites"))
    baseline_path = resolve_path(
        evaluation.get("baseline_path", "baselines/baseline.json")
    )
    corpus = CorpusStore(resolve_path(cfg.retrieval.get("corpus_path", "examples/hr/corpus")))
    intents_path = resolve_path(cfg.routing.get("intents_path", "examples/hr/intents.yaml"))
    reload_allowed = bool(cfg.server.get("allow_runtime_reload", False))

    def require_reload_allowed() -> None:
        if not reload_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Runtime reload is disabled. Set server.allow_runtime_reload to true to allow it.",
            )

    # ------------------------------------------------------------- console #

    @router.post("/console/chat", tags=["console"])
    def console_chat(payload: ConsoleChatIn) -> dict[str, Any]:
        """Answer a message and return everything behind the answer.

        The public `/chat` returns what an integrator needs; this returns what a
        tester needs, which is mostly the reasoning that led to the reply.
        """
        session: dict[str, Any] = (
            {"conversation_id": payload.conversation_id} if payload.conversation_id else {}
        )
        request = ChatRequest(
            message=payload.message,
            user_id=payload.user_id,
            tenant_id=payload.tenant_id,
            **session,
        )
        response = holder.engine.chat(request)
        return {
            "reply": response.reply,
            "conversation_id": response.conversation_id,
            "route": response.route,
            "status": response.status.value,
            "latency_ms": response.latency_ms,
            "trace_id": response.trace_id,
            "model": response.model,
            "error": response.error,
            "intent": {
                "name": response.intent.name,
                "confidence": round(response.intent.confidence, 4),
                "rationale": response.intent.rationale,
                # Sorted so the UI can render a ranked "why this route" list.
                "scores": dict(
                    sorted(response.intent.scores.items(), key=lambda kv: kv[1], reverse=True)
                ),
            },
            "citations": [
                {"doc_id": c.doc_id, "title": c.title, "source": c.source, "snippet": c.snippet}
                for c in response.citations
            ],
            "retrieved": [
                {
                    "doc_id": c.doc_id,
                    "chunk_id": c.chunk_id,
                    "title": c.title,
                    "score": c.score,
                    "text": c.text,
                    "metadata": c.metadata,
                }
                for c in response.retrieved
            ],
            "tool_calls": [
                {
                    "name": t.name,
                    "arguments": t.arguments,
                    "ok": t.ok,
                    "error": t.error,
                    "latency_ms": t.latency_ms,
                }
                for t in response.tool_calls
            ],
            "safety": {
                "blocked": response.safety.blocked,
                "injection_detected": response.safety.injection_detected,
                "risk_score": response.safety.risk_score,
                "reasons": response.safety.reasons,
                "redactions": response.safety.redactions,
            },
        }

    @router.post("/console/reset", tags=["console"])
    def console_reset(conversation_id: str | None = None) -> dict[str, str]:
        holder.engine.reset(conversation_id)
        return {"status": "reset", "conversation_id": conversation_id or "all"}

    @router.get("/console/route-test", tags=["console"])
    def route_test(q: str) -> dict[str, Any]:
        """Classify without answering. The fast loop when tuning intents."""
        intent, route = holder.engine.frontline.route(q)
        return {
            "query": q,
            "route": route,
            "intent": intent.name,
            "confidence": round(intent.confidence, 4),
            "rationale": intent.rationale,
            "scores": dict(sorted(intent.scores.items(), key=lambda kv: kv[1], reverse=True)),
        }

    @router.get("/console/search", tags=["console"])
    def search(q: str, top_k: int = 5, tenant: str = "default") -> dict[str, Any]:
        """Retrieval only, with no agent or generation in the way."""
        hits = holder.engine.retriever.search(q, top_k=min(top_k, 20), tenant=tenant)
        return {
            "query": q,
            "hits": [
                {
                    "doc_id": h.doc_id,
                    "chunk_id": h.chunk_id,
                    "title": h.title,
                    "score": h.score,
                    "text": h.text,
                }
                for h in hits
            ],
        }

    # ---------------------------------------------------------- evaluation #

    @router.get("/eval/cases", tags=["evaluation"])
    def list_cases(suite: str | None = None) -> dict[str, Any]:
        """The test cases on disk, so the UI can show what a run will cover."""
        cases = load_cases(dataset_dir)
        selected = filter_cases(cases, suites=[suite] if suite else None)
        suites: dict[str, int] = {}
        tags: dict[str, int] = {}
        for case in cases:
            suites[case.suite] = suites.get(case.suite, 0) + 1
            for tag in case.tags:
                tags[tag] = tags.get(tag, 0) + 1

        return {
            "suites": dict(sorted(suites.items())),
            "tags": dict(sorted(tags.items())),
            "total": len(cases),
            "cases": [
                {
                    "id": c.id,
                    "suite": c.suite,
                    "message": c.turns[0].user,
                    # The fields below are the opening turn's; this says when
                    # that is not the whole case.
                    "turns": len(c.turns),
                    "description": c.description,
                    "expected_route": c.turns[0].expect.expected_route,
                    "expected_status": c.turns[0].expect.expected_status,
                    "expected_docs": c.turns[0].expect.expected_docs,
                    "required_facts": c.turns[0].expect.required_facts,
                    "must_refuse": c.turns[0].expect.must_refuse,
                    "risk": c.risk,
                    "tags": c.tags,
                }
                for c in selected
            ],
        }

    @router.post("/eval/run", tags=["evaluation"])
    def run_eval(payload: EvalRunIn) -> dict[str, Any]:
        """Run the selected cases and write the reports.

        Synchronous. With the offline provider a full run is well under a
        second; once a remote model is wired in expect this to take minutes, at
        which point move it to a background task.
        """
        cases = filter_cases(
            load_cases(
                dataset_dir,
                principals=resolve_principals(cfg),
                default_samples=default_samples(evaluation),
            ),
            suites=payload.suites or None,
            tags=payload.tags or None,
            ids=payload.case_ids or None,
        )
        if not cases:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No test cases matched that selection.",
            )

        name = safe_name(payload.name) if payload.name else None
        runner = TestRunner(
            holder.adapter,
            EvaluationEngine.from_config(evaluation, safety=cfg.safety),
            workers=int(payload.workers or evaluation.get("parallel_workers", 4)),
        )
        report = runner.run(cases, run_id=name)
        # The response is a report leaving the machine like any other, so it is
        # the redacted one that was written, not the run as the runner holds it.
        redaction = resolve_mode(evaluation)
        written = write_reports(
            report, reports_dir, formats=("json", "html"), stem=report.run_id, redaction=redaction
        )

        return {
            **redacted(report, redaction).to_dict(),
            "artifacts": {fmt: str(path) for fmt, path in written.items()},
        }

    @router.get("/eval/runs", tags=["evaluation"])
    def list_runs() -> dict[str, Any]:
        """Past runs, newest first, summarised from the JSON reports on disk."""
        runs: list[dict[str, Any]] = []
        if reports_dir.is_dir():
            for path in sorted(
                reports_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            ):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue  # a half-written report should not break the list
                runs.append(
                    {
                        "name": path.stem,
                        "run_id": data.get("run_id", path.stem),
                        "finished_at": data.get("finished_at", ""),
                        "summary": data.get("summary", {}),
                        "context": data.get("context", {}),
                        "has_html": (reports_dir / f"{path.stem}.html").exists(),
                    }
                )
        return {"runs": runs, "baseline_exists": baseline_path.exists()}

    @router.get("/eval/runs/{name}", tags=["evaluation"])
    def get_run(name: str) -> dict[str, Any]:
        return _load_report_dict(name)

    @router.get("/eval/compare", tags=["evaluation"])
    def compare(candidate: str, baseline: str = BASELINE_ALIAS) -> dict[str, Any]:
        """Regression comparison between two runs, or a run and the baseline."""
        comparator = RegressionComparator(
            tolerance=float(evaluation.get("regression_tolerance", 0.05))
        )
        # The saved baseline is a summary; any other name is a whole run report.
        data = _load_report_dict(baseline)
        result = comparator.compare(
            Baseline.from_dict(data) if baseline == BASELINE_ALIAS else RunReport.from_dict(data),
            RunReport.from_dict(_load_report_dict(candidate)),
        )
        return {"baseline": baseline, "candidate": candidate, **result.to_dict()}

    # -------------------------------------------------------------- corpus #

    @router.get("/corpus", tags=["corpus"])
    def list_documents() -> dict[str, Any]:
        documents = corpus.list()
        return {
            "documents": documents,
            "files": [f.name for f in corpus.files()],
            "total_characters": sum(d["characters"] for d in documents),
            "indexed_chunks": len(holder.engine.retriever),
        }

    @router.get("/corpus/{doc_id}", tags=["corpus"])
    def get_document(doc_id: str) -> dict[str, Any]:
        document = corpus.get(doc_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No document {doc_id!r}.")
        return document

    # ------------------------------------------------------------- routing #

    @router.get("/routing/intents", tags=["routing"])
    def get_intents() -> dict[str, Any]:
        """The raw YAML, plus the parsed view the UI renders as a table."""
        text = intents_path.read_text(encoding="utf-8")
        return {
            "yaml": text,
            "path": str(intents_path),
            "intents": _parse_intents(text),
            "agents": sorted(holder.engine.agents),
        }

    # -------------------------------------------------------------- system #

    @router.get("/system/info", tags=["system"])
    def system_info() -> dict[str, Any]:
        return {**holder.describe(), "reload_allowed": reload_allowed}

    @router.post("/system/reload", tags=["system"], dependencies=[Depends(require_reload_allowed)])
    def system_reload() -> dict[str, Any]:
        holder.reload()
        return holder.describe()

    # ------------------------------------------------------------- helpers #

    def _load_report_dict(name: str) -> dict[str, Any]:
        path = baseline_path if name == BASELINE_ALIAS else reports_dir / f"{safe_name(name)}.json"
        if not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No report {name!r} at {path}.")
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    return router


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #


def safe_name(name: str) -> str:
    """Reject anything that could escape the reports directory.

    Run names arrive from the UI and become file paths, so this is the boundary
    between a user-supplied string and the filesystem.
    """
    candidate = Path(name).name  # strips any directory component outright
    if not _SAFE_NAME.match(candidate):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid name {name!r}: use letters, digits, dot, dash or underscore.",
        )
    return candidate


def _parse_intents(text: str) -> list[dict[str, Any]]:
    """Parse the intents YAML into the flat rows the UI table renders."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "PyYAML is not installed.") from exc

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid YAML: {exc}") from exc

    entries = data.get("intents", []) if isinstance(data, dict) else []
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Malformed intent entry: {entry!r}")
        rows.append(
            {
                "name": str(entry["name"]),
                "route": str(entry.get("route", "fallback_agent")),
                "description": str(entry.get("description", "")),
                "keywords": [str(k) for k in entry.get("keywords", [])],
                "examples": [str(e) for e in entry.get("examples", [])],
                "priority": float(entry.get("priority", 1.0)),
            }
        )
    return rows
