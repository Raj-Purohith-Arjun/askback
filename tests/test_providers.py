"""Model providers: a self-hosted gateway needs no key, and no model id ships unverified.

The wire tests run a real HTTP server on a real socket (stdlib only) and read what it
received, instead of patching the HTTP client: a patched client proves the patch.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from optional_deps import requires

requires("httpx", extra="http", test_module="tests/test_providers.py")

from askback.core.contracts import Message, Role  # noqa: E402
from askback.core.errors import ConfigError, ProviderError  # noqa: E402
from askback.evaluation.llm_judge import build_judge  # noqa: E402
from askback.providers.openai_compat import (  # noqa: E402
    DEFAULT_OPENAI_BASE_URL,
    AnthropicProvider,
    OpenAIProvider,
)

SCORES = {"correctness": 1, "completeness": 1, "groundedness": 1, "reasoning": 1, "clarity": 1}


class _Gateway:
    """A stand-in for vLLM / Ollama / LiteLLM: an OpenAI-shaped /chat/completions endpoint."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                reply = {
                    "choices": [{"message": {"content": json.dumps({**SCORES, "rationale": "ok"})}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def gateway() -> Iterator[_Gateway]:
    server = _Gateway()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in [n for n in os.environ if n.startswith(("JUDGE_", "PERSONA_"))]:
        monkeypatch.delenv(name)
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(name, raising=False)


class TestOpenAICompatible:
    def test_a_server_at_your_own_url_needs_no_key(self) -> None:
        provider = OpenAIProvider(base_url="http://localhost:11434/v1", model="llama3")
        assert provider.is_available() and provider.missing_settings() == {}
        assert "Authorization" not in provider._headers()

    def test_a_key_that_is_given_is_still_sent(self) -> None:
        provider = OpenAIProvider(base_url="http://localhost:11434/v1", model="m", api_key="k")
        assert provider._headers()["Authorization"] == "Bearer k"

    def test_openais_own_endpoint_still_needs_its_key_and_names_the_variable(self) -> None:
        provider = OpenAIProvider(env_prefix="JUDGE_")
        assert provider.base_url == DEFAULT_OPENAI_BASE_URL and provider.model == "gpt-4o-mini"
        assert provider.missing_settings() == {"api_key": "JUDGE_OPENAI_API_KEY"}

    def test_a_custom_server_has_no_default_model_so_the_missing_one_is_named(self) -> None:
        provider = OpenAIProvider(base_url="http://localhost:8000/v1", env_prefix="JUDGE_")
        assert provider.missing_settings() == {"model": "JUDGE_OPENAI_MODEL"}

    def test_the_base_url_and_model_come_from_the_environment_under_the_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JUDGE_OPENAI_BASE_URL", "http://gateway.internal:4000/v1/")
        monkeypatch.setenv("JUDGE_OPENAI_MODEL", "my-judge")
        judge_side = OpenAIProvider(env_prefix="JUDGE_")
        agent_side = OpenAIProvider()
        assert judge_side.base_url == "http://gateway.internal:4000/v1" and judge_side.model == "my-judge"
        # Nothing falls back across the prefix: the agent's provider does not see the judge's URL.
        assert agent_side.base_url == DEFAULT_OPENAI_BASE_URL

    def test_the_wire_carries_no_authorization_header_to_a_keyless_gateway(
        self, gateway: _Gateway
    ) -> None:
        provider = OpenAIProvider(base_url=gateway.url, model="llama3")
        reply = provider.generate([Message(Role.USER, "hi")], json_mode=True)

        (seen,) = gateway.requests
        assert seen["path"] == "/v1/chat/completions"
        assert not {h.lower() for h in seen["headers"]} & {"authorization", "api-key"}
        assert seen["body"]["model"] == "llama3" and reply.prompt_tokens == 10

    def test_a_judge_is_built_and_grades_against_a_keyless_local_gateway(
        self, gateway: _Gateway, tmp_path: Path
    ) -> None:
        judge = build_judge(
            {
                "llm": {"provider": "openai", "base_url": gateway.url, "model": "llama3"},
                "cache_path": str(tmp_path / "cache"),
            }
        )
        assert judge.identity == "model:openai/llama3"

    def test_a_judge_config_naming_no_model_for_a_custom_server_is_refused_by_setting(self) -> None:
        with pytest.raises(ConfigError, match=r"model \(set evaluation.judge.llm.model or JUDGE_OPENAI_MODEL\)"):
            build_judge({"llm": {"provider": "openai", "base_url": "http://localhost:1/v1"}})


class TestAnthropic:
    def test_there_is_no_default_model_id(self) -> None:
        provider = AnthropicProvider(api_key="k", env_prefix="JUDGE_")
        assert provider.model == ""
        assert provider.missing_settings() == {"model": "JUDGE_ANTHROPIC_MODEL"}

    def test_it_asks_for_both_the_key_and_the_model(self) -> None:
        assert set(AnthropicProvider().missing_settings()) == {"api_key", "model"}

    def test_a_named_model_makes_it_available_and_the_environment_can_name_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert AnthropicProvider(api_key="k", model="some-model").is_available()
        monkeypatch.setenv("JUDGE_ANTHROPIC_MODEL", "from-env")
        assert AnthropicProvider(api_key="k", env_prefix="JUDGE_").model == "from-env"

    def test_generate_refuses_to_call_out_unconfigured_and_says_what_is_missing(self) -> None:
        with pytest.raises(ProviderError, match=r"missing \['api_key', 'model'\]"):
            AnthropicProvider().generate([Message(Role.USER, "hi")])
