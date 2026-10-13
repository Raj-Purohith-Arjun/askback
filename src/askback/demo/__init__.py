"""The built-in demo: what `askback eval` runs when you give it nothing to run.

A plain install ships no suite of yours and no agent of yours, so with no `--config` the
command grades a small canned stand-in agent against a bundled example suite, and says so. It
is a self-check and a worked example, not a test of anything real. The run is green, so a first
run shows what a passing result looks like; point it at your agent with `--config` to find out
whether yours passes.

Both the agent and the suite are package data, so this works from an installed wheel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent import DemoAdapter, DemoAgent

#: The bundled example suite, as a directory the dataset loader can read.
SUITE_DIR = Path(__file__).resolve().parent / "suites"

NOTICE = (
    "No --config given: grading the built-in demo agent against the bundled example suite. "
    "This is a self-check, not a test of your agent. Point it at yours with --config."
)


def demo_config() -> dict[str, Any]:
    """The configuration of that default run. No suite floors: nothing here names a suite."""
    return {
        "agent": {"adapter": "demo", "side_effecting_tools": []},
        "evaluation": {
            "dataset_dir": str(SUITE_DIR),
            "reports_dir": "reports",
            "redaction": "ci",
            "pass_threshold": 0.7,
            "suite_pass_rate": 0.8,
        },
    }


__all__ = ["NOTICE", "SUITE_DIR", "DemoAdapter", "DemoAgent", "demo_config"]
