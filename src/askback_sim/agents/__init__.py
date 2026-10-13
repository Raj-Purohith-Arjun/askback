"""Agents: the FrontLine router and the specialized agents it selects."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from askback.core.errors import ConfigError

from ..registry import AGENTS
from .base import Agent, Turn
from .frontline import (
    FrontLineAgent,
    IntentSpec,
    KeywordIntentClassifier,
    LLMIntentClassifier,
)
from .specialized import (
    EscalationAgent,
    FallbackAgent,
    KnowledgeAgent,
    SmallTalkAgent,
    extract_citations,
)


def build_agents(specs: Sequence[dict[str, Any]]) -> dict[str, Agent]:
    """Instantiate the agents declared in config.

    Each spec needs `name` (the route id) and `type` (the registered class)::

        - name: policy_agent
          type: knowledge
          description: Company policy
          tools: [knowledge_search]
          filters: {domain: policy}
    """
    agents: dict[str, Agent] = {}
    for spec in specs:
        data = dict(spec)
        route = data.pop("name", None)
        kind = data.pop("type", None)
        if not route or not kind:
            raise ConfigError(f"Agent entry needs 'name' and 'type': {spec!r}")
        agents[route] = AGENTS.get(str(kind))(name=str(route), **data)
    return agents


__all__ = [
    "Agent",
    "EscalationAgent",
    "FallbackAgent",
    "FrontLineAgent",
    "IntentSpec",
    "KeywordIntentClassifier",
    "KnowledgeAgent",
    "LLMIntentClassifier",
    "SmallTalkAgent",
    "Turn",
    "build_agents",
    "extract_citations",
]
