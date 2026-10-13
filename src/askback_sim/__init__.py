"""The simulator: an HR-style agent that exists to be evaluated.

A development fixture, not the product. `askback` is the product; this is the
system it can be pointed at when there is no deployed agent to hand, and what
its own acceptance tests drive. It depends on `askback` for the shared
contracts. `askback` never imports it - tests/test_boundaries.py holds that.

    from askback_sim import ConversationEngine

    engine = ConversationEngine.from_config()
    print(engine.chat("How many days of annual leave do I get?").reply)

Extension points, selected by name from the config file (examples/hr/config.yaml):
    Retriever     askback_sim.retrieval.base.Retriever
    Agent         askback_sim.agents.base.Agent
    Tool          askback_sim.tools.base.Tool
"""

from __future__ import annotations

from .engine import ConversationEngine

__all__ = ["ConversationEngine"]
