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


def test_the_hr_example_passes_only_on_its_expected_exit(workflow: dict[str, Any]) -> None:
    # The example is red on purpose, so exit 1 is its expected result. A regression (2), a run that
    # could not be judged (3) and an unexpected green (0) must each fail the job.
    run = next(s["run"] for s in workflow["jobs"]["demo-hr"]["steps"] if "Evaluate" in s.get("name", ""))
    assert "--compare-baseline" in run and "PIPESTATUS[0]" in run
    body = run.split('case "$rc" in', 1)[1].rsplit("esac", 1)[0]
    pieces = re.split(r"^ {2}(\d|\*)\)", body, flags=re.M)  # [preamble, label, text, label, text...]
    arms = dict(zip(pieces[1::2], pieces[2::2], strict=True))
    assert set(arms) == {"1", "2", "3", "0", "*"}, sorted(arms)
    passes = re.compile(r"^\s*exit 0\b", re.M)  # an actual `exit 0`, not the words in a message
    assert passes.search(arms["1"]) and "-f reports/demo-hr.json" in arms["1"], "exit 1 needs a report"
    for code in ("2", "3", "0", "*"):
        assert not passes.search(arms[code]), f"exit code {code} must not pass"
        assert "::error" in arms[code], code
    assert "GREEN" in arms["0"], "an unexpected green must say why it is a failure"


def test_no_job_can_hide_a_failure_behind_continue_on_error(workflow: dict[str, Any]) -> None:
    # Invariants 1 and 8 applied to CI. The nightly used to carry continue-on-error, failed at its
    # first real step on every run, and reported green; `demo-hr` carried it and so could have
    # hidden a regression against its baseline. No job is exempt: one that is expected to be red
    # handles its expected exit code explicitly instead.
    masked = [n for n, j in workflow["jobs"].items() if "continue-on-error" in j]
    assert not masked, f"{masked} carry continue-on-error"
    steps = [s.get("name") for s in _steps(workflow) if "continue-on-error" in s]
    assert not steps, f"steps {steps} carry continue-on-error"


def test_the_suite_runs_on_a_push_to_main_not_only_on_pull_requests(
    workflow: dict[str, Any],
) -> None:
    assert workflow["on"]["push"] == {"branches": ["main"]}
    for name in ("checks", "wheel", "eval"):
        assert "'push'" in workflow["jobs"][name]["if"], name


def test_the_nightly_cannot_run_on_a_push_and_unconfigured_it_says_so(
    workflow: dict[str, Any],
) -> None:
    jobs = workflow["jobs"]
    for name in ("nightly", "nightly-skipped"):
        condition = jobs[name]["if"]
        assert "'push'" not in condition and "'pull_request'" not in condition, name
        assert "schedule" in condition, name
    assert "vars.JUDGE_PROVIDER != ''" in jobs["nightly"]["if"]
    assert "vars.JUDGE_PROVIDER == ''" in jobs["nightly-skipped"]["if"]
    assert "SKIPPED" in jobs["nightly-skipped"]["name"]
    text = " ".join(s.get("run", "") for s in jobs["nightly-skipped"]["steps"])
    assert "JUDGE_PROVIDER is not set" in text and "Nothing was graded" in text


def test_the_nightly_fails_unless_it_graded(workflow: dict[str, Any]) -> None:
    run = next(s["run"] for s in workflow["jobs"]["nightly"]["steps"] if s.get("name") == "Evaluate")
    # Exit 1 (the simulator's known findings) is reported; every other non-zero is propagated.
    assert "PIPESTATUS[0]" in run and 'exit "$rc"' in run
    assert "-f reports/nightly.json" in run, "exit 1 only counts as graded if a report exists"


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
