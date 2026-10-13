"""The wire-format probe, run the way it is used: as a single file, away from this repository.

`tools/probe_agent.py` is emailed to whoever can reach an agent we cannot, so it has to
stand alone: nothing from this package, and nothing that only works from this checkout. Two
tests hold that line (an import scan, and a run in isolated mode from an empty directory),
and the rest run it against real servers over real sockets, because a probe checked only
against a mock proves it agrees with itself.

What is NOT asserted: that the probe's *draft* event classification is right. It prints
that draft marked "review before use", and against the example server it is wrong
(`tool_status` is picked as the content event). The probe's job is the transcript; the draft
is a convenience.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from optional_deps import requires

requires("socketio", extra="probe", test_module="tests/test_probe.py")
requires("flask_socketio", extra="socketio-server", test_module="tests/test_probe.py")

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tools" / "probe_agent.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _probe(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run the probe in isolated mode (-I: no PYTHONPATH, no cwd on the path) from an empty dir."""
    out = tmp_path / "result.json"
    done = subprocess.run(
        [sys.executable, "-I", str(PROBE), *args, "-o", str(out)],
        capture_output=True, text=True, timeout=120, cwd=tmp_path, check=False,
    )  # fmt: skip
    return done, json.loads(out.read_text(encoding="utf-8"))


class TestStandalone:
    def test_it_imports_nothing_from_this_package(self) -> None:
        tree = ast.parse(PROBE.read_text(encoding="utf-8"))
        imported = {
            (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name="")])
        }
        assert not imported & {"askback", "askback_sim"}

    def test_it_runs_with_no_arguments_it_needs_and_says_what_is_missing(self, tmp_path: Path) -> None:
        done = subprocess.run(
            [sys.executable, "-I", str(PROBE)], capture_output=True, text=True, cwd=tmp_path, check=False
        )
        assert done.returncode == 2 and "url" in done.stderr


class TestOverHTTP:
    @pytest.fixture
    def toy(self) -> Iterator[int]:
        agent = _load(ROOT / "examples" / "toy-agent" / "agent.py", "probe_test_toy_agent")
        server = agent.make_server(0, fixed=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield int(server.server_port)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def test_it_finds_the_request_shape_the_agent_accepts(self, toy: int, tmp_path: Path) -> None:
        done, result = _probe(tmp_path, f"http://127.0.0.1:{toy}/chat", "How many days of annual leave?")

        assert done.returncode == 0 and "SUCCESS over http" in done.stdout
        assert result["accepted"]["request_shape"] == "query", "the toy agent takes {query: ...}"
        # It tried `message` first and was refused: the transcript of that is what it is for.
        assert result["attempts"][0]["response"]["status"] == 422
        assert "24 days" in json.dumps(result["accepted"]["response"]["body"])


class TestOverSocketIO:
    @pytest.fixture
    def agent(self) -> Iterator[Any]:
        module = _load(ROOT / "examples" / "socketio-agent" / "server.py", "probe_test_sio_server")
        # `message` is the first event name the probe tries, which keeps this to one attempt.
        server = module.AgentServer(event="message")
        server.port = server.start()
        yield server
        server.stop()

    def test_it_finds_the_namespace_event_and_payload_and_records_every_event(
        self, agent: Any, tmp_path: Path
    ) -> None:
        done, result = _probe(
            tmp_path, f"http://127.0.0.1:{agent.port}", "hello", "--socketio",
            "--namespace", "/chat", "--quiet-after", "1",
        )  # fmt: skip

        assert done.returncode == 0, done.stdout + done.stderr
        accepted = result["accepted"]
        assert (accepted["namespace"], accepted["emit_event"], accepted["payload_shape"]) == (
            "/chat", "message", "message",
        )
        # The catch-all handler recorded every event by name: this is the transcript that
        # makes the agent's event vocabulary knowable without reading its source.
        assert [e["event"] for e in result["events"]] == ["routing", "tool_status", "typing", "reply", "done"]
        assert agent.state.received == [(agent.state.connects[0], "hello")]

    def test_an_endpoint_that_refuses_every_shape_is_exit_1_with_each_refusal_written_down(
        self, agent: Any, tmp_path: Path
    ) -> None:
        # The example server has no HTTP /chat route, so every request shape gets a 404:
        # a failure that is fast (a closed port is slow to refuse on some platforms).
        done, result = _probe(tmp_path, f"http://127.0.0.1:{agent.port}/chat", "hi")

        assert done.returncode == 1 and "FAILED to get a reply over http" in done.stdout
        assert result["accepted"] is None
        assert {a["response"]["status"] for a in result["attempts"]} == {404}
        assert len(result["attempts"]) == 8, "every shape was tried and its refusal recorded"

    def test_a_wrong_namespace_is_reported_with_a_hint_not_a_silent_success(
        self, agent: Any, tmp_path: Path
    ) -> None:
        done, result = _probe(
            tmp_path, f"http://127.0.0.1:{agent.port}", "hello", "--socketio",
            "--namespace", "/nope", "--quiet-after", "1",
        )  # fmt: skip

        assert done.returncode == 1 and result["accepted"] is None
        assert agent.state.received == [], "nothing was delivered to a namespace that does not exist"
