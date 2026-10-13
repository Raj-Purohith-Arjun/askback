"""The LLM seam, as the simulator sees it.

The contract itself is framework-side, in `askback.providers.base`: the judge
needs it with no simulator installed. This module re-exports it so the
simulator's own code keeps one import for it.
"""

from __future__ import annotations

from askback.providers.base import LLMProvider, LLMResponse

__all__ = ["LLMProvider", "LLMResponse"]
