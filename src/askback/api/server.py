"""FastAPI application: the public API, plus the built-in dashboard.

The engine is built once at startup and reused: loading the corpus per request
would dominate latency. `InMemoryConversationStore` is per process, so run a
single worker locally, and swap in a shared `ConversationStore` before scaling
out (see `askback_sim.memory`).

    askback serve            # dashboard at /, API docs at /docs
    curl -X POST localhost:8000/chat -H "content-type: application/json" \
         -d '{"message": "How many days do I have to return an item?"}'

Two surfaces, deliberately separate:

    /chat, /info, /search    the stable contract other systems integrate against
    /api/*                   developer tooling behind the dashboard, free to change
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..core.config import AppConfig, resolve_path
from ..core.contracts import ChatRequest
from ..core.logging import get_logger, setup_logging
from .dashboard import build_dashboard_router, safe_name
from .state import EngineHolder

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- #
# Wire models. Separate from the internal contracts on purpose: the HTTP surface
# should be free to stay stable while the internals evolve.
# --------------------------------------------------------------------------- #


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = Field(None, max_length=128)
    user_id: str = Field("api_user", max_length=128)
    tenant_id: str = Field("default", max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CitationOut(BaseModel):
    doc_id: str
    title: str = ""
    source: str = ""
    snippet: str = ""


class ChatOut(BaseModel):
    reply: str
    conversation_id: str
    route: str
    status: str
    intent: str
    intent_confidence: float
    citations: list[CitationOut] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    safety_blocked: bool = False
    latency_ms: float = 0.0
    trace_id: str = ""


class ResetIn(BaseModel):
    conversation_id: str | None = None


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build the application. Import-safe: nothing runs until this is called."""
    cfg = config or AppConfig.load()
    setup_logging(str(cfg.logging.get("level", "INFO")))

    app = FastAPI(
        title="askback dashboard",
        version=__version__,
        description="Configurable conversational agent with routing, RAG, safety and evaluation.",
    )

    origins = cfg.server.get("cors_origins", ["*"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Built once, shared by every request. The holder exists so the dashboard can
    # rebuild it after the files change on disk, without restarting the server.
    holder = EngineHolder(cfg)
    expected_key = str(cfg.server.get("api_key", "") or "")

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        """No-op when `server.api_key` is unset, which is the local default."""
        if expected_key and x_api_key != expected_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key."
            )

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, Any]:
        """Liveness probe. Cheap enough for a load balancer to hit often."""
        return {"status": "ok", "version": app.version}

    @app.get("/info", tags=["ops"], dependencies=[Depends(require_api_key)])
    def info() -> dict[str, Any]:
        """How the engine is wired: provider, retriever, agents, tools, guards."""
        return holder.engine.describe()

    @app.post("/chat", response_model=ChatOut, tags=["chat"], dependencies=[Depends(require_api_key)])
    def chat(payload: ChatIn) -> ChatOut:
        """Answer one message."""
        request = ChatRequest(
            message=payload.message,
            user_id=payload.user_id,
            tenant_id=payload.tenant_id,
            metadata=payload.metadata,
            **({"conversation_id": payload.conversation_id} if payload.conversation_id else {}),
        )
        response = holder.engine.chat(request)
        return ChatOut(
            reply=response.reply,
            conversation_id=response.conversation_id,
            route=response.route,
            status=response.status.value,
            intent=response.intent.name,
            intent_confidence=round(response.intent.confidence, 4),
            citations=[
                CitationOut(
                    doc_id=c.doc_id, title=c.title, source=c.source, snippet=c.snippet[:240]
                )
                for c in response.citations
            ],
            tools_used=response.tool_names,
            safety_blocked=response.safety.blocked,
            latency_ms=response.latency_ms,
            trace_id=response.trace_id,
        )

    @app.post("/reset", tags=["chat"], dependencies=[Depends(require_api_key)])
    def reset(payload: ResetIn) -> dict[str, str]:
        """Clear one conversation, or every conversation when no id is given."""
        holder.engine.reset(payload.conversation_id)
        return {"status": "reset", "conversation_id": payload.conversation_id or "all"}

    @app.get("/search", tags=["debug"], dependencies=[Depends(require_api_key)])
    def search(q: str, top_k: int = 5, tenant: str = "default") -> dict[str, Any]:
        """Inspect retrieval directly. Useful when an answer looks wrong."""
        hits = holder.engine.retriever.search(q, top_k=min(top_k, 20), tenant=tenant)
        return {
            "query": q,
            "hits": [
                {"doc_id": h.doc_id, "title": h.title, "score": h.score, "text": h.text[:300]}
                for h in hits
            ],
        }

    app.include_router(build_dashboard_router(holder, cfg, require_api_key))
    _mount_dashboard(app, cfg)
    return app


def _mount_dashboard(app: FastAPI, cfg: AppConfig) -> None:
    """Serve the dashboard at `/`, if its assets shipped with the package.

    A slim install without the static files should still serve the API rather
    than fail at startup, so a missing directory is a warning, not an error.
    """
    if not STATIC_DIR.is_dir():
        log.warning("Dashboard assets not found at %s; serving API only.", STATIC_DIR)
        return

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/reports/{name}.html", include_in_schema=False)
    def report_page(name: str) -> FileResponse:
        """Open a generated HTML report from the dashboard's run list."""
        path = resolve_path(cfg.evaluation.get("reports_dir", "reports")) / f"{safe_name(name)}.html"
        if not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No HTML report named {name!r}.")
        return FileResponse(path)


# Uvicorn entry point: `uvicorn askback.api.server:app`
app = None  # set lazily by `create_app`; `askback serve` builds it explicitly
