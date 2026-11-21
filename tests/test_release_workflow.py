"""The release workflow, held to its registration: PyPI trusts a repository, a workflow file name
and an environment name, and trusts nothing else.

Publishing cannot be run from here, so what is checked is the file: no token, the OIDC permission
only where it is needed, the environment PyPI was told about, every action pinned, and the sdist
checked as well as the wheel.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    loaded["on"] = loaded.pop(True)  # PyYAML reads the bare key `on` as True (YAML 1.1)
    return loaded


def test_every_action_is_pinned_to_a_full_commit_sha_with_its_release_noted() -> None:
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", WORKFLOW.read_text("utf-8"), re.MULTILINE)
    assert uses, "the scan found no actions at all"
    for ref, rest in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned to a SHA"
        assert re.search(r"#\s*v\d+\.\d+\.\d+", rest), f"{ref} has no release comment"


def test_no_credential_is_read_anywhere() -> None:
    text = WORKFLOW.read_text("utf-8")
    assert "secrets." not in text and "password" not in text.lower()
    assert "PYPI_API_TOKEN" not in text


def test_the_oidc_permission_is_granted_to_the_publish_jobs_and_only_those(
    workflow: dict[str, Any],
) -> None:
    assert workflow["permissions"] == {}, "nothing may be granted at the top level"
    jobs = workflow["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    for name in ("pypi", "testpypi"):
        assert jobs[name]["permissions"] == {"id-token": "write"}, name


def test_the_real_publish_matches_what_was_registered_on_pypi(workflow: dict[str, Any]) -> None:
    assert WORKFLOW.name == "release.yml"
    job = workflow["jobs"]["pypi"]
    assert job["environment"]["name"] == "pypi"
    assert job["if"] == "github.event_name == 'release'"
    assert workflow["on"]["release"] == {"types": ["published"]}
    publish = [s for s in job["steps"] if s.get("uses", "").startswith("pypa/gh-action-pypi-publish")]
    assert len(publish) == 1 and publish[0]["with"]["attestations"] is True
    assert "repository-url" not in publish[0]["with"], "the real publish must target PyPI"


def test_a_manual_run_cannot_reach_real_pypi(workflow: dict[str, Any]) -> None:
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["jobs"]["pypi"]["if"].startswith("github.event_name == 'release'")
    test_job = workflow["jobs"]["testpypi"]
    assert "workflow_dispatch" in test_job["if"] and "publish_testpypi" in test_job["if"]
    uploads = [s for s in test_job["steps"] if "pypi-publish" in s.get("uses", "")]
    assert uploads[0]["with"]["repository-url"] == "https://test.pypi.org/legacy/"


def test_the_publish_jobs_upload_what_the_build_job_checked(workflow: dict[str, Any]) -> None:
    for name in ("pypi", "testpypi"):
        assert workflow["jobs"][name]["needs"] == "build", name
        assert any("download-artifact" in s.get("uses", "") for s in workflow["jobs"][name]["steps"])


def test_the_sdist_is_checked_as_well_as_the_wheel(workflow: dict[str, Any]) -> None:
    commands = " ".join(s.get("run", "") for s in workflow["jobs"]["build"]["steps"])
    assert "tarfile" in commands and "askback_sim" in commands and "demo.yaml" in commands
    assert "wheel sdist" in commands, "both distributions must be installed and run"
    assert "twine check --strict" in commands
