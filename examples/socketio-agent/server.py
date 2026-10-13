"""A Flask-SocketIO agent to evaluate over a real socket: the reference for the adapter.

It speaks real Socket.IO - the HTTP handshake and upgrade, the `/chat` namespace,
event framing - so a test against it exercises the protocol, which a mocked client
would not. It also has the shape of the systems the adapter is for: JWT sign-in
over HTTP (`/signin`, `/verifyotp`, `/refresh`), then a socket on which one turn
provokes several events - routing and tool metadata first, then the reply text,
then a terminator.

    python server.py                       # 127.0.0.1:9100, no auth
    python server.py --require-auth        # the socket needs a JWT from /signin + /verifyotp
    python server.py --no-terminator       # replies end by going quiet, not by an event

The message picks the behaviour, so each can be tested on its own:
    "remember ..."   replies with the FIRST thing this connection said (needs one connection)
    "who"            replies with the connection's id
    "stream"         three content events, no single event holds the reply
    "boom"           sends an error event
    "drop"           closes the connection without replying
    "slow"           replies after two seconds
anything else       routing + tool metadata, then "You said: <message>", then the terminator

Needs the `socketio-server` extra (flask-socketio, simple-websocket).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from flask import Flask, jsonify, request
from flask_socketio import SocketIO, disconnect, emit
from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server

NAMESPACE = "/chat"
USER, PASSWORD, OTP = "demo-user", "pw", "123456"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(claims: dict[str, Any], secret: str) -> str:
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def verify_jwt(token: str, secret: str, now: float) -> dict[str, Any] | None:
    try:
        head, body, sig = token.split(".")
        expected = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64(expected), sig):
            return None
        claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    return claims if claims.get("exp", 0) > now else None


@dataclass
class ServerState:
    """What the server saw, for a test to assert on. Guarded by `lock`."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    #: (sid, message) for every turn the server received, in arrival order.
    received: list[tuple[str, str]] = field(default_factory=list)
    #: sid of every connection accepted on the namespace.
    connects: list[str] = field(default_factory=list)
    #: Connections to refuse before accepting one, to look like an expired token.
    refuse_connects: int = 0
    signins: int = 0
    otp_verifications: int = 0
    refreshes: int = 0
    memory: dict[str, list[str]] = field(default_factory=dict)


class _Http11Handler(WSGIRequestHandler):
    # A WebSocket upgrade needs the connection to outlive one request, which the dev
    # server only allows on HTTP/1.1. Flask-SocketIO's own `run()` sets the same.
    protocol_version = "HTTP/1.1"


class AgentServer:
    """The example agent, runnable in a thread for a test or from the command line."""

    def __init__(
        self,
        *,
        terminator: bool = True,
        event: str = "ask",
        require_auth: bool = False,
        secret: str = "example-secret",
        token_ttl_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state = ServerState()
        self.terminator = terminator
        #: The event a turn arrives on. `ask` unless a test needs a name a prober tries first.
        self.event = event
        self.require_auth = require_auth
        self._secret = secret
        self._ttl = token_ttl_s
        self._clock = clock
        self.app = Flask(__name__)
        self.sio = SocketIO(self.app, async_mode="threading", cors_allowed_origins="*")
        self._http: BaseWSGIServer | None = None
        self._thread: threading.Thread | None = None
        self._routes()
        self._events()

    # --------------------------------------------------------------- sign-in

    def _tokens(self) -> dict[str, Any]:
        now = self._clock()
        access = make_jwt({"sub": USER, "exp": now + self._ttl}, self._secret)
        refresh = make_jwt({"sub": USER, "exp": now + 10 * self._ttl, "use": "refresh"}, self._secret)
        return {"access_token": access, "refresh_token": refresh, "expires_in": self._ttl}

    def _routes(self) -> None:
        app, state = self.app, self.state

        @app.get("/health")
        def health() -> Any:
            return jsonify(ok=True)

        @app.post("/signin")
        def signin() -> Any:
            body = request.get_json(silent=True) or {}
            if body.get("username") != USER or body.get("password") != PASSWORD:
                return jsonify(error="bad credentials"), 401
            with state.lock:
                state.signins += 1
            return jsonify(otp_required=True)

        @app.post("/verifyotp")
        def verify() -> Any:
            body = request.get_json(silent=True) or {}
            if body.get("username") != USER or body.get("otp") != OTP:
                return jsonify(error="bad otp"), 401
            with state.lock:
                state.otp_verifications += 1
            return jsonify(**self._tokens())

        @app.post("/refresh")
        def refresh() -> Any:
            body = request.get_json(silent=True) or {}
            claims = verify_jwt(str(body.get("refresh_token", "")), self._secret, self._clock())
            if claims is None or claims.get("use") != "refresh":
                return jsonify(error="bad refresh token"), 401
            with state.lock:
                state.refreshes += 1
            return jsonify(**self._tokens())

    # ---------------------------------------------------------------- socket

    def _authorised(self) -> bool:
        if not self.require_auth:
            return True
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        return scheme == "Bearer" and verify_jwt(token, self._secret, self._clock()) is not None

    def _events(self) -> None:
        sio, state = self.sio, self.state

        @sio.on("connect", namespace=NAMESPACE)
        def on_connect(auth: Any = None) -> bool:
            with state.lock:
                if state.refuse_connects > 0:
                    state.refuse_connects -= 1
                    return False
            if not self._authorised():
                return False
            with state.lock:
                state.connects.append(request.sid)
                state.memory[request.sid] = []
            return True

        @sio.on(self.event, namespace=NAMESPACE)
        def on_ask(data: Any) -> None:
            sid: str = request.sid
            message = str(data.get("message", "")) if isinstance(data, dict) else str(data)
            with state.lock:
                state.received.append((sid, message))
                earlier = list(state.memory.setdefault(sid, []))
                state.memory[sid].append(message)
            self._behave(sid, message, earlier)

    def _finish(self) -> None:
        if self.terminator:
            emit("done", {})

    def _behave(self, sid: str, message: str, earlier: list[str]) -> None:
        text = message.lower()
        if text.startswith("remember"):
            first = earlier[0] if earlier else "(nothing)"
            emit("reply", {"text": f"The first thing you said was: {first}"})
        elif text == "who":
            emit("reply", {"text": f"connection {sid}"})
        elif text == "stream":
            for piece in ("alpha ", "beta ", "gamma"):
                emit("chunk", {"text": piece})
                time.sleep(0.05)
        elif text == "boom":
            emit("oops", {"message": "upstream failed"})
            return
        elif text == "drop":
            disconnect()
            return
        elif text == "slow":
            time.sleep(2.0)
            emit("reply", {"text": "Sorry for the wait."})
        else:
            # Metadata first, on events of its own, as on the systems this models.
            emit("routing", {"route": "policy_agent", "status": "answered"})
            emit("tool_status", {
                "tools": [{"name": "knowledge_search", "ok": True}],
                "citations": [{"doc_id": "HR-001", "snippet": "annual leave"}],
            })  # fmt: skip
            emit("typing", {"on": False})  # chatter nothing should depend on
            emit("reply", {"text": f"You said: {message}"})
        self._finish()

    # ------------------------------------------------------------- lifecycle

    def start(self, port: int = 0) -> int:
        """Serve on 127.0.0.1 in a thread and return the port (a free one when 0)."""
        # The dev server logs a 400 when a WebSocket it handed off closes. Harmless noise.
        logging.getLogger("werkzeug").setLevel(logging.CRITICAL)
        self._http = make_server(
            "127.0.0.1", port, self.app, threaded=True, request_handler=_Http11Handler
        )
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._thread.start()
        return int(self._http.server_port)

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--require-auth", action="store_true")
    parser.add_argument("--no-terminator", action="store_true")
    args = parser.parse_args()
    server = AgentServer(terminator=not args.no_terminator, require_auth=args.require_auth)
    port = server.start(args.port)
    print(f"socketio agent on http://127.0.0.1:{port}{NAMESPACE} (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
