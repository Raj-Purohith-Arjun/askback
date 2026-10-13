"""The CI workflow, held to what it promises: a clean pull request is green, and nothing floats.

CI cannot be run from here, so what is checked is the file: every action pinned to a commit SHA,
the demonstration kept out of the gate, no `pull_request_target`, the Python matrix equal to what
pyproject claims, and the step that matters most (the plain-install check) present.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "evaluate.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True, which is a YAML 1.1 quirk, not a typo.
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    loaded["on"] = loaded.pop(True)
    return loaded


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for job in workflow["jobs"].values() for s in job.get("steps", [])]


def test_every_action_is_pinned_to_a_full_commit_sha_with_its_release_noted() -> None:
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", WORKFLOW.read_text("utf-8"), re.MULTILINE)
    assert uses, "the scan found no actions at all"
    for ref, rest in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a SHA"
        assert re.search(r"#\s*v\d+\.\d+\.\d+", rest), f"{ref} has no release comment"


def test_it_never_uses_pull_request_target(workflow: dict[str, Any]) -> None:
    assert "pull_request_target" not in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}


def test_the_hr_demonstration_can_never_fail_a_pull_request(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    for name in ("demo-hr", "nightly"):
        assert jobs[name]["continue-on-error"] is True, name
    assert "DEMONSTRATION" in jobs["demo-hr"]["name"]


def test_the_gating_jobs_do_not_touch_the_hr_example(workflow: dict[str, Any]) -> None:
    for name in ("checks", "wheel", "eval"):
        commands = " ".join(s.get("run", "") for s in workflow["jobs"][name]["steps"])
        assert "examples/hr" not in commands.replace("src tests tools examples", ""), name


def test_the_pr_gate_runs_the_builtin_demo_with_no_config(workflow: dict[str, Any]) -> None:
    commands = " ".join(s.get("run", "") for s in workflow["jobs"]["eval"]["steps"])
    assert "askback eval" in commands and "--config" not in commands
    assert "--compare-baseline" not in commands, "the demo has no baseline"


def test_the_plain_install_is_checked_from_an_empty_directory(workflow: dict[str, Any]) -> None:
    commands = " ".join(s.get("run", "") for s in workflow["jobs"]["wheel"]["steps"])
    assert "build --wheel" in commands and "mkdir" in commands and "askback\" eval" in commands
    assert "askback_sim" in commands, "the wheel must be checked for the simulator"


def test_the_python_matrix_is_what_pyproject_claims(workflow: dict[str, Any]) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    matrix = workflow["jobs"]["checks"]["strategy"]["matrix"]["python"]
    claimed = {
        c.rsplit(" ", 1)[-1]
        for c in project["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    }
    assert set(matrix) == claimed, (matrix, claimed)
    assert project["requires-python"] == f">={min(matrix)}"


def test_dev_tools_are_installed_through_the_constraints_file(workflow: dict[str, Any]) -> None:
    installs = [s["run"] for s in _steps(workflow) if "pip install" in s.get("run", "") and "dev" in s["run"]]
    assert installs and all("-c constraints.txt" in i for i in installs)
    pinned = {
        line.split("==")[0].lower()
        for line in (ROOT / "constraints.txt").read_text("utf-8").splitlines()
        if "==" in line
    }
    assert {"ruff", "mypy", "pytest"} <= pinned
