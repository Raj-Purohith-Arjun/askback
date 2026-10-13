"""Framework core: contracts, registry, config, logging, text helpers.

Everything here is stdlib only and stable. Build on it; avoid editing it.
"""

from .config import AppConfig, load_mapping, resolve_path
from .contracts import (
    AgentReply,
    ChatRequest,
    ChatResponse,
    Citation,
    Intent,
    Message,
    ResponseStatus,
    RetrievedChunk,
    Role,
    SafetyReport,
    Severity,
    ToolCall,
)
from .errors import (
    AskbackError,
    ConfigError,
    DatasetError,
    ProviderError,
    RegistryError,
    ToolError,
)
from .logging import get_logger, setup_logging
from .registry import EVALUATORS, Registry

__all__ = [
    "EVALUATORS",
    "AgentReply",
    "AppConfig",
    "ChatRequest",
    "ChatResponse",
    "AskbackError",
    "Citation",
    "ConfigError",
    "DatasetError",
    "Intent",
    "Message",
    "ProviderError",
    "Registry",
    "RegistryError",
    "ResponseStatus",
    "RetrievedChunk",
    "Role",
    "SafetyReport",
    "Severity",
    "ToolCall",
    "ToolError",
    "get_logger",
    "load_mapping",
    "resolve_path",
    "setup_logging",
]
