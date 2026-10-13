"""Choosing the adapter from config.

`agent.adapter` in your config file names the system under test, so pointing a run
at something else is a config edit, not a code change. Callers get an
`AgentAdapter` back and never learn which one it is.

This module never imports the simulator, not even lazily. `in_process` needs an
engine, and the caller - the CLI or the API, which are allowed to know the
simulator exists - builds it and passes it in. A factory that reached for the
simulator itself would be the one exception the package boundary could not
survive having.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.config import AppConfig
from ..core.errors import ConfigError
from ..evaluation.safety_eval import declare_side_effecting_tools
from .base import AgentAdapter
from .in_process import DEFAULT_PRINCIPALS, InProcessAdapter, InProcessEngine

#: What a config file with no `agent:` section runs against. Configs written before
#: the adapter seam existed have none, and they always meant the simulator. A run with no
#: config file at all never gets here: it is the built-in demo (see `askback.demo`).
DEFAULT_ADAPTER = "in_process"

VALID_ADAPTERS = ("in_process", "http", "socketio", "demo")


def adapter_name(settings: AppConfig) -> str:
    """Which adapter `agent.adapter` names, so a caller knows whether to build an engine."""
    return str(_agent_section(settings).get("adapter", DEFAULT_ADAPTER))


def build_adapter(
    settings: AppConfig, *, engine: InProcessEngine | None = None
) -> AgentAdapter:
    """Build the adapter named by `agent.adapter`.

    `engine` is the in-process system under test, and is required when the
    adapter is `in_process`: this factory does not build one, because that
    would mean importing the simulator. It is ignored by every other adapter.

    `agent.side_effecting_tools`, when present, is registered here as a
    declaration source for the tool-misuse check. Here rather than in each
    caller because this is the one place every run names its system under
    test, and that system is the only thing that can know its tools.
    """
    agent = _agent_section(settings)

    tools = _side_effecting_tools(agent)
    if tools is not None:
        declare_side_effecting_tools(lambda: tools)

    name = agent.get("adapter", DEFAULT_ADAPTER)
    if name == "in_process":
        if engine is None:
            raise ConfigError(
                "agent.adapter 'in_process' drives an engine in this process, and none "
                "was passed to build_adapter(engine=...). The example simulator provides one "
                "from a source checkout: see examples/hr."
            )
        return InProcessAdapter(engine, _principals(agent))

    if name == "http":
        try:
            from .http import HTTPAdapter
        except ImportError as exc:
            raise ConfigError(
                "agent.adapter 'http' needs httpx. Run: pip install -e .[http]"
            ) from exc

        return HTTPAdapter.from_config(agent)

    if name == "demo":
        from ..demo import DemoAdapter

        return DemoAdapter.from_config(agent)

    if name == "socketio":
        # No ImportError guard here: that module imports python-socketio when it
        # builds, and turns its absence into a ConfigError carrying the install hint.
        from .socketio_adapter import SocketIOAdapter

        return SocketIOAdapter.from_config(agent)

    raise ConfigError(
        f"unknown agent.adapter {name!r}; valid options: {', '.join(VALID_ADAPTERS)}"
    )


def resolve_principals(settings: AppConfig) -> dict[str, tuple[str, str]]:
    """The principal mapping a run will use, for checking datasets before it starts.

    The configured one, or the adapter's defaults when `agent.principals` is
    absent. Only the in-process adapter resolves identities to tenants; any
    other adapter yields an empty mapping, which makes the loader check a no-op.
    """
    agent = _agent_section(settings)
    if agent.get("adapter", DEFAULT_ADAPTER) != "in_process":
        # An HTTP identity is a header, not a (user_id, tenant_id) pair, so
        # there is no tenant to check a dataset's declaration against.
        return {}
    configured = _principals(agent)
    if configured is not None:
        return configured
    return dict(DEFAULT_PRINCIPALS)


def _agent_section(settings: AppConfig) -> Mapping[str, Any]:
    agent = settings.raw.get("agent", {})
    if not isinstance(agent, Mapping):
        raise ConfigError(f"config `agent` must be a mapping, got {agent!r}")
    return agent


def _principals(agent: Mapping[str, Any]) -> dict[str, tuple[str, str]] | None:
    """Read `agent.principals` into the (user_id, tenant_id) pairs an adapter takes.

    None when the key is absent, which leaves the adapter on its own defaults.
    Anything present is checked here so a malformed entry is reported with its
    config path, before a case is sent as it.
    """
    declared = agent.get("principals")
    if declared is None:
        return None
    if not isinstance(declared, Mapping):
        raise ConfigError(
            "agent.principals must map a principal label to "
            f"{{user_id, tenant_id}}, got {declared!r}"
        )

    principals: dict[str, tuple[str, str]] = {}
    for label, identity in declared.items():
        if not (
            isinstance(label, str)
            and isinstance(identity, Mapping)
            and set(identity) == {"user_id", "tenant_id"}
            and all(isinstance(part, str) for part in identity.values())
        ):
            raise ConfigError(
                f"agent.principals.{label} must be a mapping with exactly a string "
                f"user_id and a string tenant_id, got {identity!r}"
            )
        principals[label] = (identity["user_id"], identity["tenant_id"])
    return principals


def _side_effecting_tools(agent: Mapping[str, Any]) -> frozenset[str] | None:
    """Read `agent.side_effecting_tools`: the tools this agent says act on the world.

    None when the key is absent, and absent is not the same as empty: `[]`
    states that no tool has side effects, and the check can then pass. Absent
    states nothing, so the check keeps failing until someone does (invariant 8).
    A bare string is refused; iterating it would declare its letters.
    """
    declared = agent.get("side_effecting_tools")
    if declared is None:
        return None
    if not isinstance(declared, list) or not all(isinstance(name, str) for name in declared):
        raise ConfigError(
            f"agent.side_effecting_tools must be a list of tool names, got {declared!r}"
        )
    return frozenset(declared)


__all__ = [
    "DEFAULT_ADAPTER",
    "VALID_ADAPTERS",
    "adapter_name",
    "build_adapter",
    "resolve_principals",
]
