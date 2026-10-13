"""The Socket.IO adapter: an agent that answers over a long-lived event connection.

Socket.IO is not raw WebSocket. It has its own HTTP handshake and upgrade, its own
packet framing, namespaces and heartbeats, so a plain websocket client fails before
any application frame exists. This module uses `python-socketio`, which speaks the
protocol, and adds only what a test harness needs on top of it: which events are
which, when a reply is complete, who we are, and how fast we may go.

Needs `python-socketio[client]`, from the `socketio` extra. Nothing imports this
module until `agent.adapter: socketio` asks for it, and a missing library is a
`ConfigError` with the install command, not an `ImportError`.

Events. An agent's event names are its own, so none is hardcoded: config says which
are content (carry reply text), metadata (carry routing, tools, citations, and
arrive on their own events, usually before the text), the terminator (the reply is
complete), and errors. Every other event is ignorable and is logged once by name.
Every classified event's payload is merged and read with the same `response_mapping`
the HTTP adapter uses, so metadata is harvested from all of them, not just the one
that carried the text.

One connection per session. A session's connection is opened by its first turn and
reused for the rest, and it is never reopened. A reconnect starts a new server-side
conversation without the history, which shows up in a report as the agent forgetting
- a memory bug that is really ours. So a connection that is lost takes its session
with it: every later turn of that session fails and says why, and new sessions are
unaffected.

Retries, and what they cost. Only opening a connection is retried: nothing has been
sent, so trying again cannot deliver anything twice. Once a turn has been emitted it
is NEVER sent again, whatever happens next (timeout, disconnect, error event): the
agent may already have acted on it, and a replay would be answered by an agent that
now holds the turn twice. This is the opposite of what `HTTPAdapter` does for a
retried POST, deliberately.

Completion. A reply ends at the terminator event. A server with none needs an
explicit `idle_timeout_s`: the reply is complete once that long passes after its
last event. There is no default for it, because a short guess would cut a reply that
pauses mid-stream and grade the fragment. `timeout_s` (120 by default; replies on
this class of system run past 45s) bounds the whole turn, and is where a hung agent
ends up, as an error and not as a score.

Rate limits. `rate_limit` is a list of sliding windows, and `send` waits for room in
all of them before emitting. The limiter is shared by every worker of the adapter.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import json
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..core.contracts import ChatResponse, Intent
from ..core.errors import AgentCallError, ConfigError
from ..core.logging import get_logger
from .base import AdapterLifecycle
from .payload import (
    MISSING,
    chunk,
    citation,
    dig,
    fill,
    status_from,
    tool,
    validated_headers,
    validated_mapping,
    validated_template,
)

log = get_logger(__name__)

INSTALL_HINT = "agent.adapter 'socketio' needs python-socketio. Run: pip install -e .[socketio]"

DEFAULT_REQUEST_TEMPLATE: Mapping[str, Any] = {"message": "{message}"}

#: Longest the computed backoff between connection attempts grows to.
MAX_BACKOFF_S = 30.0

_DISCONNECT = "\0disconnect"
_CONTENT_MODES = ("append", "replace")


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Credentials:
    """What a connection presents when it opens: HTTP headers and/or a connect payload."""

    headers: Mapping[str, str] = field(default_factory=dict)
    #: The `auth` object of the Socket.IO CONNECT packet, for servers that read it there.
    auth: Mapping[str, Any] | None = None


class ConnectionAuth(Protocol):
    """Supplies credentials for a principal, and can be told they were refused.

    Called every time a connection opens, so a token that expired mid-run is
    replaced for the next session without the runner knowing. `refresh=True` means
    the credentials last returned were refused: do not hand out the same ones.
    Implementations must be thread-safe; workers open connections concurrently.
    """

    def credentials(self, principal: str, *, refresh: bool = False) -> Credentials: ...


class StaticAuth:
    """Fixed credentials per principal label, from config. Never falls back to anonymous."""

    def __init__(
        self,
        principal_headers: Mapping[str, Mapping[str, str]],
        principal_auth: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._headers = {
            label: validated_headers(value, f"agent.principal_headers.{label}")
            for label, value in principal_headers.items()
        }
        self._auth = {label: dict(value) for label, value in (principal_auth or {}).items()}
        for label in self._auth:
            if label not in self._headers:
                raise ConfigError(
                    f"agent.principal_auth.{label} has no matching agent.principal_headers entry; "
                    "use `{}` there for a principal that needs no header"
                )

    def credentials(self, principal: str, *, refresh: bool = False) -> Credentials:
        try:
            headers = self._headers[principal]
        except KeyError:
            raise ConfigError(
                f"unknown principal {principal!r}; agent.principal_headers knows: "
                f"{sorted(self._headers)}"
            ) from None
        return Credentials(headers=headers, auth=self._auth.get(principal))


@dataclass(frozen=True, slots=True)
class Token:
    value: str
    #: Epoch seconds, or None when the token says nothing about its own expiry.
    expires_at: float | None = None


def jwt_expiry(token: str) -> float | None:
    """The `exp` claim of a JWT, read WITHOUT verifying it. None if there is none.

    Only used to decide when to ask for a new token, never to trust one.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


class CachedTokenAuth:
    """Base for an auth hook that logs in (signin, OTP, ...) and keeps the token fresh.

    Subclass it and write `login`; that is where the signin/OTP exchange lives.
    Override `refresh` to use a refresh token instead of logging in again. The token
    is cached per principal and replaced shortly before it expires, or at once when
    the adapter says it was refused (`refresh=True`). One lock serialises logins:
    they are rare, and two workers must not run an OTP flow for one principal twice.
    A failure raises `AgentCallError`.
    """

    header = "Authorization"
    scheme = "Bearer"
    #: Renew this long before the token's own expiry, so it is never presented dead.
    skew_s = 30.0

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._tokens: dict[str, Token] = {}
        self._lock = threading.Lock()

    def login(self, principal: str) -> Token:
        raise NotImplementedError("a CachedTokenAuth subclass implements login()")

    def refresh(self, principal: str, stale: Token) -> Token:
        return self.login(principal)

    def credentials(self, principal: str, *, refresh: bool = False) -> Credentials:
        with self._lock:
            token = self._tokens.get(principal)
            if token is None:
                token = self.login(principal)
            elif refresh or self._expired(token):
                try:
                    token = self.refresh(principal, token)
                except AgentCallError as exc:
                    log.warning("Token refresh for %r failed (%s); logging in again.", principal, exc)
                    token = self.login(principal)
            self._tokens[principal] = token
        value = f"{self.scheme} {token.value}" if self.scheme else token.value
        return Credentials(headers={self.header: value})

    def _expired(self, token: Token) -> bool:
        expires = token.expires_at if token.expires_at is not None else jwt_expiry(token.value)
        return expires is not None and self._clock() >= expires - self.skew_s


def load_auth(spec: Mapping[str, Any]) -> ConnectionAuth:
    """Build the hook named by `agent.auth`: `{hook: "package.module:factory", options: {...}}`.

    The factory is called with `options` as keyword arguments. It is code named by
    your own config file, run with your own privileges, exactly like a plugin.
    """
    stray = sorted(set(spec) - {"hook", "options"})
    if stray:
        raise ConfigError(f"unknown agent.auth key(s): {stray}; known: hook, options")
    hook = spec.get("hook")
    if not isinstance(hook, str) or hook.count(":") != 1 or not all(hook.split(":")):
        raise ConfigError(f"agent.auth.hook must be 'package.module:factory', got {hook!r}")
    options = spec.get("options") or {}
    if not isinstance(options, Mapping):
        raise ConfigError(f"agent.auth.options must be a mapping, got {options!r}")
    module_name, _, factory_name = hook.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), factory_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"agent.auth.hook {hook!r} could not be loaded: {exc}") from exc
    try:
        auth: ConnectionAuth = factory(**options)
    except TypeError as exc:
        raise ConfigError(f"agent.auth.hook {hook!r} does not fit its options: {exc}") from exc
    if not callable(getattr(auth, "credentials", None)):
        raise ConfigError(f"agent.auth.hook {hook!r} returned something with no `credentials()`")
    return auth


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Sliding windows of (max requests, per seconds). `acquire` waits until all have room.

    Thread-safe. A request is counted when it is let through, so concurrent
    workers share one budget and the agent never sees more than the windows allow.
    """

    def __init__(
        self,
        windows: Sequence[tuple[int, float]],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._windows = [(count, per_s, deque[float]()) for count, per_s in windows]
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a request may go, and return how long that took, in seconds."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                wait = 0.0
                for count, per_s, stamps in self._windows:
                    while stamps and now - stamps[0] >= per_s:
                        stamps.popleft()
                    if len(stamps) >= count:
                        wait = max(wait, per_s - (now - stamps[0]))
                if wait <= 0:
                    for _, _, stamps in self._windows:
                        stamps.append(now)
                    return waited
            self._sleep(wait)
            waited += wait


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Events:
    """Which event names mean what. Disjoint by construction."""

    send: str
    content: frozenset[str]
    metadata: frozenset[str] = frozenset()
    terminator: str | None = None
    errors: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        named = [*self.content, *self.metadata, *self.errors]
        if self.terminator is not None:
            named.append(self.terminator)
        clash = sorted({n for n in named if named.count(n) > 1})
        if clash:
            raise ConfigError(f"agent.events: {clash} are given more than one role")

    def classify(self, event: str) -> str:
        if event in self.errors:
            return "error"
        if event == self.terminator:
            return "terminator"
        if event in self.content:
            return "content"
        if event in self.metadata:
            return "metadata"
        return "ignorable"


class _Conn:
    """One session's connection, and the events it has received and not yet used."""

    def __init__(self, principal: str) -> None:
        self.principal = principal
        self.client: Any = None
        self.inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.turn_lock = threading.Lock()
        #: Why the connection is gone, once it is. Set from the client's own thread.
        self.lost: str | None = None


class SocketIOAdapter(AdapterLifecycle):
    """`AgentAdapter` over a Socket.IO namespace: one connection per session, never replayed."""

    def __init__(
        self,
        url: str,
        *,
        events: Events,
        auth: ConnectionAuth,
        namespace: str = "/",
        socketio_path: str = "socket.io",
        transports: Sequence[str] | None = None,
        request_template: Mapping[str, Any] | None = None,
        response_mapping: Mapping[str, str] | None = None,
        content_mode: str = "append",
        timeout_s: float = 120.0,
        idle_timeout_s: float | None = None,
        connect_timeout_s: float = 10.0,
        max_attempts: int = 3,
        backoff_s: float = 0.5,
        rate_limit: Sequence[tuple[int, float]] = (),
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__()
        try:
            self._sio: ModuleType = importlib.import_module("socketio")
        except ImportError as exc:
            raise ConfigError(INSTALL_HINT) from exc
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https", "ws", "wss") or not parts.netloc:
            raise ConfigError(f"agent.url must be an http(s) or ws(s) URL, got {url!r}")
        if not namespace.startswith("/"):
            raise ConfigError(f"agent.namespace must start with '/', got {namespace!r}")
        if content_mode not in _CONTENT_MODES:
            raise ConfigError(f"agent.content_mode must be one of {_CONTENT_MODES}, got {content_mode!r}")
        for name, value in (("timeout_s", timeout_s), ("connect_timeout_s", connect_timeout_s)):
            if value <= 0:
                raise ConfigError(f"agent.{name} must be positive, got {value!r}")
        if idle_timeout_s is not None and idle_timeout_s <= 0:
            raise ConfigError(f"agent.idle_timeout_s must be positive, got {idle_timeout_s!r}")
        if events.terminator is None and idle_timeout_s is None:
            raise ConfigError(
                "agent has no way to tell a reply is complete: set agent.events.terminator, "
                "or agent.idle_timeout_s (seconds of quiet that end a reply). There is no "
                "default: a short guess would cut a reply that pauses and grade the fragment."
            )
        if max_attempts < 1 or backoff_s < 0:
            raise ConfigError("agent.max_attempts must be >= 1 and agent.backoff_s >= 0")

        self._url = url
        self._endpoint = f"{parts.scheme}://{parts.hostname}{f':{parts.port}' if parts.port else ''}{namespace}"
        self._namespace = namespace
        self._socketio_path = socketio_path
        self._transports = list(transports) if transports else None
        self._events = events
        self._auth = auth
        self._template = validated_template(request_template or DEFAULT_REQUEST_TEMPLATE)
        self._mapping = validated_mapping(response_mapping)
        self._content_mode = content_mode
        self._timeout_s = timeout_s
        self._idle_timeout_s = idle_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._limiter = limiter or RateLimiter(list(rate_limit))
        self._conns: dict[str, _Conn] = {}
        self._conns_lock = threading.Lock()
        self._unclassified: set[str] = set()

    @classmethod
    def from_config(cls, agent: Mapping[str, Any]) -> SocketIOAdapter:
        """Build from the `agent:` section of your config file."""
        known = {
            "adapter", "url", "namespace", "socketio_path", "transports", "events",
            "request_template", "response_mapping", "content_mode", "timeout_s",
            "idle_timeout_s", "connect_timeout_s", "max_attempts", "backoff_s", "rate_limit",
            "principal_headers", "principal_auth", "auth",
            # Read by the factory; named here so the stray-key check lets it through.
            "side_effecting_tools",
        }  # fmt: skip
        stray = sorted(set(agent) - known)
        if stray:
            raise ConfigError(
                f"unknown agent key(s) for the socketio adapter: {stray}; known: {sorted(known)}"
            )
        url = agent.get("url")
        if not isinstance(url, str) or not url:
            raise ConfigError("agent.url is required for the socketio adapter")
        return cls(
            url,
            events=_events_from(agent.get("events")),
            auth=_auth_from(agent),
            namespace=_text(agent, "namespace", "/"),
            socketio_path=_text(agent, "socketio_path", "socket.io"),
            transports=_str_list(agent, "transports") or None,
            request_template=_optional_mapping(agent, "request_template"),
            response_mapping=_optional_mapping(agent, "response_mapping"),
            content_mode=_text(agent, "content_mode", "append"),
            timeout_s=_float(agent, "timeout_s", 120.0),
            idle_timeout_s=_number(agent, "idle_timeout_s", None),
            connect_timeout_s=_float(agent, "connect_timeout_s", 10.0),
            max_attempts=int(_float(agent, "max_attempts", 3.0)),
            backoff_s=_float(agent, "backoff_s", 0.5),
            rate_limit=_rate_limit_from(agent.get("rate_limit")),
        )

    # ------------------------------------------------------------------ #
    # Lifecycle: connections live for the run, one per session.
    # ------------------------------------------------------------------ #

    def _close(self) -> None:
        with self._conns_lock:
            conns, self._conns = list(self._conns.values()), {}
        clients = []
        for conn in conns:
            client, conn.client = conn.client, None
            if client is not None:
                clients.append(client)
        if not clients:
            return
        # In parallel: a close can wait on the server (seconds, on some), and a run
        # holds one connection per session.
        with ThreadPoolExecutor(max_workers=min(16, len(clients))) as pool:
            list(pool.map(self._disconnect, clients))

    @staticmethod
    def _disconnect(client: Any) -> None:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - documented boundary: closing must not mask the run's own error
            log.exception("Socket.IO disconnect failed while closing the adapter")

    def describe(self) -> dict[str, str]:
        """The endpoint and namespace, without credentials or a query string."""
        self._require_open("describe")
        return {"adapter": "socketio", "endpoint": self._endpoint}

    # ------------------------------------------------------------------ #
    # AgentAdapter
    # ------------------------------------------------------------------ #

    def send(
        self, message: str, *, session_id: str, principal: str = "default"
    ) -> ChatResponse:
        """Emit one turn on the session's connection and collect the reply it provokes.

        Opens the session's connection on its first turn. Never re-sends a turn:
        any failure after the emit is raised, not retried.
        """
        self._require_open("send")
        conn = self._conn_for(session_id, principal)
        with conn.turn_lock:
            if conn.client is None and conn.lost is None:
                self._connect(conn, session_id)
            self._require_alive(conn, session_id)

            body = fill(self._template, {"message": message, "session_id": session_id})
            self._limiter.acquire()
            stale = self._drain(conn)
            if stale:
                log.debug("Session %s: discarded %d late event(s) from the last turn", session_id, stale)

            started = time.perf_counter()
            try:
                conn.client.emit(self._events.send, body, namespace=self._namespace)
            except Exception as exc:  # noqa: BLE001 - the library raises several types; delivery is unknown either way
                conn.lost = f"emit failed: {type(exc).__name__}: {exc}"
                raise AgentCallError(
                    f"{self._endpoint} could not take the turn for session {session_id!r} "
                    f"({type(exc).__name__}); not retried, the session is lost"
                ) from exc

            merged, texts = self._await_reply(conn, session_id)
            latency_ms = (time.perf_counter() - started) * 1000
        return self._map(merged, texts, session_id, latency_ms)

    # ------------------------------------------------------------------ #
    # Connections
    # ------------------------------------------------------------------ #

    def _conn_for(self, session_id: str, principal: str) -> _Conn:
        with self._conns_lock:
            conn = self._conns.get(session_id)
            if conn is None:
                conn = self._conns[session_id] = _Conn(principal)
        if conn.principal != principal:
            raise ConfigError(
                f"session {session_id!r} was opened as principal {conn.principal!r} and cannot "
                f"continue as {principal!r}: one connection is one identity"
            )
        return conn

    def _require_alive(self, conn: _Conn, session_id: str) -> None:
        if conn.lost is None and conn.client is not None and not conn.client.connected:
            conn.lost = "the connection dropped between turns"
        if conn.lost is not None:
            raise AgentCallError(
                f"{self._endpoint} session {session_id!r} lost its connection ({conn.lost}). "
                "No reconnect was made: a new connection is a new conversation without this "
                "one's history, and would read as the agent forgetting."
            )

    def _connect(self, conn: _Conn, session_id: str) -> None:
        """Open the session's connection. Safe to retry: nothing has been sent on it."""
        sio = self._sio
        refresh = False
        for attempt in range(1, self._max_attempts + 1):
            credentials = self._auth.credentials(conn.principal, refresh=refresh)
            # reconnection=False: the library would otherwise reconnect on its own,
            # silently starting the conversation over.
            client = sio.Client(reconnection=False, logger=False, engineio_logger=False)
            client.on("*", self._catch_all(conn), namespace=self._namespace)
            client.on("disconnect", self._on_disconnect(conn), namespace=self._namespace)
            try:
                client.connect(
                    self._url,
                    headers=dict(credentials.headers),
                    auth=dict(credentials.auth) if credentials.auth is not None else None,
                    namespaces=[self._namespace],
                    transports=self._transports,
                    socketio_path=self._socketio_path,
                    wait_timeout=self._connect_timeout_s,
                )
            except sio.exceptions.SocketIOError as exc:
                self._discard(client)
                if attempt == self._max_attempts:
                    raise AgentCallError(
                        f"{self._endpoint} refused or dropped the connection for session "
                        f"{session_id!r} after {attempt} attempt(s): {type(exc).__name__}: {exc}"
                    ) from exc
                # A refusal may be an expired token: ask for fresh credentials next time.
                refresh = True
                delay = min(self._backoff_s * 2 ** (attempt - 1), MAX_BACKOFF_S)
                log.warning(
                    "%s: connection attempt %d/%d failed (%s); retrying in %.2fs",
                    self._endpoint, attempt, self._max_attempts, type(exc).__name__, delay,
                )
                self._sleep(delay)
                continue
            conn.client = client
            return

    @staticmethod
    def _discard(client: Any) -> None:
        # A connect that failed has usually torn itself down; closing it again waits.
        if getattr(getattr(client, "eio", None), "state", "connected") == "disconnected":
            return
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - a half-open client being tidied; the connect error is the one that matters
            log.debug("Socket.IO client tidy-up after a failed connect raised", exc_info=True)

    def _catch_all(self, conn: _Conn) -> Callable[..., None]:
        def handler(event: str, *args: Any) -> None:
            conn.inbox.put((event, args[0] if len(args) == 1 else list(args)))

        return handler

    def _on_disconnect(self, conn: _Conn) -> Callable[..., None]:
        def handler(*_: Any) -> None:
            # Set before the sentinel, so a waiting turn that wakes sees why.
            if conn.lost is None:
                conn.lost = "the server closed the connection"
            conn.inbox.put((_DISCONNECT, None))

        return handler

    @staticmethod
    def _drain(conn: _Conn) -> int:
        dropped = 0
        while True:
            try:
                event, _ = conn.inbox.get_nowait()
            except queue.Empty:
                return dropped
            if event != _DISCONNECT:
                dropped += 1

    # ------------------------------------------------------------------ #
    # Reading a reply
    # ------------------------------------------------------------------ #

    def _await_reply(self, conn: _Conn, session_id: str) -> tuple[dict[str, Any], list[str]]:
        """Classify events until the reply is complete: terminator, idle, error or timeout."""
        deadline = time.monotonic() + self._timeout_s
        merged: dict[str, Any] = {}
        texts: list[str] = []
        last = time.monotonic()
        while True:
            now = time.monotonic()
            wait = deadline - now
            if wait <= 0:
                raise AgentCallError(
                    f"{self._endpoint} sent no complete reply for session {session_id!r} within "
                    f"{self._timeout_s:g}s; the turn was delivered and is not retried"
                )
            if self._idle_timeout_s is not None and texts:
                quiet_left = self._idle_timeout_s - (now - last)
                if quiet_left <= 0:
                    return merged, texts
                wait = min(wait, quiet_left)
            try:
                event, payload = conn.inbox.get(timeout=wait)
            except queue.Empty:
                continue

            if event == _DISCONNECT:
                raise AgentCallError(
                    f"{self._endpoint} dropped the connection while session {session_id!r} "
                    "was waiting for a reply. The turn may have been delivered, so it was not "
                    "retried, and the session is lost."
                )
            kind = self._events.classify(event)
            if kind == "ignorable":
                self._note_unclassified(event)
                continue
            last = time.monotonic()
            if kind == "error":
                raise AgentCallError(
                    f"{self._endpoint} sent error event {event!r} for session {session_id!r}: "
                    f"{_snippet(payload)}"
                )
            if isinstance(payload, dict):
                _merge(merged, payload)
            if kind == "content":
                piece = self._text_of(payload, event)
                if piece is not None:
                    if self._content_mode == "replace":
                        texts[:] = [piece]
                    else:
                        texts.append(piece)
            elif kind == "terminator":
                if not texts:
                    raise AgentCallError(
                        f"{self._endpoint} sent terminator {event!r} for session {session_id!r} "
                        "with no content event before it; there is no reply to grade"
                    )
                return merged, texts

    def _text_of(self, payload: Any, event: str) -> str | None:
        if isinstance(payload, str):
            return payload
        found = dig(payload, self._mapping["text"]) if isinstance(payload, dict) else MISSING
        if found is MISSING:
            return None
        if not isinstance(found, str):
            raise AgentCallError(
                f"content event {event!r}: response_mapping.text path {self._mapping['text']!r} "
                f"is a {type(found).__name__}, not text"
            )
        return found

    def _note_unclassified(self, event: str) -> None:
        with self._conns_lock:
            first = event not in self._unclassified
            self._unclassified.add(event)
        if first:
            log.warning(
                "%s: ignoring event %r, which agent.events does not classify. If it carries "
                "reply text or metadata, list it under content or metadata.",
                self._endpoint, event,
            )

    def _map(
        self, merged: dict[str, Any], texts: list[str], session_id: str, latency_ms: float
    ) -> ChatResponse:
        m = self._mapping
        route, intent, session = dig(merged, m["route"]), dig(merged, m["intent"]), dig(merged, m["session"])
        return ChatResponse(
            reply="".join(texts),
            conversation_id=session if isinstance(session, str) and session else session_id,
            route=route if isinstance(route, str) and route else "unknown",
            status=status_from(dig(merged, m["status"])),
            intent=Intent(name=intent if isinstance(intent, str) and intent else "unknown"),
            citations=[citation(i, c) for i, c in enumerate(self._list(merged, "citations"))],
            retrieved=[chunk(i, c) for i, c in enumerate(self._list(merged, "retrieved"))],
            tool_calls=[tool(i, t) for i, t in enumerate(self._list(merged, "tools"))],
            latency_ms=round(latency_ms, 3),
        )

    def _list(self, merged: dict[str, Any], key: str) -> list[Any]:
        """An optional list field: absent is empty, present-but-wrong is a contract break."""
        value = dig(merged, self._mapping[key])
        if value is MISSING or value is None:
            return []
        if not isinstance(value, list):
            raise AgentCallError(
                f"response_mapping.{key} path {self._mapping[key]!r} is a "
                f"{type(value).__name__}, not a list"
            )
        return value


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _merge(into: dict[str, Any], payload: Mapping[str, Any]) -> None:
    """Fold one event's payload into the reply's: lists extend, objects merge, scalars replace."""
    for key, value in payload.items():
        current = into.get(key)
        if isinstance(current, list) and isinstance(value, list):
            current.extend(value)
        elif isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value)
        else:
            into[key] = list(value) if isinstance(value, list) else value


def _snippet(payload: Any) -> str:
    text = " ".join(str(payload).split())
    return text[:200] + ("..." if len(text) > 200 else "") or "(empty)"


def _events_from(raw: Any) -> Events:
    if not isinstance(raw, Mapping):
        raise ConfigError("agent.events is required: a mapping with at least `send` and `content`")
    stray = sorted(set(raw) - {"send", "content", "metadata", "terminator", "errors"})
    if stray:
        raise ConfigError(
            f"unknown agent.events key(s): {stray}; known: content, errors, metadata, send, terminator"
        )
    send = raw.get("send")
    if not isinstance(send, str) or not send:
        raise ConfigError("agent.events.send must be the event name a turn is emitted on")
    content = _name_set(raw, "content")
    if not content:
        raise ConfigError("agent.events.content must name at least one event that carries reply text")
    terminator = raw.get("terminator")
    if terminator is not None and (not isinstance(terminator, str) or not terminator):
        raise ConfigError(f"agent.events.terminator must be an event name, got {terminator!r}")
    return Events(
        send=send,
        content=content,
        metadata=_name_set(raw, "metadata"),
        terminator=terminator,
        errors=_name_set(raw, "errors"),
    )


def _name_set(raw: Mapping[str, Any], key: str) -> frozenset[str]:
    value = raw.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"agent.events.{key} must be a list of event names, got {value!r}")
    return frozenset(value)


def _auth_from(agent: Mapping[str, Any]) -> ConnectionAuth:
    hook, headers = agent.get("auth"), agent.get("principal_headers")
    if hook is not None and headers is not None:
        raise ConfigError("give agent.auth (a hook) or agent.principal_headers (static), not both")
    if hook is not None:
        if not isinstance(hook, Mapping):
            raise ConfigError(f"agent.auth must be a mapping, got {hook!r}")
        return load_auth(hook)
    if not isinstance(headers, Mapping):
        raise ConfigError(
            "agent.principal_headers is required for the socketio adapter: a mapping of "
            "principal label -> {Header: value}. Use `default: {}` for an agent with no "
            "authentication; a missing label never falls back to anonymous. "
            "Or set agent.auth for a login hook."
        )
    for label, value in headers.items():
        if not isinstance(value, Mapping):
            raise ConfigError(f"agent.principal_headers.{label} must be a mapping, got {value!r}")
    fields = agent.get("principal_auth")
    if fields is not None and not isinstance(fields, Mapping):
        raise ConfigError(f"agent.principal_auth must be a mapping, got {fields!r}")
    return StaticAuth(headers, fields)


def _rate_limit_from(raw: Any) -> list[tuple[int, float]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"agent.rate_limit must be a list of {{requests, per_s}}, got {raw!r}")
    windows: list[tuple[int, float]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) != {"requests", "per_s"}:
            raise ConfigError(f"agent.rate_limit entries need exactly requests and per_s, got {entry!r}")
        count, per_s = entry["requests"], entry["per_s"]
        if (
            isinstance(count, bool) or not isinstance(count, int) or count < 1
            or isinstance(per_s, bool) or not isinstance(per_s, (int, float)) or per_s <= 0
        ):  # fmt: skip
            raise ConfigError(f"agent.rate_limit entry {entry!r}: requests >= 1 and per_s > 0")
        windows.append((count, float(per_s)))
    return windows


def _optional_mapping(agent: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = agent.get(key)
    if value is not None and not isinstance(value, Mapping):
        raise ConfigError(f"agent.{key} must be a mapping, got {value!r}")
    return value


def _text(agent: Mapping[str, Any], key: str, default: str) -> str:
    value = agent.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"agent.{key} must be non-empty text, got {value!r}")
    return value


def _str_list(agent: Mapping[str, Any], key: str) -> list[str]:
    value = agent.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"agent.{key} must be a list of strings, got {value!r}")
    return value


def _number(agent: Mapping[str, Any], key: str, default: float | None) -> float | None:
    value = agent.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"agent.{key} must be a number, got {value!r}")
    return float(value)


def _float(agent: Mapping[str, Any], key: str, default: float) -> float:
    """A number with a default. A present value is checked as it is: 0 is not "unset"."""
    value = _number(agent, key, default)
    assert value is not None  # the default is a float, so None cannot come back
    return value


__all__ = [
    "INSTALL_HINT",
    "CachedTokenAuth",
    "ConnectionAuth",
    "Credentials",
    "Events",
    "RateLimiter",
    "SocketIOAdapter",
    "StaticAuth",
    "Token",
    "jwt_expiry",
    "load_auth",
]
