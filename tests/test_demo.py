"""The built-in demo: what a plain install grades when you give it nothing.

A green first run is only worth shipping if the suite behind it can also fail. These tests hold
that line: the suite covers each kind of expectation it claims to, a broken agent fails it, and
the bundled files contain no HR vocabulary (they are the neutral default, and the HR example
lives under examples/hr).
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

import pytest

from askback.adapters.scripted import ScriptedAdapter
from askback.core.config import AppConfig
from askback.core.contracts import ChatResponse
from askback.core.errors import ConfigError
from askback.datasets.loader import load_cases
from askback.demo import SUITE_DIR, DemoAdapter, demo_config
from askback.evaluation.engine import EvaluationEngine
from askback.runner import TestRunner

ROOT = Path(__file__).resolve().parents[1]
DEMO_FILES = [SUITE_DIR / "demo.yaml", ROOT / "src" / "askback" / "demo" / "agent.py"]

#: Words that would make the neutral default read as an HR assistant.
HR_WORDS = re.compile(
    r"\b(?:HR|leave|payroll|salary|employees?|grievance|harassment|probation|handbook|"
    r"manager|sabbatical|disciplinary|onboarding)\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def pyproject() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _run(adapter: object) -> tuple[list[bool], object]:
    engine = EvaluationEngine.from_config(demo_config()["evaluation"])
    report = TestRunner(adapter, engine, workers=1).run(load_cases(SUITE_DIR))  # type: ignore[arg-type]
    return [c.passed for c in report.cases], report


class TestTheSuite:
    def test_the_demo_agent_passes_every_case(self) -> None:
        verdicts, report = _run(DemoAdapter())
        assert all(verdicts) and len(verdicts) == 8, report.summary()
        assert report.summary()["critical_failures"] == 0

    def test_it_is_small_and_covers_each_kind_of_expectation_it_claims(self) -> None:
        cases = load_cases(SUITE_DIR)
        turns = [t for c in cases for t in c.turns]
        expects = [t.expect for t in turns]
        assert 5 <= len(cases) <= 8
        assert any(e.expected_route for e in expects), "routing"
        assert any(e.required_facts for e in expects), "a required fact"
        assert any(e.prohibited_content for e in expects), "a prohibited term"
        assert any(e.must_refuse for e in expects), "a refusal"
        assert any(e.expected_status == "no_answer" for e in expects), "saying it does not know"
        assert any(t.clarification is not None for t in turns), "a clarification"
        assert any(len(c.turns) > 1 for c in cases), "a multi-turn case"

    def test_a_broken_agent_fails_it_so_a_green_run_means_something(self) -> None:
        agreeable = ScriptedAdapter(
            replies={}, default=ChatResponse(reply="Sure, anything you like!", conversation_id="s")
        )
        verdicts, _ = _run(agreeable)
        assert sum(verdicts) <= 2, "an agent that says yes to everything must not pass"

    def test_an_agent_that_never_asks_fails_the_clarification_case(self) -> None:
        class NeverAsks(DemoAdapter):
            def send(self, message: str, *, session_id: str, principal: str = "default") -> ChatResponse:
                reply = super().send(message, session_id=session_id, principal=principal)
                if "Please share" in reply.reply:
                    reply.reply = "Refund approved for the order."
                return reply

        verdicts, report = _run(NeverAsks())
        failed = {c.case_id for c in report.cases if not c.passed}  # type: ignore[attr-defined]
        assert failed == {"clarifies-then-answers"}, failed
        assert not all(verdicts)

    def test_the_bundled_files_contain_no_hr_vocabulary(self) -> None:
        for path in DEMO_FILES:
            found = sorted({m.group(0).lower() for m in HR_WORDS.finditer(path.read_text("utf-8"))})
            assert not found, f"{path.name} reads as an HR assistant: {found}"


class TestTheAgent:
    def test_sessions_do_not_share_memory(self) -> None:
        with DemoAdapter() as agent:
            agent.send("My order number is 1182.", session_id="a")
            mine = agent.send("Which order number did I mention earlier?", session_id="a")
            theirs = agent.send("Which order number did I mention earlier?", session_id="b")
        assert "1182" in mine.reply and "1182" not in theirs.reply

    def test_an_unknown_principal_is_refused_not_run_as_someone(self) -> None:
        with DemoAdapter() as agent, pytest.raises(ConfigError, match="unknown principal"):
            agent.send("hi", session_id="a", principal="admin")

    def test_it_says_what_it_is_in_the_run_context(self) -> None:
        with DemoAdapter() as agent:
            assert agent.describe()["adapter"] == "demo"
            assert "not a real system" in agent.describe()["agent"]


class TestPackaging:
    """The wheel has to carry the demo and must not carry the simulator."""

    def test_the_suite_is_declared_as_package_data(self, pyproject: dict) -> None:
        data = pyproject["tool"]["setuptools"]["package-data"]
        assert "suites/*.yaml" in data["askback.demo"]
        assert (SUITE_DIR / "demo.yaml").is_file()

    def test_the_simulator_is_not_a_shipped_package(self, pyproject: dict) -> None:
        # What setuptools' `find` does with `include`: every directory under src that holds an
        # `__init__.py`, kept if its dotted name matches a pattern. Done by hand so this test
        # does not need setuptools installed.
        patterns = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
        src = ROOT / "src"
        found = [
            ".".join(p.parent.relative_to(src).parts)
            for p in src.rglob("__init__.py")
            if "__pycache__" not in p.parts
        ]
        shipped = [name for name in found if any(fnmatch.fnmatch(name, pat) for pat in patterns)]
        assert "askback.demo" in shipped and "askback.adapters" in shipped
        assert "askback_sim" in found, "the simulator exists in the checkout..."
        assert not [p for p in shipped if p.split(".")[0] == "askback_sim"], "...and does not ship"

    def test_the_demo_config_names_no_suite_and_sets_no_floor(self) -> None:
        evaluation = AppConfig.from_mapping(demo_config()).evaluation
        assert not {"default_suites", "suite_must_pass"} & set(evaluation)
