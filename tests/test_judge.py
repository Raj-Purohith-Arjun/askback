"""The model judge: degradation, the verdict cache, cost, and the budget ceiling.

The judge's model is scripted, not patched: `_ScriptedJudgeLLM` is a real
`LLMProvider` whose replies are decided by the test, as `ScriptedAdapter` is for
the agent. Nothing here reaches a network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_gates import case, gate_for, report

from askback.adapters.scripted import ScriptedAdapter
from askback.cli import main
from askback.core.config import AppConfig, judge_budget
from askback.core.contracts import ChatResponse
from askback.core.errors import ConfigError
from askback.datasets.schema import Expectations, TestCase, Turn
from askback.evaluation.engine import EvaluationEngine
from askback.evaluation.gates import EXIT_ERROR, EXIT_OK, JUDGE_DEGRADED, evaluate_gates
from askback.evaluation.judge_cache import VerdictCache
from askback.evaluation.llm_judge import (
    DIMENSIONS,
    RATIONALE_NOT_CACHED,
    LLMJudgeEvaluator,
    ModelJudge,
    build_judge,
)
from askback.providers.base import LLM_PROVIDERS, LLMProvider, LLMResponse
from askback.runner import TestRunner

GOOD = json.dumps({**dict.fromkeys(DIMENSIONS, 0.9), "rationale": "fine"})


class _ScriptedJudgeLLM(LLMProvider):
    """A remote model whose reply to each question is decided by the test.

    `fail_on` is a substring of the question; a call about it raises. Every call
    is counted, so a cache hit is visible as a call that did not happen.
    """

    name = "scripted-judge"
    remote = True
    #: Class-level so a provider built from config (the CLI tests) is steerable too.
    fail_on: tuple[str, ...] = ()
    reply = GOOD
    prompt_tokens = 1000
    completion_tokens = 500
    calls = 0

    def __init__(self, model: str = "m-1", env_prefix: str = "", **_: Any) -> None:
        self.model = model
        self.env_prefix = env_prefix

    def generate(self, messages: Any, *, context: Any = (), **options: Any) -> LLMResponse:
        type(self).calls += 1
        if any(marker in messages[1].content for marker in self.fail_on):
            raise TimeoutError("judge endpoint timed out")
        return LLMResponse(
            text=self.reply,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )

    def is_available(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def scripted_llm(monkeypatch: pytest.MonkeyPatch) -> type[_ScriptedJudgeLLM]:
    monkeypatch.setitem(LLM_PROVIDERS._items, "scripted-judge", _ScriptedJudgeLLM)
    for name, value in dict(fail_on=(), reply=GOOD, calls=0).items():
        monkeypatch.setattr(_ScriptedJudgeLLM, name, value)
    return _ScriptedJudgeLLM


def _case(case_id: str) -> TestCase:
    turn = Turn(
        user=f"question {case_id}",
        expect=Expectations(reference_answer="Employees receive 24 days of paid leave."),
    )
    return TestCase(id=case_id, turns=[turn], suite="golden")


def _run(
    judge: ModelJudge, case_ids: list[str], *, budget: float | None = None
) -> Any:
    engine = EvaluationEngine([LLMJudgeEvaluator(judge=judge)])
    adapter = ScriptedAdapter(
        replies={}, default=ChatResponse(reply="You get 24 days of paid leave.", conversation_id="s")
    )
    return TestRunner(adapter, engine, workers=1, judge_budget_usd=budget).run(
        [_case(c) for c in case_ids]
    )


class TestDegradation:
    def test_a_failed_call_is_marked_degraded_not_quietly_lexical(self) -> None:
        _ScriptedJudgeLLM.fail_on = ("question b",)

        run = _run(ModelJudge(_ScriptedJudgeLLM()), ["a", "b"])

        ok, bad = (c.results[0] for c in run.cases)
        assert ok.degraded is None
        assert bad.degraded is not None and "TimeoutError" in bad.degraded
        assert all("DEGRADED" in c.detail for c in bad.checks)
        assert run.summary()["judge_degraded"] == 1

    def test_an_unparseable_reply_degrades_too(self) -> None:
        _ScriptedJudgeLLM.reply = "I cannot grade this."

        run = _run(ModelJudge(_ScriptedJudgeLLM()), ["a"])

        assert run.cases[0].results[0].degraded is not None

    def test_max_degraded_gates_the_run_and_names_the_count(self) -> None:
        _ScriptedJudgeLLM.fail_on = ("question b",)
        run = _run(ModelJudge(_ScriptedJudgeLLM()), ["a", "b"])

        refused = gate_for(evaluate_gates(run, {}), JUDGE_DEGRADED)
        assert (refused.passed, refused.exit_code) == (False, EXIT_ERROR)
        assert "1 judged response(s)" in refused.reason and "max_degraded is 0" in refused.reason
        assert [o.label for o in refused.offenders] == ["golden/b"]

        allowed = evaluate_gates(run, {"judge": {"max_degraded": 1}})
        assert gate_for(allowed, JUDGE_DEGRADED).passed

    def test_a_run_the_heuristic_graded_by_choice_has_no_degradation_gate(self) -> None:
        verdict = evaluate_gates(report(case("c", "golden")), {})
        assert all(g.gate != JUDGE_DEGRADED for g in verdict.gates)

    @pytest.mark.parametrize("bad", [-1, 1.5, "none", True])
    def test_max_degraded_must_be_a_whole_number_not_negative(self, bad: Any) -> None:
        with pytest.raises(ConfigError, match="max_degraded"):
            evaluate_gates(
                _run(ModelJudge(_ScriptedJudgeLLM()), ["a"]), {"judge": {"max_degraded": bad}}
            )


class TestCache:
    def test_a_cached_verdict_costs_nothing_and_makes_no_second_call(
        self, tmp_path: Path
    ) -> None:
        cache = VerdictCache(tmp_path, DIMENSIONS)
        first = _run(ModelJudge(_ScriptedJudgeLLM(), cache=cache, price_per_1k_prompt=1.0), ["a"])
        # A new judge on the same directory: the verdict came from disk, not memory.
        second = _run(ModelJudge(_ScriptedJudgeLLM(), cache=cache, price_per_1k_prompt=1.0), ["a"])

        assert _ScriptedJudgeLLM.calls == 1
        assert first.summary()["judge_cost_usd"] == pytest.approx(1.0)
        assert second.summary()["judge_cost_usd"] == 0
        assert second.summary()["judge_calls"] == 0 and second.summary()["judge_cache_hits"] == 1
        assert second.cases[0].score == first.cases[0].score

    def test_the_rationale_never_reaches_disk_and_a_hit_says_it_is_unavailable(
        self, tmp_path: Path
    ) -> None:
        cache = VerdictCache(tmp_path, DIMENSIONS)
        first = _run(ModelJudge(_ScriptedJudgeLLM(), cache=cache), ["a"])
        [entry] = tmp_path.glob("*.json")
        assert json.loads(entry.read_text(encoding="utf-8")) == {
            "scores": dict.fromkeys(DIMENSIONS, 0.9)
        }
        # An entry written before the rationale was dropped still carries one.
        entry.write_text(
            json.dumps({"scores": dict.fromkeys(DIMENSIONS, 0.9), "rationale": "quotes the reply"}),
            encoding="utf-8",
        )

        second = _run(ModelJudge(_ScriptedJudgeLLM(), cache=cache), ["a"])

        assert all("fine" in c.detail for c in first.cases[0].results[0].checks)
        details = [c.detail for c in second.cases[0].results[0].checks]
        assert all(RATIONALE_NOT_CACHED in d and "quotes the reply" not in d for d in details)

    def test_the_key_includes_model_and_prompt_version(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path, DIMENSIONS)
        _run(ModelJudge(_ScriptedJudgeLLM("m-1"), cache=cache), ["a"])
        _run(ModelJudge(_ScriptedJudgeLLM("m-2"), cache=cache), ["a"])
        _run(ModelJudge(_ScriptedJudgeLLM("m-1"), cache=cache, prompt_version="2"), ["a"])

        assert _ScriptedJudgeLLM.calls == 3

    def test_a_degraded_verdict_is_never_cached(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path, DIMENSIONS)
        _ScriptedJudgeLLM.fail_on = ("question a",)
        _run(ModelJudge(_ScriptedJudgeLLM(), cache=cache), ["a"])

        _ScriptedJudgeLLM.fail_on = ()
        recovered = _run(ModelJudge(_ScriptedJudgeLLM(), cache=cache), ["a"])

        assert recovered.cases[0].results[0].degraded is None
        assert _ScriptedJudgeLLM.calls == 2

    def test_a_corrupt_entry_is_a_miss_not_an_error(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path, DIMENSIONS)
        _run(ModelJudge(_ScriptedJudgeLLM(), cache=cache), ["a"])
        for entry in tmp_path.glob("*.json"):
            entry.write_text("{not json", encoding="utf-8")

        _run(ModelJudge(_ScriptedJudgeLLM(), cache=cache), ["a"])

        assert _ScriptedJudgeLLM.calls == 2


class TestCostAndBudget:
    def test_tokens_and_cost_are_summed_onto_the_report(self) -> None:
        judge = ModelJudge(
            _ScriptedJudgeLLM(), price_per_1k_prompt=0.01, price_per_1k_completion=0.03
        )
        summary = _run(judge, ["a", "b"]).summary()

        # Per call: 1000 prompt tokens * 0.01/1k + 500 completion * 0.03/1k = 0.025.
        assert summary["judge_calls"] == 2
        assert (summary["judge_prompt_tokens"], summary["judge_completion_tokens"]) == (2000, 1000)
        assert summary["judge_cost_usd"] == pytest.approx(0.05)

    def test_the_budget_stops_the_run_and_the_run_reaches_no_verdict(self) -> None:
        judge = ModelJudge(_ScriptedJudgeLLM(), price_per_1k_prompt=0.01)  # $0.01 a case

        run = _run(judge, ["a", "b", "c", "d"], budget=0.015)

        # After case b the spend ($0.02) exceeds the ceiling, so c and d never start.
        assert [c.case_id for c in run.cases] == ["a", "b"]
        assert run.incomplete is not None
        assert (run.incomplete["cases_run"], run.incomplete["cases_planned"]) == (2, 4)
        verdict = evaluate_gates(run, {})
        assert [g.gate for g in verdict.gates] == ["judge_budget"]
        assert verdict.exit_code == EXIT_ERROR and "2 of 4" in verdict.gates[0].reason

    def test_a_run_within_budget_is_complete(self) -> None:
        run = _run(ModelJudge(_ScriptedJudgeLLM(), price_per_1k_prompt=0.01), ["a", "b"], budget=1.0)

        assert run.incomplete is None and len(run.cases) == 2

    def test_a_negative_budget_or_price_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="budget_usd"):
            judge_budget({"judge": {"budget_usd": -1}})
        with pytest.raises(ConfigError, match="price_per_1k_prompt"):
            build_judge({"llm": {"provider": "scripted-judge"}, "price_per_1k_prompt": -1})


def _cli(tmp_path: Path, judge: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """`askback eval` over three scripted-judge cases, simulator in the loop."""
    raw = AppConfig.load().raw
    # The repo's per-suite floors name suites this three-case dataset lacks, which
    # is its own exit 3; clear them so every 3 below is the judge's doing.
    raw["evaluation"]["suite_must_pass"] = {}
    raw["evaluation"]["judge"] = {
        "llm": {"provider": "scripted-judge"},
        "cache_path": str(tmp_path / "cache"),
        "price_per_1k_prompt": 0.01,
        **judge,
    }
    config = tmp_path / "app.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cases = [
        {
            "id": f"j-{i}",
            "turns": [
                {
                    "user": f"How many days of annual leave do I get each year? ({i})",
                    "expect": {"reference_answer": "24 days of paid annual leave."},
                }
            ],
        }
        for i in range(3)
    ]
    (tmp_path / "ds.yaml").write_text(
        yaml.safe_dump({"schema_version": 2, "cases": cases}), encoding="utf-8"
    )
    code = main(
        [
            "--config", str(config), "eval", "--quiet", "--dataset", str(tmp_path / "ds.yaml"),
            "--reports-dir", str(tmp_path / "reports"), "--workers", "1",
        ]
    )
    return code, capsys.readouterr().out


class TestCli:
    def test_the_summary_line_shows_judge_calls_and_estimated_cost(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Seeded cases in this dataset are all judged: a call each, $0.01 each.
        code, out = _cli(tmp_path, {"max_degraded": 3}, capsys)

        assert "judge calls 3 est. $0.0300" in out
        assert code in (EXIT_OK, 1)  # gated on the simulator's answers, not on the judge

    def test_an_outage_is_a_visibly_degraded_run_that_exits_3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _ScriptedJudgeLLM.fail_on = ("(1)",)

        code, out = _cli(tmp_path, {}, capsys)

        assert code == EXIT_ERROR
        assert "degraded 1" in out
        assert "1 judged response(s) were graded by the heuristic fallback" in out

    def test_exceeding_the_budget_stops_the_run_and_exits_3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _cli(tmp_path, {"budget_usd": 0.015}, capsys)

        assert code == EXIT_ERROR
        assert "judge budget" in out and "2 of 3" in out
        assert "passed" not in out  # no pass rate for cases that were never all run
        written = json.loads(next((tmp_path / "reports").glob("*.json")).read_text("utf-8"))
        assert written["incomplete"]["cases_run"] == 2
