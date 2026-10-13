"""A canned stand-in agent, so `askback eval` has something to grade on a fresh install.

This is NOT a model of a real system and it is not a reference implementation. It is a few
dozen lines of keyword rules behind the adapter seam, written so the bundled example suite
(`suites/demo.yaml`) has something deterministic to run against. Its whole job is to make the
first run of the tool green and to show what a passing run looks like. It ships inside the
package, which is why it has no dependency on the simulator, and it speaks the same
`ChatResponse` every adapter does.

What it does, in order of the rules below: refuses a request for another customer's data,
remembers an order number said earlier in the session, asks which order a refund is for when it
does not know, answers a few questions from fixed text, and says it does not know about
anything else. Memory is per session, under a lock, so concurrent workers never share it.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping

from ..adapters.base import AdapterLifecycle
from ..core.contracts import ChatResponse, Intent, ResponseStatus
from ..core.errors import ConfigError

#: The labels a suite may send as. The agent has no notion of identity; these exist so that an
#: unknown label fails loudly instead of quietly running as someone else.
PRINCIPALS = frozenset({"default", "test_user"})

_ORDER = re.compile(r"\border(?:\s+(?:number|no\.?|id))?\s*(?:is\s*)?#?\s*(\d{3,})", re.IGNORECASE)

_ASK_WHICH_ORDER = (
    "Please share the order number you want refunded so I can look it up.\n\n"
    "Examples:\n"
    "- Order 1182\n"
    "- The email address used at checkout"
)


class _Memory:
    """What the agent remembers about one session."""

    def __init__(self) -> None:
        self.order: str | None = None
        self.awaiting_order_for_refund = False


class DemoAgent:
    """Deterministic keyword rules with per-session memory. Thread-safe."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Memory] = {}
        self._lock = threading.Lock()

    def respond(self, message: str, session_id: str) -> ChatResponse:
        text = message.lower()
        with self._lock:
            memory = self._sessions.setdefault(session_id, _Memory())
            found = _ORDER.search(message)
            if found:
                memory.order = found.group(1)
            order = memory.order
            was_waiting = memory.awaiting_order_for_refund

            def reply(
                body: str,
                *,
                route: str,
                intent: str,
                status: ResponseStatus = ResponseStatus.ANSWERED,
            ) -> ChatResponse:
                return ChatResponse(
                    reply=body,
                    conversation_id=session_id,
                    route=route,
                    status=status,
                    intent=Intent(name=intent, confidence=1.0),
                )

            if ("ignore" in text and "instruction" in text) or "another customer" in text:
                return reply(
                    "I can't help with that request. I can only discuss your own orders.",
                    route="safety_guard",
                    intent="refused",
                    status=ResponseStatus.REFUSED,
                )
            if "which order" in text and ("mention" in text or "gave" in text or "told" in text):
                if order:
                    return reply(
                        f"You told me earlier that your order number is {order}.",
                        route="support_agent",
                        intent="recall",
                    )
                return reply(
                    "You have not given me an order number yet.",
                    route="support_agent",
                    intent="recall",
                    status=ResponseStatus.NO_ANSWER,
                )
            if "charged twice" in text or "double charge" in text:
                return reply(
                    "Duplicate charges are refunded to the original payment method "
                    "within 5 business days.",
                    route="billing_agent",
                    intent="billing",
                )
            if "refund" in text or "money back" in text:
                if "definitely" in text or "guarantee" in text or "certain" in text:
                    return reply(
                        "Refunds are decided after the returned item is inspected, "
                        "so approval is not automatic.",
                        route="billing_agent",
                        intent="billing",
                    )
                if order:
                    memory.awaiting_order_for_refund = False
                    return reply(
                        f"I have started a refund review for order {order}. "
                        "You will hear back within 5 business days.",
                        route="billing_agent",
                        intent="billing",
                    )
                memory.awaiting_order_for_refund = True
                return reply(_ASK_WHICH_ORDER, route="billing_agent", intent="billing")
            if was_waiting and found:
                memory.awaiting_order_for_refund = False
                return reply(
                    f"Thanks. I have started a refund review for order {found.group(1)}. "
                    "You will hear back within 5 business days.",
                    route="billing_agent",
                    intent="billing",
                )
            if "where is" in text or "tracking" in text or "delivery" in text:
                if order:
                    return reply(
                        f"Order {order} is in transit with the carrier and is due within 3 days.",
                        route="shipping_agent",
                        intent="shipping",
                    )
                return reply(
                    "Please tell me your order number and I will look up the delivery.",
                    route="shipping_agent",
                    intent="shipping",
                )
            if "return" in text:
                return reply(
                    "Returns are accepted within 30 days of delivery for unused items, "
                    "so an item bought more than 30 days ago is outside the window.",
                    route="policy_agent",
                    intent="returns",
                )
            if found:
                return reply(
                    f"Thanks, I have noted order {found.group(1)}.",
                    route="support_agent",
                    intent="order_info",
                )
            return reply(
                "I don't have information about that.",
                route="fallback_agent",
                intent="unknown",
                status=ResponseStatus.NO_ANSWER,
            )


class DemoAdapter(AdapterLifecycle):
    """`AgentAdapter` over the built-in demo agent."""

    def __init__(self, agent: DemoAgent | None = None) -> None:
        super().__init__()
        self._agent = agent or DemoAgent()

    def send(
        self, message: str, *, session_id: str, principal: str = "default"
    ) -> ChatResponse:
        self._require_open("send")
        if principal not in PRINCIPALS:
            raise ConfigError(
                f"unknown principal {principal!r}; the demo agent knows: {sorted(PRINCIPALS)}"
            )
        return self._agent.respond(message, session_id)

    def describe(self) -> dict[str, str]:
        self._require_open("describe")
        return {"adapter": "demo", "agent": "built-in canned demo agent (not a real system)"}

    @classmethod
    def from_config(cls, agent: Mapping[str, object]) -> DemoAdapter:
        stray = sorted(set(agent) - {"adapter", "side_effecting_tools"})
        if stray:
            raise ConfigError(f"unknown agent key(s) for the demo adapter: {stray}")
        return cls()


__all__ = ["PRINCIPALS", "DemoAdapter", "DemoAgent"]
