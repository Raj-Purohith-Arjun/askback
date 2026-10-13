"""The HTTP adapter: a deployed agent, reached over the wire.

This is the adapter that makes the harness a tool rather than a demo. The agent's
payload is its own; this module owns the three things that differ between
agents - how to ask (a request template), who to ask as (per-principal headers,
body fields, or both), and where the answer lives (dotted response paths) - so a
payload change is a YAML edit, not a code change.

Needs httpx, from the `http` extra. Nothing imports this module until
`agent.adapter: http` asks for it, so the core stays importable without it.

Retries, and what they cost. A turn is retried on transport errors and on
408/409/425/429/500/502/503/504, up to `max_attempts` tries in all. Any other
4xx is a contract break - a 422 means our request is wrong, and sending it again
only burns wall-clock and hides the cause - so it fails at once. A retried POST
may deliver a turn TWICE: when a response is lost after the agent acted, the
agent sees the same message again and may record it twice in its history. For an
agent that is not idempotent per `session_id`, set `max_attempts: 1` and accept
transient failures as failed cases.

Honest reporting. A field the agent does not return stays empty - no citations,
no retrieved chunks, no tool calls - so the evaluators that need it call the case
unmeasurable. A status that is absent or not one we know becomes
`ResponseStatus.UNKNOWN`, never `ANSWERED`: defaulting to success is how a
broken integration goes green.
"""

from __future__ import annotations

import copy
import email.utils
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..core.contracts import (
    ChatResponse,
    Intent,
)
from ..core.errors import AgentCallError, ConfigError
from ..core.logging import get_logger
from .base import AdapterLifecycle
from .payload import DEFAULT_RESPONSE_MAPPING
from .payload import MISSING as _MISSING
from .payload import chunk as _chunk
from .payload import citation as _citation
from .payload import dig as _dig
from .payload import fill as _fill
from .payload import status_from as _status
from .payload import tool as _tool
from .payload import validated_headers as _validated_headers
from .payload import validated_mapping as _validated_mapping
from .payload import validated_template as _validated_template

log = get_logger(__name__)

#: Retried alongside any `httpx.TransportError`. Everything else non-2xx fails.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Longest a `Retry-After` is honoured for. An agent asking for an hour would
#: otherwise park a worker, and with it the run, for an hour.
MAX_RETRY_AFTER_S = 60.0

#: Longest the computed (not server-requested) backoff grows to.
MAX_BACKOFF_S = 30.0

DEFAULT_REQUEST_TEMPLATE: Mapping[str, Any] = {
    "query": "{message}",
    "session_id": "{session_id}",
}



class HTTPAdapter(AdapterLifecycle):
    """`AgentAdapter` over one HTTP endpoint that takes a turn and returns JSON."""

    def __init__(
        self,
        url: str,
        *,
        principal_headers: Mapping[str, Mapping[str, str]],
        principal_fields: Mapping[str, Mapping[str, Any]] | None = None,
        request_template: Mapping[str, Any] | None = None,
        response_mapping: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
        max_attempts: int = 3,
        backoff_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError(f"agent.url must be an http(s) URL, got {url!r}")
        if timeout_s <= 0:
            raise ConfigError(f"agent.timeout_s must be positive, got {timeout_s!r}")
        if max_attempts < 1:
            raise ConfigError(f"agent.max_attempts must be at least 1, got {max_attempts!r}")
        if backoff_s < 0:
            raise ConfigError(f"agent.backoff_s cannot be negative, got {backoff_s!r}")

        self._url = url
        # Credentials and query strings never reach a report: it is an artifact.
        self._endpoint = f"{parts.scheme}://{parts.hostname}{f':{parts.port}' if parts.port else ''}{parts.path}"
        self._template = _validated_template(request_template or DEFAULT_REQUEST_TEMPLATE)
        self._mapping = _validated_mapping(response_mapping)
        self._headers = _validated_headers(headers or {}, "agent.headers")
        # Copied, validated, and deep enough that a caller mutating its own
        # dicts mid-run cannot change who a later turn is sent as.
        self._principals = {
            label: _validated_headers(value, f"agent.principal_headers.{label}")
            for label, value in principal_headers.items()
        }
        self._fields = _validated_fields(principal_fields, self._principals, self._template)
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s
        self._sleep = sleep
        # httpx's own seam for where requests go: an ASGI app, a proxy, a mock.
        self._transport = transport
        self._client: httpx.Client | None = None

    @classmethod
    def from_config(cls, agent: Mapping[str, Any]) -> HTTPAdapter:
        """Build from the `agent:` section of your config file."""
        known = {
            "adapter", "url", "timeout_s", "max_attempts", "backoff_s", "headers",
            "request_template", "response_mapping", "principal_headers", "principal_fields",
            # Read by the factory, which registers it before this runs; named here
            # only so the stray-key check does not refuse the one key an HTTP run
            # needs to make `tool_side_effects` measurable.
            "side_effecting_tools",
        }  # fmt: skip
        stray = sorted(set(agent) - known)
        if stray:
            raise ConfigError(
                f"unknown agent key(s) for the http adapter: {stray}; known: {sorted(known)}"
            )
        url = agent.get("url")
        if not isinstance(url, str) or not url:
            raise ConfigError("agent.url is required for the http adapter")
        principals = agent.get("principal_headers")
        if not isinstance(principals, Mapping):
            raise ConfigError(
                "agent.principal_headers is required for the http adapter: a mapping of "
                "principal label -> {Header: value}. Use `default: {}` for an agent with "
                "no authentication; a missing label never falls back to anonymous."
            )
        for label, value in principals.items():
            if not isinstance(value, Mapping):
                raise ConfigError(
                    f"agent.principal_headers.{label} must be a mapping, got {value!r}"
                )
        return cls(
            url,
            principal_headers=principals,
            principal_fields=_optional_mapping(agent, "principal_fields"),
            request_template=_optional_mapping(agent, "request_template"),
            response_mapping=_optional_mapping(agent, "response_mapping"),
            headers=_optional_mapping(agent, "headers"),
            timeout_s=_number(agent, "timeout_s", 30.0),
            max_attempts=int(_number(agent, "max_attempts", 3)),
            backoff_s=_number(agent, "backoff_s", 0.5),
        )

    # ------------------------------------------------------------------ #
    # Lifecycle: one pooled client for the whole run, never one per call.
    # ------------------------------------------------------------------ #

    def _open(self) -> None:
        self._client = httpx.Client(
            timeout=self._timeout_s, headers=self._headers, transport=self._transport
        )

    def _close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    # ------------------------------------------------------------------ #
    # AgentAdapter
    # ------------------------------------------------------------------ #

    def send(
        self, message: str, *, session_id: str, principal: str = "default"
    ) -> ChatResponse:
        """Deliver one turn, retrying only what is worth retrying, and map the reply.

        No lock: `httpx.Client` is safe for concurrent use, and nothing else
        here is shared and mutable.
        """
        self._require_open("send")
        client = self._client
        if client is None:  # unreachable while the depth guard holds; keeps mypy honest
            raise RuntimeError("HTTPAdapter has no open client")
        headers = self._principal_headers(principal)
        body = _fill(self._template, {"message": message, "session_id": session_id})
        # Disjoint from the template's keys by construction, so this only adds.
        body.update(copy.deepcopy(self._fields.get(principal, {})))

        response, latency_ms = self._post_with_retry(client, body, headers, session_id)
        return self._map(_json_object(response), session_id, latency_ms)

    def describe(self) -> dict[str, str]:
        """Provenance for the run report.

        The endpoint without credentials or query string. Header values are never
        recorded: they are tokens, and a report is an artifact.
        """
        self._require_open("describe")
        return {"adapter": "http", "endpoint": self._endpoint}

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    def _principal_headers(self, principal: str) -> dict[str, str]:
        """Resolve a label to headers, or refuse. Never falls back to anonymous.

        An isolation case that quietly ran with no credentials, or someone
        else's, would pass for the wrong reason.
        """
        try:
            return self._principals[principal]
        except KeyError:
            raise ConfigError(
                f"unknown principal {principal!r}; agent.principal_headers knows: "
                f"{sorted(self._principals)}"
            ) from None

    def _post_with_retry(
        self,
        client: httpx.Client,
        body: Any,
        headers: dict[str, str],
        session_id: str,
    ) -> tuple[httpx.Response, float]:
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            started = time.perf_counter()
            try:
                response = client.post(self._url, json=body, headers=headers)
            except httpx.TransportError as exc:
                if last:
                    raise AgentCallError(
                        f"{self._endpoint} unreachable after {attempt} attempt(s) "
                        f"for session {session_id!r}: {type(exc).__name__}: {exc}"
                    ) from exc
                self._wait(attempt, None, f"{type(exc).__name__}")
                continue
            latency_ms = (time.perf_counter() - started) * 1000

            if response.is_success:
                return response, latency_ms
            if response.status_code not in RETRYABLE_STATUSES:
                raise AgentCallError(
                    f"{self._endpoint} answered HTTP {response.status_code}, which is not "
                    f"retried: {_snippet(response)}"
                )
            if last:
                raise AgentCallError(
                    f"{self._endpoint} still answering HTTP {response.status_code} after "
                    f"{attempt} attempt(s): {_snippet(response)}"
                )
            self._wait(
                attempt,
                _retry_after(response.headers.get("Retry-After")),
                f"HTTP {response.status_code}",
            )
        raise AssertionError("unreachable: the last attempt returns or raises")  # pragma: no cover

    def _wait(self, attempt: int, retry_after: float | None, why: str) -> None:
        if retry_after is not None:
            delay = min(retry_after, MAX_RETRY_AFTER_S)
        else:
            # Jittered so workers that failed together do not retry together.
            delay = min(self._backoff_s * 2 ** (attempt - 1), MAX_BACKOFF_S) * random.uniform(
                0.5, 1.0
            )
        log.warning(
            "%s: %s on attempt %d/%d; retrying in %.2fs",
            self._endpoint, why, attempt, self._max_attempts, delay,
        )  # fmt: skip
        self._sleep(delay)

    # ------------------------------------------------------------------ #
    # Mapping
    # ------------------------------------------------------------------ #

    def _map(self, payload: dict[str, Any], session_id: str, latency_ms: float) -> ChatResponse:
        m = self._mapping

        text = _dig(payload, m["text"])
        if text is _MISSING or not isinstance(text, str):
            problem = "is missing" if text is _MISSING else f"is a {type(text).__name__}, not text"
            raise AgentCallError(
                f"response_mapping.text path {m['text']!r} {problem}; the payload's "
                f"top-level keys are {sorted(payload)}"
            )

        route = _dig(payload, m["route"])
        intent = _dig(payload, m["intent"])
        session = _dig(payload, m["session"])
        return ChatResponse(
            reply=text,
            conversation_id=session if isinstance(session, str) and session else session_id,
            route=route if isinstance(route, str) and route else "unknown",
            status=_status(_dig(payload, m["status"])),
            intent=Intent(name=intent if isinstance(intent, str) and intent else "unknown"),
            citations=[_citation(i, c) for i, c in enumerate(self._list(payload, "citations"))],
            retrieved=[_chunk(i, c) for i, c in enumerate(self._list(payload, "retrieved"))],
            tool_calls=[_tool(i, t) for i, t in enumerate(self._list(payload, "tools"))],
            latency_ms=round(latency_ms, 3),
        )

    def _list(self, payload: dict[str, Any], key: str) -> list[Any]:
        """An optional list field: absent is empty, present-but-wrong is a contract break."""
        value = _dig(payload, self._mapping[key])
        if value is _MISSING or value is None:
            return []
        if not isinstance(value, list):
            raise AgentCallError(
                f"response_mapping.{key} path {self._mapping[key]!r} is a "
                f"{type(value).__name__}, not a list"
            )
        return value


# --------------------------------------------------------------------------- #
# Request building
# --------------------------------------------------------------------------- #


def _validated_fields(
    fields: Mapping[str, Mapping[str, Any]] | None,
    principals: Mapping[str, Any],
    template: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Per-principal body fields, for agents that take identity in the payload.

    Merged into the top level of the request body. When given at all, the labels
    must be exactly those of `principal_headers`: a label with headers but no
    fields would be sent with no identity in the body, and pass an isolation
    case for the wrong reason. Use `{}` for a principal that needs none.
    """
    if fields is None:
        return {}
    if set(fields) != set(principals):
        raise ConfigError(
            "agent.principal_fields and agent.principal_headers must declare the same "
            f"principals; fields has {sorted(fields)}, headers has {sorted(principals)}"
        )
    out: dict[str, dict[str, Any]] = {}
    for label, values in fields.items():
        where = f"agent.principal_fields.{label}"
        if not isinstance(values, Mapping):
            raise ConfigError(f"{where} must be a mapping, got {type(values).__name__}")
        for name, value in values.items():
            # Same rule as headers: an unset ${VAR} expands to "", and the value
            # is never echoed.
            if not isinstance(name, str) or not name or value is None or value == "":
                raise ConfigError(
                    f"{where}.{name} must be a non-empty value (is its environment variable set?)"
                )
            # Overwriting a template key could replace the message itself.
            if name in template:
                raise ConfigError(
                    f"{where}.{name} collides with a key of agent.request_template; "
                    "an identity field cannot overwrite the request"
                )
        out[label] = copy.deepcopy(dict(values))
    return out


def _optional_mapping(agent: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = agent.get(key)
    if value is not None and not isinstance(value, Mapping):
        raise ConfigError(f"agent.{key} must be a mapping, got {value!r}")
    return value


def _number(agent: Mapping[str, Any], key: str, default: float) -> float:
    value = agent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"agent.{key} must be a number, got {value!r}")
    return float(value)


# --------------------------------------------------------------------------- #
# Response reading
# --------------------------------------------------------------------------- #


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AgentCallError(
            f"response was not JSON (HTTP {response.status_code}, "
            f"content-type {response.headers.get('content-type', 'none')!r}): {_snippet(response)}"
        ) from exc
    if not isinstance(payload, dict):
        raise AgentCallError(
            f"response JSON must be an object, got {type(payload).__name__}"
        )
    return payload


def _retry_after(value: str | None) -> float | None:
    """Seconds from a `Retry-After` header (a number, or an HTTP date), else None."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _snippet(response: httpx.Response) -> str:
    """A short, single-line look at a body, for an error message."""
    text = " ".join(response.text.split())
    return text[:200] + ("..." if len(text) > 200 else "") or "(empty body)"


__all__ = [
    "DEFAULT_REQUEST_TEMPLATE",
    "DEFAULT_RESPONSE_MAPPING",
    "RETRYABLE_STATUSES",
    "HTTPAdapter",
]
