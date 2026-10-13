"""Adapters: the seam between the harness and the system under test.

The runner depends on `AgentAdapter` and nothing more specific, which is what
lets one suite file grade a simulator and a deployed agent without changing.
"""

from __future__ import annotations

from .base import AgentAdapter

__all__ = ["AgentAdapter"]
