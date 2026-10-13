"""Request templates and response mappings, shared by the adapters that speak JSON.

An agent's payload is its own. What differs between agents is how to ask (a request
template with `{message}` and `{session_id}`), and where the answer lives (dotted
response paths), so a payload change is a YAML edit and not a code change. This
module owns those two things and imports no transport, so an adapter over HTTP and
one over Socket.IO read an agent's reply the same way.

Honest reporting lives here too. A field the agent does not return stays empty, so
the evaluators that need it call the case unmeasurable, and a status that is absent
or not one we know becomes `ResponseStatus.UNKNOWN`, never `ANSWERED`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..core.contracts import Citation, ResponseStatus, RetrievedChunk, ToolCall
from ..core.errors import AgentCallError, ConfigError

#: Dotted paths into the response. The defaults are the key names of this repo's
#: own `ChatResponse.to_dict()`; `text` is the only mandatory one. Nothing here
#: works against a particular agent unconfigured - the simulator's own API takes
#: `message` and `conversation_id`, not the request template above, and its
#: identity in the body. tests/test_acceptance.py carries the config that does.
DEFAULT_RESPONSE_MAPPING: Mapping[str, str] = {
    "text": "reply",
    "route": "route",
    "status": "status",
    "intent": "intent.name",
    "citations": "citations",
    "retrieved": "retrieved",
    "tools": "tool_calls",
    "session": "conversation_id",
}

PLACEHOLDER = re.compile(r"\{(\w+)\}")
PLACEHOLDERS = frozenset({"message", "session_id"})
MISSING = object()


def validated_template(template: Mapping[str, Any]) -> Mapping[str, Any]:
    seen: set[str] = set()

    def walk(node: Any, where: str) -> None:
        if isinstance(node, str):
            for name in PLACEHOLDER.findall(node):
                if name not in PLACEHOLDERS:
                    raise ConfigError(
                        f"agent.request_template{where} uses {{{name}}}; only "
                        f"{sorted(PLACEHOLDERS)} are filled in"
                    )
                seen.add(name)
        elif isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{where}[{i}]")

    walk(template, "")
    if "message" not in seen:
        raise ConfigError(
            "agent.request_template never uses {message}: every case would send the "
            "same turn and the run would measure nothing"
        )
    return template


def fill(node: Any, values: Mapping[str, str]) -> Any:
    """Substitute placeholders, in one pass over the template only.

    Not `str.format`: the template is JSON-ish and full of braces. Not repeated
    replacement either: a message that itself contains "{session_id}" must reach
    the agent as typed.
    """
    if isinstance(node, str):
        return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), node)
    if isinstance(node, Mapping):
        return {key: fill(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [fill(value, values) for value in node]
    return node


def validated_headers(headers: Mapping[str, Any], where: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        # An unset ${TOKEN} expands to "". Refuse it here, by name, rather than
        # send an unauthenticated request and read the 401 as an agent failure.
        # The value is never echoed: it may be a credential.
        if not isinstance(name, str) or not isinstance(value, str) or not value:
            raise ConfigError(
                f"{where}.{name} must be a non-empty string (is its environment variable set?)"
            )
        out[name] = value
    return out


def validated_mapping(overrides: Mapping[str, str] | None) -> dict[str, str]:
    mapping = dict(DEFAULT_RESPONSE_MAPPING)
    for key, path in (overrides or {}).items():
        if key not in DEFAULT_RESPONSE_MAPPING:
            raise ConfigError(
                f"unknown agent.response_mapping key {key!r}; "
                f"valid keys: {', '.join(DEFAULT_RESPONSE_MAPPING)}"
            )
        if not isinstance(path, str) or not path:
            raise ConfigError(f"agent.response_mapping.{key} must be a dotted path, got {path!r}")
        mapping[key] = path
    return mapping


def dig(payload: Any, path: str) -> Any:
    """Follow a dotted path through dicts and lists (`choices.0.message.content`)."""
    node = payload
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return MISSING
    return node


def status_from(raw: Any) -> ResponseStatus:
    """Map the agent's status string, or say we cannot. Never a success default."""
    if isinstance(raw, str):
        try:
            return ResponseStatus(raw.strip().lower())
        except ValueError:
            pass
    return ResponseStatus.UNKNOWN


def citation(index: int, raw: Any) -> Citation:
    item = item_of(raw, "citations", index, required="doc_id")
    return Citation(
        doc_id=str(item["doc_id"]),
        chunk_id=str(item.get("chunk_id", "")),
        snippet=str(item.get("snippet", "")),
        title=str(item.get("title", "")),
        source=str(item.get("source", "")),
    )


def chunk(index: int, raw: Any) -> RetrievedChunk:
    item = item_of(raw, "retrieved", index, required="doc_id")
    if not isinstance(item.get("text"), str):
        raise AgentCallError(f"retrieved[{index}] has no text; its keys are {sorted(item)}")
    score = item.get("score", 0.0)
    # Carried through, not dropped: evaluators read it (the tenant a chunk
    # belongs to), and a check whose data went missing here passes vacuously.
    metadata = item.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise AgentCallError(
            f"retrieved[{index}].metadata is a {type(metadata).__name__}, not an object"
        )
    return RetrievedChunk(
        doc_id=str(item["doc_id"]),
        chunk_id=str(item.get("chunk_id", "")),
        text=item["text"],
        score=float(score) if isinstance(score, (int, float)) else 0.0,
        title=str(item.get("title", "")),
        source=str(item.get("source", "")),
        metadata=dict(metadata or {}),
    )


def tool(index: int, raw: Any) -> ToolCall:
    if isinstance(raw, str):
        return ToolCall(name=raw)
    item = item_of(raw, "tools", index, required="name")
    arguments = item.get("arguments", {})
    return ToolCall(
        name=str(item["name"]),
        arguments=arguments if isinstance(arguments, dict) else {},
        output=item.get("output"),
        ok=bool(item.get("ok", True)),
        error=str(item["error"]) if item.get("error") else None,
    )


def item_of(raw: Any, field: str, index: int, *, required: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or required not in raw:
        got = f"keys {sorted(raw)}" if isinstance(raw, dict) else type(raw).__name__
        raise AgentCallError(f"{field}[{index}] needs a {required!r}; got {got}")
    return raw


__all__ = [
    "DEFAULT_RESPONSE_MAPPING",
    "MISSING",
    "PLACEHOLDERS",
    "chunk",
    "citation",
    "dig",
    "fill",
    "status_from",
    "tool",
    "validated_headers",
    "validated_mapping",
    "validated_template",
]
