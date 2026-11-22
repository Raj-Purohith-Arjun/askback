"""Every workflow in `.github/workflows`, and the automation config beside it, held to one standard.

`test_workflow.py` and `test_release_workflow.py` each read one named file, so a workflow added
later shipped with no test that its actions were pinned. These tests *glob* the directory
instead: a new file is covered the moment it exists, and there is no exemption list to add it to.
The one guard on the glob itself is that it finds the files we know are there, so a scan that
silently matches nothing cannot pass.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
KNOWN = {"evaluate.yml", "release.yml", "codeql.yml", "scorecard.yml"}


def _load(path: Path) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in loaded:  # PyYAML reads the bare key `on` as the boolean True (YAML 1.1)
        loaded["on"] = loaded.pop(True)
    return loaded


def _named(name: str) -> dict[Any, Any]:
    return _load(ROOT / ".github" / "workflows" / name)


def test_the_scan_finds_every_workflow_we_know_about() -> None:
    assert {p.name for p in WORKFLOWS} >= KNOWN, "the glob stopped finding a workflow"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_in_every_workflow_is_pinned_to_a_sha_with_its_release_noted(
    path: Path,
) -> None:
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", path.read_text("utf-8"), re.MULTILINE)
    for ref, rest in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"{path.name}: {ref} is not pinned"
        assert re.search(r"#\s*v\d+\.\d+\.\d+", rest), f"{path.name}: {ref} has no release comment"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_uses_pull_request_target_or_leaves_permissions_implicit(path: Path) -> None:
    workflow = _load(path)
    assert "pull_request_target" not in workflow["on"], path.name
    # An undeclared `permissions` inherits the repository default, which can include write.
    assert "permissions" in workflow or all(
        "permissions" in job for job in workflow["jobs"].values()
    ), f"{path.name} leaves some job's permissions implicit"


def test_codeql_scans_python_only_on_pull_requests_and_a_schedule() -> None:
    workflow = _named("codeql.yml")
    # workflow_dispatch lets the first run (and any re-run) skip the weekly cron.
    assert set(workflow["on"]) == {"pull_request", "schedule", "workflow_dispatch"}
    job = workflow["jobs"]["analyze"]
    assert job["permissions"] == {"security-events": "write", "contents": "read"}
    init = next(s for s in job["steps"] if s.get("uses", "").startswith("github/codeql-action/init"))
    assert init["with"]["languages"] == "python"


def test_scorecard_is_scheduled_publishes_and_asks_for_three_permissions() -> None:
    workflow = _named("scorecard.yml")
    assert "schedule" in workflow["on"]
    job = workflow["jobs"]["analysis"]
    assert job["permissions"] == {
        "id-token": "write",
        "security-events": "write",
        "contents": "read",
    }
    scorecard = next(s for s in job["steps"] if "scorecard-action" in s.get("uses", ""))
    assert scorecard["with"]["publish_results"] is True
    # The Scorecard service verifies the workflow's shape before it will publish; no write at the
    # top level is part of that.
    assert workflow["permissions"] in ("read-all", {}, {"contents": "read"})


def test_dependabot_covers_actions_and_pip_weekly_and_grouped() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text("utf-8"))
    assert config["version"] == 2
    updates = {u["package-ecosystem"]: u for u in config["updates"]}
    assert set(updates) == {"github-actions", "pip"}
    for name, update in updates.items():
        assert update["schedule"]["interval"] == "weekly", name
        assert update["groups"], f"{name} is not grouped"


def test_the_release_tools_are_pinned_in_constraints_and_installed_through_them() -> None:
    pinned = {
        line.split("==")[0].lower()
        for line in (ROOT / "constraints.txt").read_text("utf-8").splitlines()
        if "==" in line and not line.startswith("#")
    }
    assert {"build", "twine"} <= pinned
    installs = [
        s["run"]
        for s in _named("release.yml")["jobs"]["build"]["steps"]
        if "pip install" in s.get("run", "")
    ]
    assert installs and all("-c constraints.txt" in i for i in installs)


def test_coverage_is_a_ci_only_line_coverage_floor_on_the_3_14_leg_and_not_in_addopts() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    assert "--cov" not in pyproject["tool"]["pytest"]["ini_options"]["addopts"], "keeps local pytest fast"
    floor = pyproject["tool"]["coverage"]["report"]["fail_under"]
    assert isinstance(floor, int) and floor > 0
    assert any(d.startswith("pytest-cov") for d in pyproject["project"]["optional-dependencies"]["dev"])
    steps = _named("evaluate.yml")["jobs"]["checks"]["steps"]
    covered = [s for s in steps if "--cov" in s.get("run", "")]
    assert len(covered) == 1, "coverage runs in exactly one step"
    assert covered[0]["if"] == "matrix.python == '3.14'" and "--cov=askback" in covered[0]["run"]
    plain = [s for s in steps if s.get("run", "").startswith("pytest") and "--cov" not in s["run"]]
    assert plain and all(s["if"] == "matrix.python != '3.14'" for s in plain), "no leg runs tests twice"


def test_coverage_tools_are_pinned_and_the_readme_badge_states_the_floor() -> None:
    pinned = {
        line.split("==")[0].lower()
        for line in (ROOT / "constraints.txt").read_text("utf-8").splitlines()
        if "==" in line and not line.startswith("#")
    }
    assert {"coverage", "pytest-cov"} <= pinned, "the measuring tools move the number"
    floor = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["tool"]["coverage"]["report"][
        "fail_under"
    ]
    badge = re.search(r"line%20coverage%20floor-(\d+)%25", (ROOT / "README.md").read_text("utf-8"))
    assert badge, "the README has no line-coverage badge"
    assert int(badge.group(1)) == floor, "the badge and fail_under have drifted apart"
