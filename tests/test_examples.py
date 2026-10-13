"""The examples the docs walk a stranger through, kept honest.

docs/quickstart.md tells a reader to run `askback eval` against examples/toy-agent
and promises two outcomes: red while the agent has its flaw, green once fixed. The
other examples back writing-an-adapter.md and writing-an-evaluator.md. If an adapter,
gate or evaluator change breaks one, the docs are wrong and this says so before a
reader does.

Nothing is written into the repository: reports go to tmp_path.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from optional_deps import requires

requires("httpx", extra="http", test_module="tests/test_examples.py")

from askback.cli import main  # noqa: E402
from askback.core.contracts import ChatResponse, ResponseStatus  # noqa: E402
from askback.core.errors import ConfigError  # noqa: E402
from askback.core.registry import EVALUATORS  # noqa: E402
from askback.datasets.schema import TestCase, Turn  # noqa: E402
from askback.evaluation.gates import EXIT_FAILED, EXIT_OK  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "toy-agent"


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_agent() -> Any:
    return _load(EXAMPLE / "agent.py")


def _run(tmp_path: Path, *, fixed: bool) -> int:
    server = _load_agent().make_server(0, fixed=fixed)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = yaml.safe_load((EXAMPLE / "config.yaml").read_text(encoding="utf-8"))
        config["agent"]["url"] = f"http://127.0.0.1:{server.server_port}/chat"
        config["evaluation"]["dataset_dir"] = str(EXAMPLE / "suites")
        config["evaluation"]["reports_dir"] = str(tmp_path / "reports")
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return main(["--config", str(path), "eval", "--quiet", "--format", "json"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _no_other_declarations(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The simulator declared its tools when conftest imported it. The quickstart
    # runs without it, so the config's own `side_effecting_tools` must be enough.
    monkeypatch.setattr("askback.evaluation.safety_eval._SIDE_EFFECT_SOURCES", [])
    yield


def test_the_flawed_agent_fails_the_run(tmp_path: Path) -> None:
    assert _run(tmp_path, fixed=False) == EXIT_FAILED


def test_the_fixed_agent_passes_the_run(tmp_path: Path) -> None:
    assert _run(tmp_path, fixed=True) == EXIT_OK


def test_the_python_adapter_example_passes() -> None:
    assert _load(EXAMPLE.parent / "python-adapter" / "run.py").main() == EXIT_OK


class TestCustomEvaluatorExample:
    """The worked example in docs/writing-an-evaluator.md, tested the way it teaches."""

    @pytest.fixture
    def evaluator(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        directory = EXAMPLE.parent / "custom-evaluator"
        monkeypatch.syspath_prepend(str(directory))
        # Registered by import. Drop it afterwards so a second import can register again.
        monkeypatch.setattr("askback.core.registry.EVALUATORS._items", dict(EVALUATORS._items))
        monkeypatch.delitem(sys.modules, "disclaimer", raising=False)
        module = _load(directory / "disclaimer.py")
        return module.DisclaimerEvaluator(phrase="not financial advice", tags=["financial"])

    @staticmethod
    def _grade(evaluator: Any, reply: str, status: ResponseStatus, *tags: str) -> Any:
        case = TestCase(id="c", turns=[Turn(user="q")], tags=list(tags))
        response = ChatResponse(reply=reply, conversation_id="s", status=status)
        return evaluator.evaluate(case, case.turns[0], response)

    def test_an_unreadable_reply_never_passes(self, evaluator: Any) -> None:
        for status in (ResponseStatus.UNKNOWN, ResponseStatus.ERROR):
            result = self._grade(evaluator, "Not financial advice.", status, "financial")
            assert not result.passed and not result.skipped

    def test_a_missing_disclaimer_fails_and_a_present_one_passes(self, evaluator: Any) -> None:
        answered = ResponseStatus.ANSWERED
        assert not self._grade(evaluator, "Buy it.", answered, "financial").passed
        assert self._grade(evaluator, "Buy it. NOT   financial advice.", answered, "financial").passed

    def test_a_case_it_does_not_apply_to_is_skipped_not_passed(self, evaluator: Any) -> None:
        result = self._grade(evaluator, "Sunny.", ResponseStatus.ANSWERED, "weather")
        assert result.skipped

    def test_a_refusal_is_skipped(self, evaluator: Any) -> None:
        assert self._grade(evaluator, "I cannot help.", ResponseStatus.REFUSED, "financial").skipped

    def test_an_empty_phrase_is_refused_at_construction(self, evaluator: Any) -> None:
        with pytest.raises(ConfigError, match="phrase"):
            type(evaluator)(phrase=" ", tags=["financial"])
