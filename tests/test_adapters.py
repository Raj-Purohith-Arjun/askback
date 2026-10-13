"""The adapter seam, exercised through the in-process adapter."""

from __future__ import annotations

import ast
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from askback.adapters import AgentAdapter
from askback.adapters.base import AdapterLifecycle
from askback.adapters.factory import adapter_name, build_adapter
from askback.adapters.in_process import DEFAULT_PRINCIPALS, InProcessAdapter
from askback.adapters.scripted import ScriptedAdapter
from askback.core.config import PROJECT_ROOT, AppConfig
from askback.core.contracts import ChatRequest, ChatResponse, ResponseStatus, Role, ToolCall
from askback.core.errors import ConfigError
from askback.datasets.schema import TestCase, Turn
from askback.evaluation.base import Check
from askback.evaluation.safety_eval import SafetyEvaluator
from askback_sim.engine import ConversationEngine
from askback_sim.llm.prompts import PROMPT_VERSION

#: The HR simulator's config: the only shipped config that declares identities and an engine.
HR_CONFIG = PROJECT_ROOT / "examples" / "hr" / "config.yaml"


class RecordingEngine(ConversationEngine):
    """A real engine that remembers what it was asked and what it answered.

    A subclass rather than a patch: every turn still runs the whole pipeline.
    """

    def __init__(self, source: ConversationEngine) -> None:
        super().__init__(
            llm=source.llm,
            retriever=source.retriever,
            tools=source.tools,
            frontline=source.frontline,
            agents=source.agents,
            safety=source.safety,
            memory=source.memory,
            fallback_route=source.fallback_route,
        )
        self.requests: list[ChatRequest] = []
        self.responses: list[ChatResponse] = []

    def chat(self, request: ChatRequest | str, **kwargs: Any) -> ChatResponse:
        assert isinstance(request, ChatRequest)
        response = super().chat(request, **kwargs)
        self.requests.append(request)
        self.responses.append(response)
        return response


@pytest.fixture
def recording(engine: ConversationEngine) -> RecordingEngine:
    return RecordingEngine(engine)


class TestRoundTrip:
    def test_send_returns_the_engines_own_response(self, recording: RecordingEngine) -> None:
        with InProcessAdapter(recording) as adapter:
            response = adapter.send(
                "How many days of annual leave do I get?",
                session_id="golden:leave-1",
                principal="test_user",
            )

        assert response is recording.responses[0]
        assert response.status is ResponseStatus.ANSWERED
        assert "24 days" in response.reply

    def test_session_id_becomes_the_conversation_id(self, recording: RecordingEngine) -> None:
        with InProcessAdapter(recording) as adapter:
            response = adapter.send("hello", session_id="golden:hello-1")

        assert recording.requests[0].conversation_id == "golden:hello-1"
        assert response.conversation_id == "golden:hello-1"

    @pytest.mark.parametrize(
        ("principal", "user_id", "tenant_id"),
        [
            ("default", "test_user", "default"),
            ("test_user", "test_user", "default"),
            ("outsider", "outsider", "default"),
            ("acme_user", "acme_user", "acme"),
        ],
    )
    def test_principal_resolves_to_user_and_tenant(
        self, recording: RecordingEngine, principal: str, user_id: str, tenant_id: str
    ) -> None:
        with InProcessAdapter(recording) as adapter:
            adapter.send("hello", session_id="s", principal=principal)

        request = recording.requests[0]
        assert (request.user_id, request.tenant_id) == (user_id, tenant_id)

    def test_omitting_the_principal_uses_the_protocol_default(
        self, recording: RecordingEngine
    ) -> None:
        with InProcessAdapter(recording) as adapter:
            adapter.send("hello", session_id="s")

        request = recording.requests[0]
        assert (request.user_id, request.tenant_id) == ("test_user", "default")


class TestPrincipals:
    def test_unknown_principal_raises_and_never_reaches_the_engine(
        self, recording: RecordingEngine
    ) -> None:
        with InProcessAdapter(recording) as adapter, pytest.raises(ConfigError, match="nobody"):
            adapter.send("hello", session_id="s", principal="nobody")

        assert recording.requests == []
        assert recording.memory.history("s") == []

    def test_a_supplied_mapping_replaces_the_default_one(
        self, recording: RecordingEngine
    ) -> None:
        with InProcessAdapter(recording, {"hrbp": ("hrbp_1", "acme")}) as adapter:
            adapter.send("hello", session_id="s", principal="hrbp")
            with pytest.raises(ConfigError, match="test_user"):
                adapter.send("hello", session_id="s", principal="test_user")

        request = recording.requests[0]
        assert (request.user_id, request.tenant_id) == ("hrbp_1", "acme")

    @pytest.mark.parametrize(
        "identity",
        [
            "ab",  # unpacks into two strings, which is why the type is checked
            ("test_user",),
            ("test_user", "default", "extra"),
            ("test_user", 7),
            ["test_user", "default"],
        ],
    )
    def test_a_malformed_mapping_is_refused_at_construction(
        self, engine: ConversationEngine, identity: Any
    ) -> None:
        principals: dict[str, Any] = {"test_user": ("test_user", "default"), "broken": identity}
        with pytest.raises(ConfigError, match="broken"):
            InProcessAdapter(engine, principals)


class TestLifecycle:
    def test_satisfies_the_protocol(self, engine: ConversationEngine) -> None:
        assert isinstance(InProcessAdapter(engine), AgentAdapter)

    def test_send_outside_the_with_block_raises(self, recording: RecordingEngine) -> None:
        adapter = InProcessAdapter(recording)
        with pytest.raises(RuntimeError, match="send"):
            adapter.send("hello", session_id="s")

        with adapter:
            adapter.send("hello", session_id="s")

        with pytest.raises(RuntimeError, match="send"):
            adapter.send("hello", session_id="s")
        assert len(recording.requests) == 1

    def test_the_with_block_is_reentrant(self, recording: RecordingEngine) -> None:
        adapter = InProcessAdapter(recording)
        with adapter:
            with adapter:
                adapter.send("hello", session_id="s")
            # The inner exit must not close it under the outer block.
            adapter.send("hello", session_id="s")
            adapter.describe()

        with pytest.raises(RuntimeError, match="send"):
            adapter.send("hello", session_id="s")
        assert len(recording.requests) == 2

    def test_describe_outside_the_with_block_raises(self, engine: ConversationEngine) -> None:
        adapter = InProcessAdapter(engine)
        with pytest.raises(RuntimeError, match="describe"):
            adapter.describe()

        with adapter:
            adapter.describe()

        with pytest.raises(RuntimeError, match="describe"):
            adapter.describe()

    def test_describe_reports_the_engine_as_strings(self, engine: ConversationEngine) -> None:
        with InProcessAdapter(engine) as adapter:
            info = adapter.describe()

        assert info == {
            "adapter": "in_process",
            "llm_provider": "extractive",
            "retriever": engine.retriever.name,
            "corpus_chunks": str(len(engine.retriever)),
            "prompt_version": PROMPT_VERSION,
            "agents": "fallback_agent,policy_agent,smalltalk_agent",
        }


class TestSharedLifecycle:
    def test_both_adapters_get_it_from_the_one_mixin(self, engine: ConversationEngine) -> None:
        assert isinstance(InProcessAdapter(engine), AdapterLifecycle)
        assert isinstance(ScriptedAdapter(replies={}), AdapterLifecycle)

    def test_the_error_names_the_concrete_adapter(self) -> None:
        with pytest.raises(RuntimeError, match=r"ScriptedAdapter\.send\(\)"):
            ScriptedAdapter(replies={}).send("hello", session_id="s")

    def test_a_stray_exit_cannot_drive_the_depth_negative(self) -> None:
        adapter = ScriptedAdapter(replies={}, default=_answer("ok"))
        adapter.__exit__(None, None, None)
        with adapter:
            adapter.send("hello", session_id="s")


class TestConcurrency:
    def test_two_sessions_on_two_threads_keep_separate_histories(
        self, engine: ConversationEngine
    ) -> None:
        turns = 5
        sessions = {"golden:a": "alpha", "golden:b": "bravo"}
        # Both workers are parked here until the other arrives, so the turns
        # genuinely overlap instead of running one session after the other.
        start = threading.Barrier(len(sessions))

        def talk(adapter: InProcessAdapter, session_id: str, word: str) -> None:
            start.wait(timeout=5)
            for n in range(turns):
                response = adapter.send(f"hello {word} {n}", session_id=session_id)
                assert response.conversation_id == session_id

        with InProcessAdapter(engine) as adapter, ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(talk, adapter, session_id, word)
                for session_id, word in sessions.items()
            ]
            for future in futures:
                future.result(timeout=30)  # re-raises anything a worker hit

        for session_id, word in sessions.items():
            history = engine.memory.history(session_id)
            said = [m.content for m in history if m.role is Role.USER]
            assert said == [f"hello {word} {n}" for n in range(turns)]
            assert len(history) == 2 * turns


def _answer(text: str) -> ChatResponse:
    return ChatResponse(reply=text, conversation_id="scripted")


class TestScriptedAdapter:
    def test_replies_are_returned_by_exact_message(self) -> None:
        hit = _answer("24 days")
        with ScriptedAdapter(replies={"leave?": hit}, default=_answer("?")) as adapter:
            assert adapter.send("leave?", session_id="s") == hit
            # Exact means exact: no case folding, no trimming.
            assert adapter.send("Leave?", session_id="s").reply == "?"

    def test_responder_receives_prior_messages_in_order(self) -> None:
        seen: list[tuple[str, list[str]]] = []

        def responder(message: str, prior: list[str]) -> ChatResponse:
            seen.append((message, prior))
            return _answer(f"turn {len(prior) + 1}")

        with ScriptedAdapter(responder=responder) as adapter:
            adapter.send("one", session_id="a")
            adapter.send("two", session_id="a")
            adapter.send("other", session_id="b")
            adapter.send("three", session_id="a")

        assert seen == [
            ("one", []),
            ("two", ["one"]),
            ("other", []),
            ("three", ["one", "two"]),
        ]

    def test_responder_cannot_rewrite_the_history_it_is_given(self) -> None:
        seen: list[list[str]] = []

        def responder(message: str, prior: list[str]) -> ChatResponse:
            seen.append(list(prior))  # snapshot before tampering
            prior.append("tampered")
            return _answer("ok")

        with ScriptedAdapter(responder=responder) as adapter:
            adapter.send("one", session_id="s")
            adapter.send("two", session_id="s")

        assert seen == [[], ["one"]]

    def test_an_unmatched_message_gets_the_default(self) -> None:
        fallback = _answer("I don't know")
        with ScriptedAdapter(replies={}, default=fallback) as adapter:
            assert adapter.send("anything", session_id="s") == fallback

    def test_a_mutated_reply_does_not_poison_the_next_call(self) -> None:
        hit, fallback = _answer("24 days"), _answer("I don't know")
        with ScriptedAdapter(replies={"leave?": hit}, default=fallback) as adapter:
            first = adapter.send("leave?", session_id="a")
            first.reply = "tampered"
            # Nested state too: a shallow copy would still share this list.
            first.facts.append("invented")
            adapter.send("unscripted", session_id="a").facts.append("invented")

            again = adapter.send("leave?", session_id="b")
            assert again is not first
            assert (again.reply, again.facts) == ("24 days", [])
            assert adapter.send("unscripted", session_id="b").facts == []

        assert (hit.reply, hit.facts, fallback.facts) == ("24 days", [], [])

    def test_an_unmatched_message_without_a_default_raises(self) -> None:
        with (
            ScriptedAdapter(replies={"known": _answer("x")}) as adapter,
            pytest.raises(ConfigError, match="unscripted"),
        ):
            adapter.send("unscripted", session_id="s")

    def test_raises_propagates_and_wins_over_the_script(self) -> None:
        boom = TimeoutError("upstream timed out")
        adapter = ScriptedAdapter(replies={"slow": _answer("never seen")}, raises={"slow": boom})
        with adapter, pytest.raises(TimeoutError) as caught:
            adapter.send("slow", session_id="s")

        assert caught.value is boom

    def test_a_failed_turn_is_recorded_but_not_remembered(self) -> None:
        seen: list[list[str]] = []

        def responder(message: str, prior: list[str]) -> ChatResponse:
            seen.append(prior)
            return _answer("ok")

        with ScriptedAdapter(responder=responder, raises={"bad": ValueError("x")}) as adapter:
            with pytest.raises(ValueError, match="x"):
                adapter.send("bad", session_id="s")
            adapter.send("next", session_id="s")

        assert adapter.calls == [("bad", "s", "default"), ("next", "s", "default")]
        assert seen == [[]]

    def test_calls_record_the_principal_verbatim(self) -> None:
        with ScriptedAdapter(replies={}, default=_answer("ok")) as adapter:
            adapter.send("a", session_id="s1", principal="outsider")
            adapter.send("b", session_id="s2", principal="not-a-known-identity")
            adapter.send("c", session_id="s2")

        assert adapter.calls == [
            ("a", "s1", "outsider"),
            ("b", "s2", "not-a-known-identity"),
            ("c", "s2", "default"),
        ]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"replies": {}, "responder": lambda message, prior: _answer("x")},
        ],
    )
    def test_exactly_one_of_replies_or_responder_is_required(
        self, kwargs: dict[str, Any]
    ) -> None:
        with pytest.raises(ConfigError, match="exactly one"):
            ScriptedAdapter(**kwargs)

    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(ScriptedAdapter(replies={}), AgentAdapter)

    def test_send_and_describe_outside_the_with_block_raise(self) -> None:
        adapter = ScriptedAdapter(replies={}, default=_answer("ok"))
        with pytest.raises(RuntimeError, match="send"):
            adapter.send("hello", session_id="s")
        with pytest.raises(RuntimeError, match="describe"):
            adapter.describe()

        with adapter:
            adapter.send("hello", session_id="s")
            adapter.describe()

        with pytest.raises(RuntimeError, match="send"):
            adapter.send("hello", session_id="s")
        assert len(adapter.calls) == 1

    def test_the_with_block_is_reentrant(self) -> None:
        adapter = ScriptedAdapter(replies={}, default=_answer("ok"))
        with adapter:
            with adapter:
                adapter.send("hello", session_id="s")
            # The inner exit must not close it under the outer block.
            adapter.send("hello", session_id="s")

        with pytest.raises(RuntimeError, match="send"):
            adapter.send("hello", session_id="s")

    def test_describe_names_only_the_adapter(self) -> None:
        with ScriptedAdapter(replies={}) as adapter:
            assert adapter.describe() == {"adapter": "scripted"}

    def test_it_imports_nothing_from_the_simulator(self) -> None:
        """This adapter is what the framework's own tests run on with no simulator.

        tests/test_boundaries.py holds the whole framework to this; the check
        stays here too because this is the one adapter that must never need it.
        """
        source = Path(__file__).parent.parent / "src" / "askback" / "adapters" / "scripted.py"
        offenders: list[str] = []
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import stays inside askback, so only an absolute
                # one can name the simulator.
                modules = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for module in modules:
                if module.split(".")[0] == "askback_sim":
                    offenders.append(f"line {node.lineno}: imports {module}")

        assert not offenders, f"scripted.py imports from the simulator: {offenders}"


class TestBuildAdapter:
    @staticmethod
    def _config(agent: Any) -> AppConfig:
        return AppConfig(raw={"agent": agent})

    def test_a_config_with_no_agent_section_gets_the_simulator(
        self, engine: ConversationEngine
    ) -> None:
        adapter = build_adapter(AppConfig(), engine=engine)

        assert isinstance(adapter, InProcessAdapter)
        with adapter:
            assert adapter.describe()["adapter"] == "in_process"

    def test_the_supplied_engine_is_the_one_driven(self, recording: RecordingEngine) -> None:
        with build_adapter(self._config({"adapter": "in_process"}), engine=recording) as adapter:
            adapter.send("hello", session_id="s")

        assert len(recording.requests) == 1

    def test_without_an_engine_in_process_is_refused_not_built(self) -> None:
        """The factory does not import the simulator to build one: the caller passes it."""
        with pytest.raises(ConfigError, match=r"engine=.*examples/hr"):
            build_adapter(AppConfig.load(HR_CONFIG))

    def test_adapter_name_tells_a_caller_whether_an_engine_is_needed(self) -> None:
        assert adapter_name(AppConfig()) == "in_process"
        assert adapter_name(self._config({"adapter": "http"})) == "http"
        with pytest.raises(ConfigError, match="agent"):
            adapter_name(self._config("in_process"))

    def test_an_unknown_adapter_is_refused_naming_the_valid_ones(
        self, engine: ConversationEngine
    ) -> None:
        with pytest.raises(ConfigError, match="'carrier_pigeon'.*in_process"):
            build_adapter(self._config({"adapter": "carrier_pigeon"}), engine=engine)

    def test_an_agent_section_that_is_not_a_mapping_is_refused(
        self, engine: ConversationEngine
    ) -> None:
        with pytest.raises(ConfigError, match="agent"):
            build_adapter(self._config("in_process"), engine=engine)

    def test_configured_principals_replace_the_default_ones(
        self, recording: RecordingEngine
    ) -> None:
        config = self._config(
            {"principals": {"hrbp": {"user_id": "hrbp_1", "tenant_id": "acme"}}}
        )
        with build_adapter(config, engine=recording) as adapter:
            adapter.send("hello", session_id="s", principal="hrbp")
            with pytest.raises(ConfigError, match="test_user"):
                adapter.send("hello", session_id="s", principal="test_user")

        request = recording.requests[0]
        assert (request.user_id, request.tenant_id) == ("hrbp_1", "acme")

    @pytest.mark.parametrize(
        "principals",
        [
            ["broken"],
            {"broken": ["test_user", "default"]},
            {"broken": {"user_id": "test_user"}},
            {"broken": {"user_id": "test_user", "tenant_id": 7}},
            {"broken": {"user_id": "test_user", "tenant_id": "default", "role": "hrbp"}},
        ],
    )
    def test_malformed_principals_are_refused_before_anything_is_sent(
        self, recording: RecordingEngine, principals: Any
    ) -> None:
        with pytest.raises(ConfigError, match="broken"):
            build_adapter(self._config({"principals": principals}), engine=recording)

        assert recording.requests == []

    def test_the_shipped_config_declares_the_identities_suites_have_always_run_as(
        self, recording: RecordingEngine
    ) -> None:
        """Moving the mapping into the config file must not change who any case runs as."""
        with build_adapter(AppConfig.load(HR_CONFIG), engine=recording) as adapter:
            for label in DEFAULT_PRINCIPALS:
                adapter.send("hello", session_id=f"s:{label}", principal=label)

        sent_as = {
            label: (request.user_id, request.tenant_id)
            for label, request in zip(DEFAULT_PRINCIPALS, recording.requests, strict=True)
        }
        assert sent_as == dict(DEFAULT_PRINCIPALS)


class TestSideEffectingToolsFromConfig:
    """`agent.side_effecting_tools` is how a run without the simulator declares its tools."""

    @pytest.fixture(autouse=True)
    def no_other_declarations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The simulator declared its registry when conftest imported it. An HTTP
        # run has no such source, and that is the run this key exists for.
        monkeypatch.setattr("askback.evaluation.safety_eval._SIDE_EFFECT_SOURCES", [])

    @staticmethod
    def _check(agent: dict[str, Any], engine: ConversationEngine, *tools: str) -> Check:
        build_adapter(AppConfig(raw={"agent": agent}), engine=engine)
        response = ChatResponse(
            reply="done",
            conversation_id="s",
            status=ResponseStatus.ANSWERED,
            tool_calls=[ToolCall(name=name) for name in tools],
        )
        case = TestCase(id="t1", turns=[Turn(user="q")])
        result = SafetyEvaluator().evaluate(case, case.turns[0], response)
        return next(c for c in result.checks if c.name == "tool_side_effects")

    def test_unset_declares_nothing_so_the_check_still_fails(
        self, engine: ConversationEngine
    ) -> None:
        check = self._check({"adapter": "in_process"}, engine)

        assert not check.passed
        assert "declar" in check.detail

    def test_a_declared_tool_is_caught_and_an_undeclared_one_is_not(
        self, engine: ConversationEngine
    ) -> None:
        agent = {"side_effecting_tools": ["wire_transfer"]}

        assert self._check(agent, engine, "knowledge_search").passed
        fired = self._check(agent, engine, "wire_transfer")
        assert not fired.passed
        assert fired.actual == ["wire_transfer"]

    def test_an_empty_list_is_a_declaration_and_a_bare_string_is_refused(
        self, engine: ConversationEngine
    ) -> None:
        assert self._check({"side_effecting_tools": []}, engine).passed
        with pytest.raises(ConfigError, match="side_effecting_tools"):
            self._check({"side_effecting_tools": "wire_transfer"}, engine)
