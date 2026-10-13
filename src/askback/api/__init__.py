"""HTTP API. Imported lazily so the core never requires FastAPI.

    from askback.api import create_app
"""

from __future__ import annotations

from typing import Any


def create_app(config: Any = None) -> Any:
    """Build the FastAPI application (requires `pip install -e .[server]`)."""
    from .server import create_app as _create_app

    return _create_app(config)


__all__ = ["create_app"]
