"""The simulator's plugin tables: agents, retrievers and tools, picked by name from config.

These are simulator concepts - the evaluation framework grades an agent's
replies and has no agents, retrievers or tools of its own - so the tables live
here and not in `askback.core.registry`, which keeps only the mechanism.

    from askback_sim.registry import AGENTS

    @AGENTS.register("billing")
    class BillingAgent(Agent):
        ...

    # examples/hr/config.yaml -> agents: [billing]
"""

from __future__ import annotations

from typing import Any

from askback.core.registry import Registry
from askback.evaluation.safety_eval import declare_side_effecting_tools

AGENTS: Registry[Any] = Registry("agent")
RETRIEVERS: Registry[Any] = Registry("retriever")
TOOLS: Registry[Any] = Registry("tool")


def _side_effecting() -> set[str]:
    return {name for name, cls in TOOLS if getattr(cls, "side_effecting", False)}


# The safety evaluator needs to know which tools act on the world, and it cannot
# import this package to ask. So the simulator tells it: read at evaluation
# time, not copied now, so a tool registered later is still counted.
declare_side_effecting_tools(_side_effecting)

__all__ = ["AGENTS", "RETRIEVERS", "TOOLS"]
