"""Write a copy of a config file with the model judge switched on.

    python scripts/ci_judge_config.py SOURCE.yaml OUT.yaml

The shipped configs grade with the free heuristic judge, and choosing a model
means uncommenting `evaluation.judge.llm` by hand. A scheduled run cannot edit a
file by hand, so this makes the same edit to a copy, from the environment:

    JUDGE_PROVIDER                  openai | azure_openai | anthropic
    JUDGE_BUDGET_USD                the most one run may spend on the judge
    JUDGE_PRICE_PER_1K_PROMPT       your model's prices per 1,000 tokens
    JUDGE_PRICE_PER_1K_COMPLETION
    JUDGE_MODEL                     the model id; REQUIRED for anthropic and for an openai
                                    server at your own base URL (JUDGE_OPENAI_BASE_URL)

The first four are required, and all three numbers must be above zero. The
budget is counted in those prices, so a price of 0 is a ceiling that never
trips: this refuses to write one rather than leave a run that looks capped.

No credential is written. The judge reads its own JUDGE_-prefixed variables
(JUDGE_AZURE_OPENAI_API_KEY and so on) when it is built, and a run without them
stops instead of grading with the heuristic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml


def _positive(name: str) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise SystemExit(f"{name} must be a number above 0, got {raw!r}.")
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit(__doc__)
    source = Path(argv[1])
    provider = os.environ.get("JUDGE_PROVIDER", "").strip()
    if not provider:
        raise SystemExit("JUDGE_PROVIDER is not set: there is no model judge to switch on.")

    # Loaded raw, not through AppConfig: the ${VAR:default} references must
    # reach the copy unexpanded, so the run still reads its own environment.
    config = yaml.safe_load(source.read_text(encoding="utf-8"))
    judge = config["evaluation"].setdefault("judge", {})
    judge["llm"] = {"provider": provider, "temperature": 0.0}
    if model := os.environ.get("JUDGE_MODEL", "").strip():
        judge["llm"]["model"] = model
    judge["budget_usd"] = _positive("JUDGE_BUDGET_USD")
    judge["price_per_1k_prompt"] = _positive("JUDGE_PRICE_PER_1K_PROMPT")
    judge["price_per_1k_completion"] = _positive("JUDGE_PRICE_PER_1K_COMPLETION")

    target = Path(argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    print(f"Judge {provider!r} enabled, budget ${judge['budget_usd']:.2f}: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
