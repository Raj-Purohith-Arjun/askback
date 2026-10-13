"""The config a user is told to copy: it must load, must not trip its own gates, and must lead
with a judge that is not tied to one vendor.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from askback.adapters.factory import build_adapter
from askback.core.config import AppConfig
from askback.core.errors import ConfigError
from askback.evaluation.engine import EvaluationEngine
from askback.evaluation.gates import evaluate_gates

TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "askback.example.yaml"


def test_it_names_no_suite_and_sets_no_floor() -> None:
    evaluation = AppConfig.load(TEMPLATE).evaluation
    assert not {"default_suites", "suite_must_pass"} & set(evaluation)


def test_a_run_of_your_own_suite_passes_the_gates_the_template_sets() -> None:
    from test_gates import case, report  # one passing case, in a suite the template never heard of

    verdict = evaluate_gates(report(case("c1", "my-suite")), AppConfig.load(TEMPLATE).evaluation)
    assert verdict.exit_code == 0, [g.reason for g in verdict.gates]


def test_the_engine_builds_from_it() -> None:
    evaluators = EvaluationEngine.from_config(AppConfig.load(TEMPLATE).evaluation).describe()
    assert "pii_egress" in evaluators["evaluators"]
    assert "citation_existence" not in evaluators["evaluators"], "opt-in: only with a manifest"


def test_an_agent_url_you_have_not_set_is_refused_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("httpx")
    monkeypatch.delenv("AGENT_URL", raising=False)
    with pytest.raises(ConfigError, match="agent.url"):
        build_adapter(AppConfig.load(TEMPLATE))


def test_the_first_judge_example_is_a_server_you_run_yourself() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    first = {name: text.index(name) for name in ("base_url", "provider: azure_openai", "provider: anthropic")}
    assert first["base_url"] < first["provider: azure_openai"] < first["provider: anthropic"]
    assert "No API key" in text or "no API key" in text


def test_the_demo_adapter_and_the_template_are_the_only_defaults_that_ship() -> None:
    # The template is plain YAML: no step may need the simulator or an HR file.
    parsed = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert "routing" not in parsed and "retrieval" not in parsed and "llm" not in parsed


def test_an_evaluator_options_block_with_every_setting_commented_out_is_not_a_crash() -> None:
    # YAML reads `pii_egress:` followed only by comments as null. A user copying the template
    # and leaving the examples commented out hit `dict(None)` here.
    engine = EvaluationEngine.from_config(
        {"evaluator_options": {"pii_egress": None, "rag": None}, "evaluators": ["pii_egress", "rag"]}
    )
    assert [e.name for e in engine.evaluators] == ["pii_egress", "rag"]
    assert EvaluationEngine.from_config({"evaluator_options": None}).evaluators
