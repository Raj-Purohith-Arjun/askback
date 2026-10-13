"""`scripts/package.py` ships what git tracks and nothing that merely sits on this machine.

Its predecessor walked the directory. It would have zipped `.claude/settings.local.json`, an
untracked working note, and a whole `*.egg-info` directory, none of which any `.gitignore`
protects it from. These tests build a throwaway git repository with exactly those files lying
around, run the real script in it, and read the archive.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "package.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _package(tmp_path: Path) -> tuple[list[str], str]:
    assert shutil.which("git"), "this test needs git, as the script does"
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src" / "askback").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "package.py")
    (repo / "src" / "askback" / "__init__.py").write_text('__version__ = "9.9.9"\n', "utf-8")
    (repo / "README.md").write_text("tracked\n", "utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "scripts", "src", "README.md")

    # Everything below is on disk, untracked, and must not ship.
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text("{}", "utf-8")
    (repo / "NOTES-internal.md").write_text("an untracked working note\n", "utf-8")
    (repo / "src" / "thing.egg-info").mkdir()
    (repo / "src" / "thing.egg-info" / "PKG-INFO").write_text("x", "utf-8")
    (repo / ".env").write_text("SECRET=1\n", "utf-8")
    (repo / "reports").mkdir()
    (repo / "reports" / "run.json").write_text("{}", "utf-8")

    done = subprocess.run(
        [sys.executable, str(repo / "scripts" / "package.py")],
        capture_output=True, text=True, check=True, cwd=repo,
    )  # fmt: skip
    archive = repo / "dist" / "askback-9.9.9.zip"
    return sorted(zipfile.ZipFile(archive).namelist()), done.stdout


def test_only_tracked_files_are_in_the_archive_and_the_version_comes_from_the_package(
    tmp_path: Path,
) -> None:
    names, out = _package(tmp_path)

    assert names == [
        "askback/README.md",
        "askback/scripts/package.py",
        "askback/src/askback/__init__.py",
    ]
    assert "3 files" in out


def test_it_refuses_to_run_outside_a_git_checkout(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "askback").mkdir(parents=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / "package.py")
    (tmp_path / "src" / "askback" / "__init__.py").write_text('__version__ = "1"\n', "utf-8")

    done = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "package.py")],
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )  # fmt: skip
    assert done.returncode != 0 and "git" in done.stderr
