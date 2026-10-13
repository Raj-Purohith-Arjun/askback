"""Build the shareable folder and zip from what git tracks, and nothing else.

    python scripts/package.py

Produces dist/askback/ and dist/askback-<version>.zip. The file list comes from
`git ls-files`, not from walking the directory, so it cannot ship what is only on this
machine: an editor or assistant settings file, an untracked working note, a build directory,
a cache, `.env`, generated reports. A file that is not committed (or at least staged) is not
in the package. An earlier version walked the filesystem and would have shipped
`.claude/settings.local.json` and a whole `*.egg-info` directory.

Run it from a clean checkout of the revision you mean to share. It refuses to run outside a git
checkout, and says which tracked files have uncommitted changes (they are packaged as they are on
disk, so the zip would not match the commit).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
NAME = "askback"


def tracked_files() -> list[Path]:
    """Every file git tracks, as paths under ROOT. Raises if this is not a git checkout."""
    try:
        listing = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            "scripts/package.py packages what git tracks and could not run `git ls-files` "
            f"here ({exc}). Run it from a git checkout."
        ) from exc
    names = [n for n in listing.decode("utf-8").split("\0") if n]
    return [ROOT / n for n in names if (ROOT / n).is_file()]


def uncommitted() -> list[str]:
    """Tracked files that differ from the commit, so the zip would not match it."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line[3:] for line in out.splitlines()]


def version() -> str:
    """Read the version from the package, the one place it is written."""
    for line in (ROOT / "src" / "askback" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def main() -> int:
    files = tracked_files()
    dirty = uncommitted()
    if dirty:
        print(f"warning: {len(dirty)} tracked file(s) have uncommitted changes:", file=sys.stderr)
        for name in dirty[:10]:
            print(f"  {name}", file=sys.stderr)

    staging = DIST / NAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for source in files:
        target = staging / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    archive = DIST / f"{NAME}-{version()}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                # Everything nests under one folder, so unzipping never scatters
                # files across the user's Downloads directory.
                zf.write(path, Path(NAME) / path.relative_to(staging))

    size_mb = archive.stat().st_size / 1_048_576
    print(f"folder : {staging}")
    print(f"zip    : {archive}  ({size_mb:.2f} MB, {len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
