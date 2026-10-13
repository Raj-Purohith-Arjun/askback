"""Shared fixtures, plus the strict-environment hooks.

Everything here is built from fakes, not the real corpus, so the unit tests stay
fast and independent of the data files.

The hooks below exist because a skipped test module is indistinguishable from a
passing one in pytest's summary line. See tests/optional_deps.py.
"""

from __future__ import annotations

import os

import pytest

from askback_sim.agents.frontline import FrontLineAgent, IntentSpec, KeywordIntentClassifier
from askback_sim.agents.specialized import FallbackAgent, KnowledgeAgent, SmallTalkAgent
from askback_sim.engine import ConversationEngine
from askback_sim.llm.extractive import ExtractiveProvider
from askback_sim.retrieval.base import Document
from askback_sim.retrieval.bm25 import BM25Retriever
from askback_sim.safety import SafetyPipeline
from askback_sim.tools import build_toolbox
from optional_deps import (
    COLLECTED,
    INSTALL_HINT,
    MISSING,
    SELECTION,
    load_baseline,
    resolve_strict,
    set_strict,
)

# --------------------------------------------------------------------------- #
# Strict environment
# --------------------------------------------------------------------------- #


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--strict-env",
        action="store_true",
        default=False,
        help=(
            "Refuse to skip a test module for a missing optional dependency, and "
            "assert the recorded collection baseline. What CI runs."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    set_strict(resolve_strict(bool(config.getoption("--strict-env"))))
    SELECTION["subset"] = bool(
        config.option.file_or_dir or config.option.keyword or config.option.markexpr
    )
    config.addinivalue_line(
        "markers",
        "strict_env: asserts something about the run itself; needs every optional extra",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Record what was collected, and park the strict-only items when not strict."""
    COLLECTED.clear()
    for item in items:
        module = item.location[0].replace(os.sep, "/")
        COLLECTED[module] = COLLECTED.get(module, 0) + 1

    if resolve_strict(bool(config.getoption("--strict-env"))):
        return

    parked = pytest.mark.skip(reason="needs --strict-env and every optional extra")
    for item in items:
        if "strict_env" in item.keywords:
            item.add_marker(parked)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Say what this run did not test. Loudly, and at the end where it is read."""
    if not MISSING:
        return

    per_module = load_baseline()["minimum_per_module"]
    reporter = terminalreporter
    reporter.write_sep("=", "MODULES NOT TESTED", red=True, bold=True)
    for record in MISSING:
        lost = per_module.get(record.test_module)
        count = f"at least {lost} tests" if lost else "an unknown number of tests"
        reporter.write_line(
            f"  {record.test_module}: needs '{record.dependency}' from the "
            f"[{record.extra}] extra - {count} not collected"
        )
    reporter.write_line(f"  This run proved nothing about those modules. {INSTALL_HINT}")
    reporter.write_line("  CI runs --strict-env, where this is an error rather than a note.")


@pytest.fixture
def documents() -> list[Document]:
    return [
        Document(
            doc_id="POL-1",
            title="Annual Leave",
            text=(
                "Full-time employees receive 24 days of paid annual leave per calendar year.\n\n"
                "A maximum of 5 unused annual leave days may be carried over."
            ),
            metadata={"domain": "policy", "tenant": "default"},
        ),
        Document(
            doc_id="POL-2",
            title="Expenses",
            text="Expense claims must be submitted within 30 days of the expense date.",
            metadata={"domain": "policy", "tenant": "default"},
        ),
        Document(
            doc_id="SECRET-1",
            title="Acme Pay Bands",
            text="Acme grade 5 engineers are paid between 96,000 and 118,000 units.",
            metadata={"domain": "policy", "tenant": "acme"},
        ),
    ]


@pytest.fixture
def retriever(documents: list[Document]) -> BM25Retriever:
    r = BM25Retriever()
    r.index(documents)
    return r


@pytest.fixture
def intents() -> list[IntentSpec]:
    return [
        IntentSpec(
            name="leave_policy",
            route="policy_agent",
            keywords=["leave", "holiday"],
            examples=["how many days of annual leave do I get"],
        ),
        IntentSpec(
            name="expense_policy",
            route="policy_agent",
            keywords=["expense", "claim"],
            examples=["how long do I have to submit an expense claim"],
        ),
        IntentSpec(
            name="compensation",
            route="policy_agent",
            keywords=["compensation", "pay band", "grade"],
            examples=["what is the pay band for grade 5 engineers"],
        ),
        IntentSpec(
            name="greeting",
            route="smalltalk_agent",
            keywords=["hello", "hi"],
            examples=["hello there"],
        ),
    ]


@pytest.fixture
def engine(retriever: BM25Retriever, intents: list[IntentSpec]) -> ConversationEngine:
    frontline = FrontLineAgent(
        KeywordIntentClassifier(intents),
        {spec.name: spec.route for spec in intents},
    )
    return ConversationEngine(
        llm=ExtractiveProvider(),
        retriever=retriever,
        tools=build_toolbox({"retriever": retriever}),
        frontline=frontline,
        agents={
            "policy_agent": KnowledgeAgent(name="policy_agent", tools=["knowledge_search"]),
            "smalltalk_agent": SmallTalkAgent(name="smalltalk_agent", tools=[]),
            "fallback_agent": FallbackAgent(name="fallback_agent", tools=[]),
        },
        safety=SafetyPipeline.from_config({}),
    )
