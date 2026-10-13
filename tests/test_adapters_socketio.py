"""The Socket.IO adapter, against a real Flask-SocketIO server over a real socket.

A mocked client would prove only that we agree with ourselves about a protocol none
of us had spoken. So every test that touches the wire opens a TCP connection to
`examples/socketio-agent/server.py`, which speaks actual Socket.IO: the HTTP
handshake and upgrade, the `/chat` namespace, event framing. What the server saw is
read back from its own state, which is how the "never replayed" and "one connection
per session" claims are checked against the agent and not against the adapter's
account of itself.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from optional_deps import requires

requires("socketio", extra="socketio", test_module="tests/test_adapters_socketio.py")
requires("flask_socketio", extra="socketio-server", test_module="tests/test_adapters_socketio.py")

from askback.adapters import socketio_adapter as sio_module  # noqa: E402
from askback.adapters.factory import build_adapter  # noqa: E402
from askback.adapters.socketio_adapter import (  # noqa: E402
    CachedTokenAuth,
    Credentials,
    RateLimiter,
    SocketIOAdapter,
    Token,
    jwt_expiry,
    load_auth,
)
from askback.core.config import AppConfig  # noqa: E402
from askback.core.errors import AgentCallError, ConfigError  # noqa: E402
from askback.datasets.schema import Expectations, TestCase, Turn  # noqa: E402
from askback.evaluation.engine import EvaluationEngine  # noqa: E402
from askback.evaluation.rule_based import RuleBasedEvaluator  # noqa: E402
from askback.runner import TestRunner  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "socketio-agent"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"socketio_example_{name}", EXAMPLE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass resolves its own module by name while it is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


server_module = _load("server")
auth_module = _load("auth")


def _config(port: int, **override: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "adapter": "socketio",
        "url": f"http://127.0.0.1:{port}",
        "namespace": "/chat",
        "events": {
            "send": "ask",
            "content": ["reply", "chunk"],
            "metadata": ["routing", "tool_status"],
            "terminator": "done",
            "errors": ["oops"],
        },
        "response_mapping": {"text": "text", "tools": "tools"},
        "principal_headers": {"default": {}},
        "max_attempts": 1,
    }
    config.update(override)
    return config


@pytest.fixture
def server() -> Iterator[Any]:
    agent = server_module.AgentServer()
    agent.port = agent.start()
    yield agent
    agent.stop()


def _adapter(server: Any, **override: Any) -> SocketIOAdapter:
    return SocketIOAdapter.from_config(_config(server.port, **override))


class TestWire:
    @pytest.mark.parametrize("transports", [None, ["polling"]], ids=["upgrade", "polling-only"])
    def test_the_handshake_and_namespace_work_over_a_real_socket(
        self, server: Any, transports: list[str] | None
    ) -> None:
        extra = {"transports": transports} if transports else {}
        with _adapter(server, **extra) as adapter:
            reply = adapter.send("hi", session_id="s1")
            client = adapter._conns["s1"].client
            transport = client.transport()

        assert reply.reply == "You said: hi"
        assert transport == ("polling" if transports else "websocket")
        assert len(server.state.connects) == 1, "the server accepted one connection on /chat"

    def test_the_wrong_namespace_is_refused_by_the_server_not_papered_over(self, server: Any) -> None:
        with _adapter(server, namespace="/nope") as adapter, pytest.raises(AgentCallError, match="refused"):
            adapter.send("hi", session_id="s1")
        assert server.state.received == []

    def test_metadata_on_earlier_events_is_harvested_with_the_content(self, server: Any) -> None:
        with _adapter(server) as adapter:
            reply = adapter.send("hi", session_id="s1")

        assert reply.route == "policy_agent" and reply.status.value == "answered"
        assert [c.doc_id for c in reply.citations] == ["HR-001"]
        assert [t.name for t in reply.tool_calls] == ["knowledge_search"]

    def test_an_unclassified_event_is_ignored_and_named_once(
        self, server: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with _adapter(server) as adapter:
            adapter.send("hi", session_id="s1")
            adapter.send("hi again", session_id="s1")
        assert sum("ignoring event 'typing'" in r.getMessage() for r in caplog.records) == 1


class TestConnections:
    def test_a_session_keeps_one_connection_and_its_server_side_memory(self, server: Any) -> None:
        with _adapter(server) as adapter:
            adapter.send("hello there", session_id="s1")
            recalled = adapter.send("remember", session_id="s1")
            other = adapter.send("remember", session_id="s2")

        assert "hello there" in recalled.reply
        assert "(nothing)" in other.reply, "another session must not see s1's history"
        sids = {sid for sid, message in server.state.received if message in ("hello there", "remember")}
        assert len(server.state.connects) == 2 == len(sids)

    def test_workers_share_the_adapter_and_never_cross_sessions(self, server: Any) -> None:
        results: dict[str, set[str]] = {}

        def work(name: str, adapter: SocketIOAdapter) -> None:
            results[name] = {adapter.send("who", session_id=name).reply for _ in range(3)}

        with _adapter(server) as adapter:
            threads = [threading.Thread(target=work, args=(f"s{i}", adapter)) for i in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        assert all(len(replies) == 1 for replies in results.values()), "one connection per session"
        assert len({next(iter(r)) for r in results.values()}) == 6
        assert len(server.state.connects) == 6

    def test_a_multi_turn_case_through_the_runner_does_not_reconnect_between_turns(
        self, server: Any
    ) -> None:
        case = TestCase(
            id="memory",
            turns=[
                Turn(user="my id is E-4471", evaluate=False),
                Turn(
                    user="remember",
                    expect=Expectations(required_facts=["first thing you said was: my id is E-4471"]),
                ),
            ],
        )
        adapter = _adapter(server, principal_headers={"test_user": {}})
        runner = TestRunner(adapter, EvaluationEngine([RuleBasedEvaluator()]), workers=1)
        report = runner.run([case])

        assert report.cases[0].passed, report.cases[0].reply
        assert len(server.state.connects) == 1

    def test_one_connection_is_one_identity(self, server: Any) -> None:
        cfg = {"principal_headers": {"default": {}, "other": {}}}
        with _adapter(server, **cfg) as adapter:
            adapter.send("hi", session_id="s1", principal="default")
            with pytest.raises(ConfigError, match="one connection is one identity"):
                adapter.send("hi", session_id="s1", principal="other")


class TestCompletion:
    def test_a_terminator_ends_the_reply_and_chunks_are_joined(self, server: Any) -> None:
        with _adapter(server) as adapter:
            assert adapter.send("stream", session_id="s1").reply == "alpha beta gamma"

    def test_with_no_terminator_the_reply_ends_when_it_goes_quiet(self) -> None:
        quiet = server_module.AgentServer(terminator=False)
        quiet.port = quiet.start()
        try:
            events = {**_config(quiet.port)["events"], "terminator": None}
            with SocketIOAdapter.from_config(
                _config(quiet.port, events=events, idle_timeout_s=0.5)
            ) as adapter:
                assert adapter.send("stream", session_id="s1").reply == "alpha beta gamma"
                assert adapter.send("hi", session_id="s1").reply == "You said: hi"
        finally:
            quiet.stop()

    def test_no_terminator_and_no_idle_timeout_is_refused_rather_than_guessed(self) -> None:
        events = {"send": "ask", "content": ["reply"]}
        with pytest.raises(ConfigError, match="no way to tell a reply is complete"):
            SocketIOAdapter.from_config(_config(1, events=events))

    def test_a_turn_that_runs_out_of_time_is_an_error_and_is_not_sent_again(self, server: Any) -> None:
        with _adapter(server, timeout_s=0.5) as adapter, pytest.raises(AgentCallError, match="not retried"):
            adapter.send("slow", session_id="s1")
        assert [m for _, m in server.state.received] == ["slow"]


class TestFailures:
    def test_an_error_event_fails_the_turn_and_leaves_the_session_usable(self, server: Any) -> None:
        with _adapter(server) as adapter:
            with pytest.raises(AgentCallError, match="oops.*upstream failed"):
                adapter.send("boom", session_id="s1")
            assert adapter.send("hi", session_id="s1").reply == "You said: hi"
        assert len(server.state.connects) == 1

    def test_a_disconnect_mid_run_loses_the_session_and_nothing_is_replayed(self, server: Any) -> None:
        with _adapter(server) as adapter:
            adapter.send("hello", session_id="s1")
            with pytest.raises(AgentCallError, match="not retried|may have been delivered"):
                adapter.send("drop", session_id="s1")
            # The next turn must not quietly start a new conversation on a new connection.
            with pytest.raises(AgentCallError, match="No reconnect was made"):
                adapter.send("remember", session_id="s1")
            # Other sessions are unaffected.
            assert adapter.send("hi", session_id="s2").reply == "You said: hi"

        assert [m for _, m in server.state.received].count("drop") == 1
        assert len(server.state.connects) == 2, "s1's connection and s2's; no reconnect for s1"

    def test_a_refused_connection_retries_with_fresh_credentials_and_sends_nothing(
        self, server: Any
    ) -> None:
        seen: list[bool] = []

        class Auth:
            def credentials(self, principal: str, *, refresh: bool = False) -> Credentials:
                seen.append(refresh)
                return Credentials()

        server.state.refuse_connects = 1
        adapter = SocketIOAdapter(
            f"http://127.0.0.1:{server.port}",
            events=sio_module.Events("ask", frozenset({"reply"}), terminator="done"),
            auth=Auth(),
            namespace="/chat",
            response_mapping={"text": "text"},
            max_attempts=2,
            backoff_s=0.0,
            sleep=lambda _: None,
        )
        with adapter:
            assert adapter.send("hi", session_id="s1").reply == "You said: hi"
        assert seen == [False, True]

    def test_exhausted_connection_attempts_fail_without_having_sent_anything(self, server: Any) -> None:
        server.state.refuse_connects = 5
        with _adapter(server, max_attempts=2, backoff_s=0.0) as adapter, pytest.raises(
            AgentCallError, match="after 2 attempt"
        ):
            adapter.send("hi", session_id="s1")
        assert server.state.received == []


class TestAuth:
    def test_a_static_token_is_presented_and_a_missing_one_is_refused(self) -> None:
        secured = server_module.AgentServer(require_auth=True)
        secured.port = secured.start()
        try:
            token = server_module.make_jwt({"sub": "demo-user", "exp": 9e12}, "example-secret")
            headers = {"default": {"Authorization": f"Bearer {token}"}}
            with SocketIOAdapter.from_config(_config(secured.port, principal_headers=headers)) as adapter:
                assert adapter.send("hi", session_id="s1").reply == "You said: hi"
            with SocketIOAdapter.from_config(_config(secured.port)) as adapter, pytest.raises(
                AgentCallError, match="refused"
            ):
                adapter.send("hi", session_id="s1")
        finally:
            secured.stop()

    def test_the_login_hook_runs_signin_and_otp_once_and_renews_an_expired_token_mid_run(self) -> None:
        clock = _Clock(1_000_000.0)
        secured = server_module.AgentServer(require_auth=True, token_ttl_s=100.0, clock=clock)
        secured.port = secured.start()
        try:
            hook = auth_module.OTPAuth(
                base_url=f"http://127.0.0.1:{secured.port}",
                users={"default": {"username": "demo-user", "password": "pw", "otp": "123456"}},
                clock=clock,
            )
            cfg = _config(secured.port)
            del cfg["principal_headers"]
            adapter = SocketIOAdapter(
                cfg["url"],
                events=sio_module.Events("ask", frozenset({"reply"}), terminator="done"),
                auth=hook,
                namespace="/chat",
                response_mapping={"text": "text"},
            )
            with adapter:
                assert adapter.send("hi", session_id="s1").reply == "You said: hi"
                adapter.send("again", session_id="s2")
                assert (secured.state.signins, secured.state.otp_verifications) == (1, 1)

                clock.now += 200  # the access token is now dead, on both sides
                assert adapter.send("later", session_id="s3").reply == "You said: later"
                # s1 was already connected and is unaffected by the token dying.
                assert adapter.send("still here", session_id="s1").reply == "You said: still here"
            assert secured.state.refreshes == 1
        finally:
            secured.stop()

    def test_a_hook_is_loaded_from_a_dotted_path_and_a_bad_one_is_refused(self) -> None:
        auth = load_auth({"hook": "test_adapters_socketio:_fake_auth", "options": {"label": "x"}})
        assert auth.credentials("p").headers == {"X-Label": "x"}
        with pytest.raises(ConfigError, match="could not be loaded"):
            load_auth({"hook": "no_such_module:factory"})
        with pytest.raises(ConfigError, match="package.module:factory"):
            load_auth({"hook": "nocolon"})

    def test_jwt_expiry_is_read_without_trusting_the_token(self) -> None:
        token = server_module.make_jwt({"exp": 1234.5}, "any")
        assert jwt_expiry(token) == 1234.5
        assert jwt_expiry("not-a-jwt") is None

    def test_a_cached_token_is_reused_until_it_is_about_to_expire(self) -> None:
        clock = _Clock(0.0)
        issued: list[str] = []

        class Hook(CachedTokenAuth):
            def login(self, principal: str) -> Token:
                issued.append(principal)
                return Token(f"t{len(issued)}", expires_at=clock.now + 100)

        hook = Hook(clock=clock)
        first = hook.credentials("p").headers["Authorization"]
        assert hook.credentials("p").headers["Authorization"] == first
        clock.now = 80  # inside the 30s skew
        assert hook.credentials("p").headers["Authorization"] != first
        assert hook.credentials("p", refresh=True).headers["Authorization"].endswith("t3")


class TestRateLimit:
    def test_windows_hold_a_request_until_every_one_has_room(self) -> None:
        clock = _Clock(0.0)
        starts: list[float] = []
        limiter = RateLimiter(
            [(2, 1.0), (3, 10.0)], clock=clock, sleep=lambda s: setattr(clock, "now", clock.now + s)
        )
        for _ in range(4):
            limiter.acquire()
            starts.append(clock.now)
        # Two at once; the third waits out the 1s window; the fourth the 10s one.
        assert starts == [0.0, 0.0, 1.0, 10.0]

    def test_the_adapter_waits_on_the_limiter_before_it_emits(self, server: Any) -> None:
        clock = _Clock(0.0)
        waits: list[float] = []

        def sleep(seconds: float) -> None:
            waits.append(seconds)
            clock.now += seconds

        limiter = RateLimiter([(2, 5.0)], clock=clock, sleep=sleep)
        adapter = SocketIOAdapter(
            f"http://127.0.0.1:{server.port}",
            events=sio_module.Events("ask", frozenset({"reply"}), terminator="done"),
            auth=sio_module.StaticAuth({"default": {}}),
            namespace="/chat",
            response_mapping={"text": "text"},
            limiter=limiter,
        )
        with adapter:
            for n in range(3):
                adapter.send(f"m{n}", session_id="s1")
        assert waits == [5.0], "the third turn waited for the window; the first two did not"


class TestConfiguration:
    def test_a_missing_library_is_an_install_hint_not_an_import_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "socketio", None)
        with pytest.raises(ConfigError, match=r"pip install -e \.\[socketio\]"):
            SocketIOAdapter.from_config(_config(1))

    def test_the_factory_builds_it_from_agent_config(self, server: Any) -> None:
        with build_adapter(AppConfig(raw={"agent": _config(server.port)})) as adapter:
            assert adapter.describe() == {
                "adapter": "socketio",
                "endpoint": f"http://127.0.0.1:{server.port}/chat",
            }

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"events": None}, "agent.events is required"),
            ({"events": {"send": "ask", "content": []}}, "at least one event"),
            (
                {"events": {"send": "a", "content": ["x"], "metadata": ["x"], "terminator": "d"}},
                "more than one role",
            ),
            ({"auth": {"hook": "a:b"}}, "not both"),
            ({"principal_headers": None}, "principal_headers is required"),
            ({"rate_limit": [{"requests": 0, "per_s": 1}]}, "requests >= 1"),
            ({"namespace": "chat"}, "must start with '/'"),
            ({"colour": "red"}, "unknown agent key"),
        ],
    )
    def test_bad_config_is_refused_by_name(self, override: dict[str, Any], message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            SocketIOAdapter.from_config(_config(1, **override))


class _Clock:
    """A clock a test moves by hand; also callable, as the code under test expects."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _fake_auth(label: str) -> Any:
    class Auth:
        def credentials(self, principal: str, *, refresh: bool = False) -> Credentials:
            return Credentials(headers={"X-Label": label})

    return Auth()
