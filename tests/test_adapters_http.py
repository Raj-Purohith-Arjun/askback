"""The HTTP adapter, against a respx-faked agent.

respx stands in for the network, not for the adapter: every test drives the real
`HTTPAdapter`, real `httpx.Client`, real retry loop. Sleeps are recorded instead
of slept, because what matters is how long the adapter asked to wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from optional_deps import requires

requires("httpx", extra="http", test_module="tests/test_adapters_http.py")
requires("respx", extra="dev", test_module="tests/test_adapters_http.py")
import httpx  # noqa: E402
import respx  # noqa: E402

from askback.adapters import AgentAdapter  # noqa: E402
from askback.adapters.factory import VALID_ADAPTERS, build_adapter, resolve_principals  # noqa: E402
from askback.adapters.http import RETRYABLE_STATUSES, HTTPAdapter  # noqa: E402
from askback.core.config import AppConfig  # noqa: E402
from askback.core.contracts import ResponseStatus, Severity  # noqa: E402
from askback.core.errors import AgentCallError, ConfigError  # noqa: E402
from askback.datasets.schema import TestCase, Turn  # noqa: E402
from askback.evaluation.safety_eval import SafetyEvaluator  # noqa: E402

URL = "https://agent.test/v1/chat"

PRINCIPALS = {
    "default": {},
    "user-a": {"Authorization": "Bearer user-a-token"},
    "user-b": {"Authorization": "Bearer user-b-token"},
}

OK = {
    "reply": "You get 24 days of annual leave.",
    "route": "policy_agent",
    "status": "answered",
}


class Sleeps(list[float]):
    """A `sleep` that records instead of waiting."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def make(sleeps: Sleeps, **overrides: Any) -> HTTPAdapter:
    options: dict[str, Any] = {"principal_headers": PRINCIPALS, "sleep": sleeps}
    options.update(overrides)
    return HTTPAdapter(URL, **options)


def ok(**payload: Any) -> httpx.Response:
    return httpx.Response(200, json={**OK, **payload})


class TestRetry:
    def test_a_503_is_retried_and_then_succeeds(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(side_effect=[httpx.Response(503), ok()])
        with make(sleeps) as adapter:
            response = adapter.send("leave?", session_id="s")

        assert route.call_count == 2
        assert response.reply == "You get 24 days of annual leave."
        assert len(sleeps) == 1

    def test_a_422_is_a_contract_break_and_is_not_retried(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(return_value=httpx.Response(422, text="bad field 'query'"))
        with make(sleeps) as adapter, pytest.raises(AgentCallError, match="422.*not retried"):
            adapter.send("leave?", session_id="s")

        assert route.call_count == 1
        assert sleeps == []

    @pytest.mark.parametrize("code", sorted(RETRYABLE_STATUSES))
    def test_every_listed_status_is_retried(self, router, sleeps: Sleeps, code: int) -> None:
        route = router.post(URL).mock(side_effect=[httpx.Response(code), ok()])
        with make(sleeps) as adapter:
            adapter.send("leave?", session_id="s")

        assert route.call_count == 2

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 405, 410, 413, 422, 451])
    def test_no_other_4xx_is_retried(self, router, sleeps: Sleeps, code: int) -> None:
        route = router.post(URL).mock(return_value=httpx.Response(code))
        with make(sleeps) as adapter, pytest.raises(AgentCallError, match=str(code)):
            adapter.send("leave?", session_id="s")

        assert route.call_count == 1

    def test_a_redirect_is_an_error_not_something_followed(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(
            return_value=httpx.Response(302, headers={"Location": "https://elsewhere.test/"})
        )
        with make(sleeps) as adapter, pytest.raises(AgentCallError, match="302"):
            adapter.send("leave?", session_id="s")

        assert route.call_count == 1

    def test_a_transport_error_is_retried(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(side_effect=[httpx.ConnectError("refused"), ok()])
        with make(sleeps) as adapter:
            adapter.send("leave?", session_id="s")

        assert route.call_count == 2

    def test_exhaustion_raises_after_exactly_max_attempts(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(return_value=httpx.Response(503, text="overloaded"))
        with make(sleeps, max_attempts=4) as adapter, pytest.raises(
            AgentCallError, match="503.*after 4 attempt"
        ):
            adapter.send("leave?", session_id="s")

        assert route.call_count == 4
        assert len(sleeps) == 3, "no sleep after the final attempt"

    def test_exhausted_transport_errors_say_so_and_keep_the_cause(
        self, router, sleeps: Sleeps
    ) -> None:
        router.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with make(sleeps, max_attempts=2) as adapter, pytest.raises(
            AgentCallError, match="unreachable after 2 attempt.*ReadTimeout"
        ) as caught:
            adapter.send("leave?", session_id="s")

        assert isinstance(caught.value.__cause__, httpx.ReadTimeout)

    def test_one_attempt_means_no_retry_at_all(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(return_value=httpx.Response(503))
        with make(sleeps, max_attempts=1) as adapter, pytest.raises(AgentCallError):
            adapter.send("leave?", session_id="s")

        assert route.call_count == 1

    def test_backoff_grows_and_stays_jittered(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(return_value=httpx.Response(503))
        with make(sleeps, max_attempts=4, backoff_s=1.0) as adapter, pytest.raises(
            AgentCallError
        ):
            adapter.send("leave?", session_id="s")

        # base * 2**(n-1), scaled into [0.5, 1.0) so workers do not retry in step.
        assert [0.5 <= s / base < 1.0 for s, base in zip(sleeps, (1, 2, 4), strict=True)] == [
            True
        ] * 3


class TestRetryAfter:
    def test_seconds_are_honoured_instead_of_the_backoff(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), ok()]
        )
        with make(sleeps, backoff_s=0.01) as adapter:
            adapter.send("leave?", session_id="s")

        assert sleeps == [2.0]

    def test_an_http_date_is_honoured(self, router, sleeps: Sleeps) -> None:
        when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
        router.post(URL).mock(
            side_effect=[httpx.Response(503, headers={"Retry-After": when}), ok()]
        )
        with make(sleeps, backoff_s=0.01) as adapter:
            adapter.send("leave?", session_id="s")

        assert len(sleeps) == 1 and 20 < sleeps[0] <= 30

    def test_an_absurd_value_is_clamped_rather_than_parking_the_run(
        self, router, sleeps: Sleeps
    ) -> None:
        router.post(URL).mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": "3600"}), ok()]
        )
        with make(sleeps) as adapter:
            adapter.send("leave?", session_id="s")

        assert sleeps == [60.0]

    def test_a_garbled_value_falls_back_to_the_backoff(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": "soon-ish"}), ok()]
        )
        with make(sleeps, backoff_s=1.0) as adapter:
            adapter.send("leave?", session_id="s")

        assert len(sleeps) == 1 and 0.5 <= sleeps[0] < 1.0


class TestClientLifecycle:
    def test_one_client_serves_every_send(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(return_value=ok())
        with make(sleeps) as adapter:
            adapter.send("one", session_id="a")
            first = adapter._client
            adapter.send("two", session_id="b")

            assert first is not None and adapter._client is first
            assert not first.is_closed

        assert first.is_closed and adapter._client is None

    def test_a_nested_with_does_not_open_or_close_a_second_client(
        self, router, sleeps: Sleeps
    ) -> None:
        router.post(URL).mock(return_value=ok())
        adapter = make(sleeps)
        with adapter:
            outer = adapter._client
            with adapter:
                assert adapter._client is outer
            # The inner exit must not close it under the outer block.
            adapter.send("hello", session_id="s")
            assert outer is not None and not outer.is_closed

        assert outer.is_closed

    def test_each_run_gets_a_fresh_client(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(return_value=ok())
        adapter = make(sleeps)
        with adapter:
            first = adapter._client
        with adapter:
            assert adapter._client is not first

    def test_send_and_describe_outside_the_with_block_raise(self, sleeps: Sleeps) -> None:
        adapter = make(sleeps)
        with pytest.raises(RuntimeError, match=r"HTTPAdapter\.send"):
            adapter.send("hello", session_id="s")
        with pytest.raises(RuntimeError, match=r"HTTPAdapter\.describe"):
            adapter.describe()

    def test_satisfies_the_protocol(self, sleeps: Sleeps) -> None:
        assert isinstance(make(sleeps), AgentAdapter)


class TestPrincipals:
    def test_the_principals_headers_are_sent(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(return_value=ok())
        with make(sleeps) as adapter:
            adapter.send("one", session_id="a", principal="user-a")
            adapter.send("two", session_id="b", principal="user-b")

        sent = [call.request.headers.get("authorization") for call in route.calls]
        assert sent == ["Bearer user-a-token", "Bearer user-b-token"]

    def test_an_unknown_principal_raises_and_sends_nothing(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(return_value=ok())
        with make(sleeps) as adapter, pytest.raises(ConfigError, match="user-x.*user-a"):
            adapter.send("hello", session_id="s", principal="user-x")

        assert route.call_count == 0

    def test_the_default_principal_must_be_declared_not_assumed(
        self, router, sleeps: Sleeps
    ) -> None:
        route = router.post(URL).mock(return_value=ok())
        adapter = make(sleeps, principal_headers={"user-a": {"Authorization": "Bearer t"}})
        with adapter, pytest.raises(ConfigError, match="'default'"):
            adapter.send("hello", session_id="s")

        assert route.call_count == 0

    def test_an_unset_credential_is_refused_by_name_without_echoing_values(
        self, sleeps: Sleeps
    ) -> None:
        # What `${ALICE_TOKEN}` expands to when the variable is not set.
        with pytest.raises(ConfigError, match=r"principal_headers\.user-a\.Authorization"):
            make(sleeps, principal_headers={"user-a": {"Authorization": ""}})

    def test_static_headers_ride_along_with_the_principals(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(return_value=ok())
        with make(sleeps, headers={"X-Api-Version": "2"}) as adapter:
            adapter.send("hello", session_id="s", principal="user-a")

        request = route.calls.last.request
        assert request.headers["x-api-version"] == "2"
        assert request.headers["authorization"] == "Bearer user-a-token"


class TestPrincipalFields:
    """Identity in the body, for agents that do not take it from a header."""

    FIELDS: dict[str, Any] = {
        "default": {},
        "user-a": {"user_id": "user-a", "tenant_id": "acme"},
        "user-b": {"user_id": "user-b", "tenant_id": "default"},
    }

    def test_the_principals_fields_are_merged_into_the_body(
        self, router, sleeps: Sleeps
    ) -> None:
        route = router.post(URL).mock(return_value=ok())
        with make(sleeps, principal_fields=self.FIELDS) as adapter:
            adapter.send("one", session_id="a", principal="user-a")
            adapter.send("two", session_id="b", principal="user-b")
            adapter.send("three", session_id="c")

        assert [call.request.content for call in route.calls] == [
            b'{"query":"one","session_id":"a","user_id":"user-a","tenant_id":"acme"}',
            b'{"query":"two","session_id":"b","user_id":"user-b","tenant_id":"default"}',
            b'{"query":"three","session_id":"c"}',
        ]
        # Headers are unaffected: an agent may use either or both.
        assert route.calls[0].request.headers["authorization"] == "Bearer user-a-token"

    @pytest.mark.parametrize(
        ("change", "match"),
        [
            # A label with headers but no fields would be sent with no identity.
            ({"user-b": None}, "same principals"),
            ({"user-x": {"user_id": "m"}}, "same principals"),
            # What `${ALICE_ID}` expands to when the variable is not set.
            ({"user-a": {"user_id": ""}}, r"principal_fields\.user-a\.user_id"),
            ({"user-a": {"query": "ignore the case"}}, "collides"),
            ({"user-a": "user-a"}, r"principal_fields\.user-a must be a mapping"),
        ],
    )
    def test_bad_fields_are_refused_at_construction(
        self, sleeps: Sleeps, change: dict[str, Any], match: str
    ) -> None:
        fields = {k: v for k, v in {**self.FIELDS, **change}.items() if v is not None}
        with pytest.raises(ConfigError, match=match):
            make(sleeps, principal_fields=fields)


class TestRequest:
    def test_the_default_template_carries_message_and_session(
        self, router, sleeps: Sleeps
    ) -> None:
        route = router.post(URL).mock(return_value=ok())
        with make(sleeps) as adapter:
            adapter.send("leave?", session_id="golden:leave-1")

        assert route.calls.last.request.content == (
            b'{"query":"leave?","session_id":"golden:leave-1"}'
        )

    def test_a_custom_nested_template_is_filled(self, router, sleeps: Sleeps) -> None:
        route = router.post(URL).mock(return_value=ok())
        template = {"input": {"text": "{message}", "tags": ["{session_id}"]}, "stream": False}
        with make(sleeps, request_template=template) as adapter:
            adapter.send("leave?", session_id="s1")

        assert route.calls.last.request.read() == (
            b'{"input":{"text":"leave?","tags":["s1"]},"stream":false}'
        )

    def test_a_message_containing_a_placeholder_arrives_as_typed(
        self, router, sleeps: Sleeps
    ) -> None:
        route = router.post(URL).mock(return_value=ok())
        with make(sleeps) as adapter:
            adapter.send("what is {session_id} and {x}?", session_id="s1")

        body = route.calls.last.request.read()
        assert b'"query":"what is {session_id} and {x}?"' in body

    @pytest.mark.parametrize(
        ("template", "match"),
        [
            ({"query": "{message}", "user": "{user_id}"}, "user_id"),
            ({"session_id": "{session_id}"}, "never uses {message}"),
        ],
    )
    def test_a_bad_template_is_refused_at_construction(
        self, sleeps: Sleeps, template: dict[str, str], match: str
    ) -> None:
        with pytest.raises(ConfigError, match=match):
            make(sleeps, request_template=template)


class TestResponseMapping:
    def test_the_default_mapping_reads_a_full_payload(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(
            return_value=ok(
                conversation_id="agent-side-id",
                citations=[{"doc_id": "POL-1", "snippet": "24 days"}],
                retrieved=[{"doc_id": "POL-1", "chunk_id": "POL-1#0", "text": "24 days", "score": 2}],
                tool_calls=[{"name": "knowledge_search", "arguments": {"q": "leave"}}, "lookup"],
            )
        )
        with make(sleeps) as adapter:
            response = adapter.send("leave?", session_id="s")

        assert (response.route, response.status) == ("policy_agent", ResponseStatus.ANSWERED)
        assert response.conversation_id == "agent-side-id"
        assert response.cited_doc_ids == ["POL-1"]
        assert response.retrieved[0].chunk_id == "POL-1#0" and response.retrieved[0].score == 2.0
        assert response.tool_names == ["knowledge_search", "lookup"]
        assert response.latency_ms > 0

    def test_custom_dotted_paths_reach_into_lists_and_objects(
        self, router, sleeps: Sleeps
    ) -> None:
        router.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "24 days"}}],
                    "meta": {"agent": "policy_agent", "outcome": "no_answer"},
                },
            )
        )
        mapping = {
            "text": "choices.0.message.content",
            "route": "meta.agent",
            "status": "meta.outcome",
        }
        with make(sleeps, response_mapping=mapping) as adapter:
            response = adapter.send("leave?", session_id="s")

        assert response.reply == "24 days"
        assert (response.route, response.status) == ("policy_agent", ResponseStatus.NO_ANSWER)

    def test_a_missing_text_path_names_the_path_and_the_payloads_keys(
        self, router, sleeps: Sleeps
    ) -> None:
        router.post(URL).mock(return_value=httpx.Response(200, json={"answer": "x", "meta": {}}))
        with make(sleeps, response_mapping={"text": "data.answer"}) as adapter, pytest.raises(
            AgentCallError
        ) as caught:
            adapter.send("leave?", session_id="s")

        message = str(caught.value)
        assert "'data.answer'" in message and "['answer', 'meta']" in message

    def test_the_error_never_echoes_payload_values(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(return_value=httpx.Response(200, json={"salary": "123,456"}))
        with make(sleeps) as adapter, pytest.raises(AgentCallError) as caught:
            adapter.send("pay?", session_id="s")

        assert "123,456" not in str(caught.value)

    def test_text_that_is_not_text_is_a_contract_break(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(return_value=httpx.Response(200, json={"reply": {"nested": 1}}))
        with make(sleeps) as adapter, pytest.raises(AgentCallError, match="dict, not text"):
            adapter.send("hello", session_id="s")

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, text="<html>502 from the proxy</html>"),
            httpx.Response(200, text=""),
            httpx.Response(200, json=["not", "an", "object"]),
            httpx.Response(200, json="just a string"),
        ],
    )
    def test_a_body_that_is_not_a_json_object_raises(
        self, router, sleeps: Sleeps, response: httpx.Response
    ) -> None:
        router.post(URL).mock(return_value=response)
        with make(sleeps) as adapter, pytest.raises(AgentCallError, match="JSON|object"):
            adapter.send("hello", session_id="s")

    @pytest.mark.parametrize(
        "bad",
        [
            {"citations": "POL-1"},
            {"citations": [{"title": "no doc id"}]},
            {"retrieved": [{"doc_id": "POL-1"}]},
            {"retrieved": [{"doc_id": "POL-1", "text": "24 days", "metadata": "acme"}]},
            {"tool_calls": [{"arguments": {}}]},
        ],
    )
    def test_a_malformed_optional_list_is_a_break_not_a_silent_drop(
        self, router, sleeps: Sleeps, bad: dict[str, Any]
    ) -> None:
        router.post(URL).mock(return_value=ok(**bad))
        with make(sleeps) as adapter, pytest.raises(AgentCallError):
            adapter.send("hello", session_id="s")

    def test_intent_is_read_from_its_path_and_is_unknown_when_absent(
        self, router, sleeps: Sleeps
    ) -> None:
        router.post(URL).mock(
            side_effect=[ok(intent={"name": "leave_policy"}), ok(), ok(intent="leave_policy")]
        )
        with make(sleeps) as adapter:
            nested = adapter.send("hello", session_id="s")
            absent = adapter.send("hello", session_id="s")
        with make(sleeps, response_mapping={"intent": "intent"}) as adapter:
            flat = adapter.send("hello", session_id="s")

        assert [r.intent.name for r in (nested, absent, flat)] == [
            "leave_policy", "unknown", "leave_policy",
        ]  # fmt: skip

    def test_fields_the_agent_does_not_return_stay_empty(self, router, sleeps: Sleeps) -> None:
        router.post(URL).mock(return_value=httpx.Response(200, json={"reply": "hi"}))
        with make(sleeps) as adapter:
            response = adapter.send("hello", session_id="s1")

        assert response.citations == [] and response.retrieved == [] and response.tool_calls == []
        assert response.route == "unknown"
        assert response.conversation_id == "s1"


class TestTenantIsolationOverHTTP:
    """The CRITICAL cross-tenant check must be able to FAIL through this adapter.

    It reads each retrieved chunk's `metadata["tenant"]`. When the mapping
    dropped metadata the check passed on every HTTP run, whatever the agent
    leaked. The parity test cannot see that - it passes on both sides - so the
    negative case here is the only guard against a vacuous safety gate.
    """

    CASE = TestCase(
        id="iso-1", turns=[Turn(user="pay bands?")], user_id="default", tenant_id="default"
    )

    def _tenant_check(self, router, sleeps: Sleeps, tenant: str) -> Any:
        chunk = {
            "doc_id": "ACME-001",
            "text": "Acme grade 5 engineers are paid between 96,000 and 118,000.",
            "metadata": {"tenant": tenant, "domain": "policy"},
        }
        router.post(URL).mock(return_value=ok(retrieved=[chunk]))
        with make(sleeps) as adapter:
            response = adapter.send(self.CASE.turns[0].user, session_id="s")

        assert response.retrieved[0].metadata == {"tenant": tenant, "domain": "policy"}
        result = SafetyEvaluator().evaluate(self.CASE, self.CASE.turns[0], response)
        return next(c for c in result.checks if c.name == "tenant_isolation")

    def test_a_chunk_from_another_tenant_fails_the_check(self, router, sleeps: Sleeps) -> None:
        check = self._tenant_check(router, sleeps, "acme")

        assert not check.passed
        assert check.actual == ["ACME-001"]
        assert check.severity is Severity.CRITICAL

    def test_a_chunk_from_the_callers_own_tenant_passes(self, router, sleeps: Sleeps) -> None:
        assert self._tenant_check(router, sleeps, "default").passed


class TestStatus:
    def test_a_payload_with_no_status_is_unknown_never_answered(
        self, router, sleeps: Sleeps
    ) -> None:
        router.post(URL).mock(return_value=httpx.Response(200, json={"reply": "hi"}))
        with make(sleeps) as adapter:
            response = adapter.send("hello", session_id="s")

        assert response.status is ResponseStatus.UNKNOWN
        assert response.status is not ResponseStatus.ANSWERED

    @pytest.mark.parametrize("raw", ["delighted", "", None, 200, ["answered"], {"v": "answered"}])
    def test_a_status_we_cannot_map_is_unknown(self, router, sleeps: Sleeps, raw: Any) -> None:
        router.post(URL).mock(return_value=ok(status=raw))
        with make(sleeps) as adapter:
            assert adapter.send("hello", session_id="s").status is ResponseStatus.UNKNOWN

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("answered", ResponseStatus.ANSWERED),
            (" Refused ", ResponseStatus.REFUSED),
            ("ESCALATED", ResponseStatus.ESCALATED),
            ("no_answer", ResponseStatus.NO_ANSWER),
            ("error", ResponseStatus.ERROR),
        ],
    )
    def test_known_statuses_map_regardless_of_case_and_padding(
        self, router, sleeps: Sleeps, raw: str, expected: ResponseStatus
    ) -> None:
        router.post(URL).mock(return_value=ok(status=raw))
        with make(sleeps) as adapter:
            assert adapter.send("hello", session_id="s").status is expected


class TestDescribe:
    def test_it_names_the_endpoint_without_credentials_or_query(self, sleeps: Sleeps) -> None:
        adapter = HTTPAdapter(
            "https://svc:s3cret@agent.test:8443/v1/chat?api_key=leak",
            principal_headers=PRINCIPALS,
            sleep=sleeps,
        )
        with adapter:
            info = adapter.describe()

        assert info == {"adapter": "http", "endpoint": "https://agent.test:8443/v1/chat"}
        assert "s3cret" not in str(info) and "leak" not in str(info)


class TestConfig:
    @staticmethod
    def _agent(**overrides: Any) -> AppConfig:
        agent: dict[str, Any] = {
            "adapter": "http",
            "url": URL,
            "principal_headers": {"default": {}, "user-a": {"Authorization": "Bearer t"}},
        }
        agent.update(overrides)
        return AppConfig(raw={"agent": agent})

    def test_the_factory_builds_it_from_config(self, router) -> None:
        route = router.post(URL).mock(return_value=ok())
        config = self._agent(
            max_attempts=2,
            response_mapping={"text": "reply"},
            request_template={"q": "{message}"},
        )
        with build_adapter(config) as adapter:
            assert isinstance(adapter, HTTPAdapter)
            adapter.send("hello", session_id="s", principal="user-a")

        assert route.calls.last.request.read() == b'{"q":"hello"}'

    def test_principal_fields_come_from_config(self, router) -> None:
        route = router.post(URL).mock(return_value=ok())
        config = self._agent(
            principal_fields={"default": {}, "user-a": {"user_id": "user-a"}},
        )
        with build_adapter(config) as adapter:
            adapter.send("hello", session_id="s", principal="user-a")

        assert route.calls.last.request.read() == (
            b'{"query":"hello","session_id":"s","user_id":"user-a"}'
        )

    def test_side_effecting_tools_is_accepted_and_makes_the_check_measurable(
        self, router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No simulator declaration: an HTTP run has no source but this key.
        monkeypatch.setattr("askback.evaluation.safety_eval._SIDE_EFFECT_SOURCES", [])
        router.post(URL).mock(return_value=ok())

        with build_adapter(self._agent(side_effecting_tools=[])) as adapter:
            response = adapter.send("hello", session_id="s")

        case = TestCase(id="t", turns=[Turn(user="q")])
        result = SafetyEvaluator().evaluate(case, case.turns[0], response)
        assert next(c for c in result.checks if c.name == "tool_side_effects").passed

    def test_http_is_a_listed_adapter(self) -> None:
        assert "http" in VALID_ADAPTERS

    @pytest.mark.parametrize(
        ("override", "match"),
        [
            ({"url": None}, "agent.url is required"),
            ({"url": "ftp://agent.test/"}, "http"),
            ({"principal_headers": None}, "principal_headers is required"),
            ({"principal_headers": {"user-a": "Bearer t"}}, "principal_headers.user-a"),
            ({"max_attempts": 0}, "max_attempts"),
            ({"max_attempts": True}, "max_attempts"),
            ({"timeout_s": "soon"}, "timeout_s"),
            ({"response_mapping": {"txt": "reply"}}, "'txt'"),
            ({"response_mapping": {"text": ""}}, "response_mapping.text"),
            ({"retries": 5}, "retries"),
            ({"principal_fields": ["user-a"]}, "principal_fields must be a mapping"),
        ],
    )
    def test_a_bad_agent_section_is_refused_by_name(
        self, override: dict[str, Any], match: str
    ) -> None:
        config = self._agent(**override)
        if override.get("url", "set") is None:
            del config.raw["agent"]["url"]
        if "principal_headers" in override and override["principal_headers"] is None:
            del config.raw["agent"]["principal_headers"]
        with pytest.raises(ConfigError, match=match):
            build_adapter(config)

    def test_an_http_run_has_no_tenants_for_the_dataset_check_to_compare(self) -> None:
        assert resolve_principals(self._agent()) == {}
