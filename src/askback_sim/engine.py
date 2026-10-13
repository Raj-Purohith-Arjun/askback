"""The conversation engine: one turn, start to finish.

Pipeline, matching the execution flow in the architecture diagram:

    request -> input guards -> FrontLine (intent + route) -> specialized agent
            -> output guards -> memory -> ChatResponse

Everything the engine touches arrives through the constructor, so a test can
build one with fakes in three lines. `ConversationEngine.from_config` is the
convenience path that reads YAML and assembles the real thing.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from askback.core.config import AppConfig, load_mapping, resolve_path
from askback.core.contracts import (
    ChatRequest,
    ChatResponse,
    Message,
    ResponseStatus,
    Role,
)
from askback.core.errors import ConfigError
from askback.core.logging import get_logger

from .agents import build_agents
from .agents.base import Agent, Turn
from .agents.frontline import FrontLineAgent, IntentSpec
from .llm import build_provider
from .llm.base import LLMProvider
from .llm.prompts import PROMPT_VERSION, REFUSAL_TEMPLATE
from .memory import ConversationStore, InMemoryConversationStore
from .retrieval import build_retriever
from .retrieval.base import Retriever
from .safety import SafetyPipeline, merge_reports
from .tools import build_toolbox
from .tools.base import ToolBox

log = get_logger(__name__)

DEFAULT_INTENTS_PATH = "examples/hr/intents.yaml"


class ConversationEngine:
    """Turns a `ChatRequest` into a `ChatResponse`. The single entry point."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        retriever: Retriever,
        tools: ToolBox,
        frontline: FrontLineAgent,
        agents: dict[str, Agent],
        safety: SafetyPipeline,
        memory: ConversationStore | None = None,
        fallback_route: str = "fallback_agent",
    ) -> None:
        self.llm = llm
        self.retriever = retriever
        self.tools = tools
        self.frontline = frontline
        self.agents = agents
        self.safety = safety
        self.memory = memory or InMemoryConversationStore()
        self.fallback_route = fallback_route

        if fallback_route not in agents:
            raise ConfigError(
                f"fallback route {fallback_route!r} is not among the configured "
                f"agents: {sorted(agents)}"
            )

    # ------------------------------------------------------------------ API #

    def chat(self, request: ChatRequest | str, **kwargs: Any) -> ChatResponse:
        """Answer one user message.

        Accepts a plain string for convenience: `engine.chat("hello")`.
        """
        if isinstance(request, str):
            request = ChatRequest(message=request, **kwargs)

        started = time.perf_counter()
        history = self.memory.history(request.conversation_id)

        def finish(response: ChatResponse) -> ChatResponse:
            response.latency_ms = round((time.perf_counter() - started) * 1000, 2)
            response.model = self.llm.name
            self.memory.append(
                request.conversation_id,
                [Message(Role.USER, request.message), Message(Role.ASSISTANT, response.reply)],
            )
            return response

        # 1. Input guards. Classify anyway so blocked turns still report an intent.
        _, input_report = self.safety.run(
            request.message, stage="input", meta={"user_id": request.user_id}
        )
        intent, route = self.frontline.route(request.message, history)

        if input_report.blocked:
            log.info(
                "Refused turn", extra={"trace": request.conversation_id, "reasons": input_report.reasons}
            )
            return finish(
                ChatResponse(
                    reply=REFUSAL_TEMPLATE.format(reason=input_report.reasons[0]),
                    conversation_id=request.conversation_id,
                    route="safety_guard",
                    status=ResponseStatus.REFUSED,
                    intent=intent,
                    safety=input_report,
                )
            )

        # 2. Specialized agent.
        agent = self.agents.get(route) or self.agents[self.fallback_route]
        turn = Turn(
            request=request,
            llm=self.llm,
            retriever=self.retriever,
            tools=self.tools,
            history=history,
            intent=intent,
        )

        try:
            reply = agent.handle(turn)
        except Exception as exc:  # noqa: BLE001 - one bad agent must not 500 the service
            log.exception("Agent %s failed", agent.name)
            return finish(
                ChatResponse(
                    reply="Something went wrong while answering. Please try again.",
                    conversation_id=request.conversation_id,
                    route=agent.name,
                    status=ResponseStatus.ERROR,
                    intent=intent,
                    safety=input_report,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

        # 3. Output guards. Redaction rewrites the text; a block replaces it.
        safe_text, output_report = self.safety.run(
            reply.text, stage="output", meta={"user_id": request.user_id}
        )
        safety = merge_reports(input_report, output_report)

        if output_report.blocked:
            return finish(
                ChatResponse(
                    reply=REFUSAL_TEMPLATE.format(reason=output_report.reasons[0]),
                    conversation_id=request.conversation_id,
                    route=agent.name,
                    status=ResponseStatus.REFUSED,
                    intent=intent,
                    safety=safety,
                )
            )

        return finish(
            ChatResponse(
                reply=safe_text,
                conversation_id=request.conversation_id,
                route=agent.name,
                status=reply.status,
                intent=intent,
                citations=reply.citations,
                retrieved=reply.retrieved,
                tool_calls=reply.tool_calls,
                facts=reply.facts,
                safety=safety,
            )
        )

    def reset(self, conversation_id: str | None = None) -> None:
        """Forget history for one conversation, or all of them."""
        self.memory.reset(conversation_id)

    def describe(self) -> dict[str, Any]:
        """Runtime summary, used by the CLI banner and the /health endpoint."""
        return {
            "llm": self.llm.name,
            "retriever": f"{self.retriever.name} ({len(self.retriever)} chunks)",
            "agents": sorted(self.agents),
            "tools": self.tools.available(),
            "guards": [g.name for g in self.safety.guards],
        }

    def provenance(self) -> dict[str, str]:
        """What a run report records about this engine, for comparing runs.

        Strings only, and stable keys: the in-process adapter passes this
        straight into the run context, where a baseline is compared against it.
        """
        return {
            "llm_provider": self.llm.name,
            "retriever": self.retriever.name,
            "corpus_chunks": str(len(self.retriever)),
            "prompt_version": PROMPT_VERSION,
            "agents": ",".join(sorted(self.agents)),
        }

    # -------------------------------------------------------------- factory #

    @classmethod
    def from_config(
        cls,
        config: AppConfig | str | Path | None = None,
        *,
        intents_path: str | Path | None = None,
    ) -> ConversationEngine:
        """Build a fully wired engine from a config file and its intents file."""
        cfg = config if isinstance(config, AppConfig) else AppConfig.load(config)

        llm = build_provider(cfg.llm)
        retriever = build_retriever(cfg.retrieval)
        # knowledge_search needs the retriever, so tools are built after it.
        tools = build_toolbox({"retriever": retriever})

        routing = cfg.routing or {}
        intent_specs = load_intent_specs(
            intents_path or routing.get("intents_path", DEFAULT_INTENTS_PATH)
        )
        frontline = FrontLineAgent.from_config(intent_specs, routing, llm)

        agent_specs = routing.get("agents")
        if not agent_specs:
            raise ConfigError("config routing.agents is empty; nothing can answer.")
        agents = build_agents(agent_specs)

        return cls(
            llm=llm,
            retriever=retriever,
            tools=tools,
            frontline=frontline,
            agents=agents,
            safety=SafetyPipeline.from_config(cfg.safety),
            memory=InMemoryConversationStore(int(routing.get("memory_turns", 40))),
            fallback_route=str(routing.get("default_route", "fallback_agent")),
        )


def load_intent_specs(path: str | Path) -> list[IntentSpec]:
    """Read the intents file into `IntentSpec` objects."""
    data = load_mapping(resolve_path(path))
    entries: Sequence[dict[str, Any]] = data.get("intents", [])
    if not entries:
        raise ConfigError(f"{path} declares no intents.")
    return [IntentSpec.from_dict(entry) for entry in entries]


__all__ = ["ConversationEngine", "load_intent_specs"]
