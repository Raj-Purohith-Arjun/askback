"""An evaluation framework for conversational agents.

Drives an agent through test conversations, grades the replies with layered
evaluators, and gates on the result. The agent is reached through an adapter
(`askback.adapters`), so the same suite grades a deployed service over HTTP or
the `askback_sim` fixture in this process. This package never imports `askback_sim`.

Extension points:
    Adapter       askback.adapters.base.AgentAdapter
    Evaluator     askback.evaluation.base.Evaluator
    LLM provider  askback.providers.base.LLMProvider
"""

from __future__ import annotations

from .core.config import AppConfig
from .core.contracts import ChatRequest, ChatResponse, ResponseStatus
from .core.logging import setup_logging

__version__ = "0.1.0"

__all__ = [
    "AppConfig",
    "ChatRequest",
    "ChatResponse",
    "ResponseStatus",
    "__version__",
    "setup_logging",
]
