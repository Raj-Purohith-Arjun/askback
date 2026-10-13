"""Built-in tools. Generic on purpose: replace them with your real integrations.

Each one is a small, complete example of a different tool shape:
  - knowledge_search : read-only, needs runtime context (the retriever)
  - calculator       : pure function, no I/O
  - create_ticket    : side-effecting, must never fire on a read-only question
  - business_days    : deterministic domain logic
"""

from __future__ import annotations

import ast
import operator
import uuid
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, ClassVar

from askback.core.errors import ToolError

from ..registry import TOOLS
from .base import Tool


@TOOLS.register("knowledge_search")
class KnowledgeSearchTool(Tool):
    """Search the knowledge base. The read path every grounded agent uses."""

    name: ClassVar[str] = "knowledge_search"
    description: ClassVar[str] = "Search internal documentation for relevant passages."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language search query."},
            "top_k": {"type": "integer", "description": "How many passages to return."},
        },
        "required": ["query"],
    }
    #: Tells build_toolbox to hand this tool the runtime context.
    wants_context: ClassVar[bool] = True

    def __init__(self, retriever: Any = None, **_: Any) -> None:
        self.retriever = retriever

    # Each tool names its own parameters, so `run` narrows `Tool.run(**kwargs)`.
    # Deliberate throughout this module: tools are called with keyword
    # arguments taken from a model's tool call, never positionally via the base.
    def run(  # type: ignore[override]  # see above
        self, query: str, top_k: int = 5, tenant: str = "default", **_: Any
    ) -> Any:
        if self.retriever is None:
            raise ToolError("knowledge_search has no retriever attached.")
        hits = self.retriever.search(query, top_k=top_k, tenant=tenant)
        return [
            {"doc_id": h.doc_id, "title": h.title, "score": h.score, "text": h.text}
            for h in hits
        ]


@TOOLS.register("calculator")
class CalculatorTool(Tool):
    """Evaluate arithmetic safely, via AST inspection rather than `eval`."""

    name: ClassVar[str] = "calculator"
    description: ClassVar[str] = "Evaluate an arithmetic expression, e.g. '18 * 0.75'."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    }

    _OPS: ClassVar[dict[type, Callable[..., float]]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def run(self, expression: str, **_: Any) -> float:  # type: ignore[override]  # narrows by design
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ToolError(f"Not a valid expression: {expression!r}") from exc
        return self._eval(tree.body)

    def _eval(self, node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in self._OPS:
            return self._OPS[type(node.op)](self._eval(node.left), self._eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._OPS:
            return self._OPS[type(node.op)](self._eval(node.operand))
        raise ToolError("Only numbers and + - * / // % ** are allowed.")


@TOOLS.register("create_ticket")
class CreateTicketTool(Tool):
    """Raise a support ticket. Side-effecting, so the safety suite watches it."""

    name: ClassVar[str] = "create_ticket"
    description: ClassVar[str] = "Create a support ticket for a request needing a human."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "category": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["summary"],
    }
    side_effecting: ClassVar[bool] = True

    def run(  # type: ignore[override]  # narrows by design
        self, summary: str, category: str = "general", priority: str = "normal", **_: Any
    ) -> dict[str, str]:
        if not summary.strip():
            raise ToolError("A ticket needs a non-empty summary.")
        # Replace with your ITSM client. The shape of the return value is what
        # agents and reports depend on, so keep these keys if you swap it out.
        return {
            "ticket_id": f"TKT-{uuid.uuid4().hex[:8].upper()}",
            "summary": summary.strip(),
            "category": category,
            "priority": priority,
            "status": "open",
        }


@TOOLS.register("business_days")
class BusinessDaysTool(Tool):
    """Add working days to a date, skipping weekends."""

    name: ClassVar[str] = "business_days"
    description: ClassVar[str] = "Add N business days to a date (YYYY-MM-DD)."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "days": {"type": "integer"},
        },
        "required": ["start_date", "days"],
    }

    def run(self, start_date: str, days: int, **_: Any) -> str:  # type: ignore[override]  # narrows by design
        try:
            current = date.fromisoformat(start_date)
        except ValueError as exc:
            raise ToolError(f"start_date must be YYYY-MM-DD, got {start_date!r}") from exc

        step = 1 if days >= 0 else -1
        remaining = abs(int(days))
        while remaining:
            current += timedelta(days=step)
            if current.weekday() < 5:  # Monday-Friday
                remaining -= 1
        return current.isoformat()
