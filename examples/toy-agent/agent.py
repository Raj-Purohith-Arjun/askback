"""A toy agent to evaluate: a canned FAQ bot behind one HTTP endpoint.

It exists so the quickstart has something to grade without anyone's real agent.
Standard library only.

    python agent.py            # listens on 127.0.0.1:8123, with its deliberate flaw
    python agent.py --fixed    # same, without the flaw

POST /chat  {"query": "...", "session_id": "..."}
         -> {"reply": "...", "status": "answered" | "no_answer"}

The flaw: asked about parking, it invents a policy instead of saying it has none.
That is the failure the suite's `faq-parking` case exists to catch.
"""

from __future__ import annotations

import contextlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ANSWERS = {
    "leave": "Full-time employees receive 24 days of paid annual leave per calendar year.",
    "expense": "Expense claims must be submitted within 30 days of the expense date.",
    "probation": "New employees serve a probation period of 6 months.",
}
NO_ANSWER = "I do not have that information."


def respond(query: str, *, fixed: bool) -> dict[str, str]:
    text = query.lower()
    for keyword, answer in ANSWERS.items():
        if keyword in text:
            return {"reply": answer, "status": "answered"}
    if "parking" in text and not fixed:
        return {"reply": "Yes, employees get free parking in the main car park.", "status": "answered"}
    return {"reply": NO_ANSWER, "status": "no_answer"}


class Handler(BaseHTTPRequestHandler):
    fixed = False

    def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
        try:
            length = int(self.headers.get("Content-Length", "0"))
            query = str(json.loads(self.rfile.read(length))["query"])
        except (ValueError, KeyError, TypeError):
            self.send_error(422, "expected JSON with a `query` string")
            return
        body = json.dumps(respond(query, fixed=self.fixed)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # one line per turn would bury the eval's own output


def make_server(port: int, *, fixed: bool) -> HTTPServer:
    """A server for one flavour of the agent. Port 0 asks the OS for a free one."""

    class _Handler(Handler):
        pass

    _Handler.fixed = fixed
    return HTTPServer(("127.0.0.1", port), _Handler)


def main() -> None:
    fixed = "--fixed" in sys.argv[1:]
    server = make_server(8123, fixed=fixed)
    print(f"toy agent on http://127.0.0.1:8123/chat (fixed={fixed}); Ctrl+C to stop")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
