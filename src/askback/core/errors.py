"""Exception hierarchy. Catch `AskbackError` to catch anything this package raises."""

from __future__ import annotations


class AskbackError(Exception):
    """Base class for every error raised by the framework."""


class ConfigError(AskbackError):
    """Configuration file is missing, malformed, or references unknown plugins."""


class RegistryError(AskbackError):
    """A component was requested by name but is not registered."""


class ProviderError(AskbackError):
    """An LLM provider failed (network, auth, quota, bad response)."""


class ToolError(AskbackError):
    """A tool failed while executing. Surfaced on ToolCall.error, not fatal."""


class AgentCallError(AskbackError):
    """A turn could not be delivered to the agent, or its reply could not be read.

    Raised by adapters. The runner records it as a failed case, so say what
    broke and where, but never include a payload value: replies may be personal data.
    """


class DatasetError(AskbackError):
    """A test dataset could not be read or is missing required fields."""
