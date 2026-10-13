"""Allows `python -m askback ...` as well as the installed `askback` command."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
