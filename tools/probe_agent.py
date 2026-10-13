#!/usr/bin/env python3
"""Standalone wire-format probe. Single file, no installation.

The problem this solves: the people building the test framework often cannot reach
the agent. It runs on someone else's machine, behind a VPN, or on a laptop nobody is
going to hand over. Meanwhile the framework needs about fifteen facts about the
agent's API, and guessing them from a screenshot of the chat UI does not work.

So this file is the handoff. Send it to whoever can reach the agent. They run one
command, review the output, and send back a JSON file. That is the whole exchange —
no repository, no credentials shared, no access granted.

    python probe_agent.py ws://127.0.0.1:9000 "what is the leave policy?"
    python probe_agent.py http://127.0.0.1:9000/api/chat "what is the leave policy?"
    python probe_agent.py http://127.0.0.1:9000 "..." --discover

Only the standard library is needed for HTTP. WebSocket probing additionally needs
`pip install websockets`, and the script says so rather than failing obscurely.

Review the output before sending it on: it contains whatever the agent replied, which
on an internal assistant may be internal content.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Request body shapes, most common first. An agent's API almost always accepts one of
# these; trying them in order is faster than asking someone to read their own router.
REQUEST_SHAPES: list[tuple[str, dict[str, Any]]] = [
    ("message", {"message": "{q}"}),
    ("query", {"query": "{q}"}),
    ("question", {"question": "{q}"}),
    ("text", {"text": "{q}"}),
    ("input", {"input": "{q}"}),
    ("prompt", {"prompt": "{q}"}),
    ("openai-style", {"messages": [{"role": "user", "content": "{q}"}]}),
    ("message+session", {"message": "{q}", "session_id": "probe-session-1"}),
]

# Paths to try when given a base URL rather than an exact endpoint.
CANDIDATE_PATHS = [
    "/chat", "/api/chat", "/query", "/api/query", "/ask", "/api/ask",
    "/message", "/api/message", "/v1/chat", "/chat/completions", "/invoke", "/",
]


def _render(shape: dict[str, Any], question: str) -> dict[str, Any]:
    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return node.replace("{q}", question)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(shape)  # type: ignore[no-any-return]


def post_json(
    url: str, payload: dict[str, Any], timeout: float, headers: dict[str, str]
) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            elapsed = (time.perf_counter() - started) * 1000
            return {
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "elapsed_ms": round(elapsed, 1),
                "body": _maybe_json(raw),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": exc.code,
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "body": _maybe_json(exc.read().decode("utf-8", "replace")[:4000]),
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _maybe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return {"_non_json_text": raw[:4000]}


def probe_http(
    url: str, question: str, *, timeout: float, headers: dict[str, str], discover: bool
) -> dict[str, Any]:
    """Try request shapes until one is accepted, optionally hunting for the path too."""
    targets = (
        [url.rstrip("/") + path for path in CANDIDATE_PATHS] if discover else [url]
    )
    attempts: list[dict[str, Any]] = []

    for target in targets:
        for name, shape in REQUEST_SHAPES:
            payload = _render(shape, question)
            result = post_json(target, payload, timeout, headers)
            attempts.append(
                {
                    "url": target,
                    "request_shape": name,
                    "request_body": payload,
                    "response": result,
                }
            )
            status = result.get("status")
            # 2xx means this shape was understood. Stop here rather than hammering a
            # live assistant with every remaining permutation.
            if status is not None and 200 <= status < 300:
                return {"transport": "http", "accepted": attempts[-1], "attempts": attempts}
            # A connection failure means the path is wrong; move on without trying
            # seven more body shapes against a URL that does not answer.
            if status is None and discover:
                break

    return {"transport": "http", "accepted": None, "attempts": attempts}


def probe_websocket(
    url: str, question: str, *, timeout: float, quiet_after: float, headers: dict[str, str]
) -> dict[str, Any]:
    try:
        import asyncio

        import websockets
    except ImportError:
        print(
            "WebSocket probing needs the 'websockets' package:\n"
            "    pip install websockets\n",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    async def run() -> dict[str, Any]:
        for name, shape in REQUEST_SHAPES:
            payload = _render(shape, question)
            frames: list[dict[str, Any]] = []
            started = time.perf_counter()
            try:
                async with websockets.connect(
                    url,
                    additional_headers=headers or None,
                    open_timeout=15,
                    max_size=None,
                ) as socket:
                    await socket.send(json.dumps(payload))
                    while True:
                        budget = min(
                            quiet_after, timeout - (time.perf_counter() - started)
                        )
                        if budget <= 0:
                            break
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=budget)
                        except (TimeoutError, asyncio.TimeoutError):
                            break  # gone quiet; assume the reply finished
                        except Exception:
                            break  # closed, which is itself a terminator
                        elapsed = (time.perf_counter() - started) * 1000
                        parsed = _maybe_json(
                            raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                        )
                        if not isinstance(parsed, dict):
                            parsed = {"_raw_value": parsed}
                        parsed["_elapsed_ms"] = round(elapsed, 1)
                        frames.append(parsed)
            except Exception as exc:
                return {
                    "transport": "websocket",
                    "accepted": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "request_shape_tried": name,
                }

            # Frames came back, so this request shape was understood.
            if frames:
                return {
                    "transport": "websocket",
                    "accepted": {"request_shape": name, "request_body": payload},
                    "frames": frames,
                }

        return {
            "transport": "websocket",
            "accepted": None,
            "error": "connected, but no request shape produced a reply",
        }

    return asyncio.run(run())


# Event names a chat server is likely to listen on. Tried in order, because the
# probe exists precisely so nobody has to read the server's own router to answer.
EMIT_EVENTS = [
    "message", "user_message", "chat_message", "send_message",
    "chat", "query", "ask", "send", "user_query",
]

# Payload shapes to pair with them. Reuses the HTTP shapes plus the two-field forms
# Socket.IO handlers commonly take.
SOCKETIO_SHAPES: list[tuple[str, Any]] = [
    ("message", {"message": "{q}"}),
    ("text", {"text": "{q}"}),
    ("query", {"query": "{q}"}),
    ("message+session", {"message": "{q}", "session_id": "probe-1"}),
    ("bare-string", "{q}"),
]


def probe_socketio(
    url: str,
    question: str,
    *,
    namespace: str,
    timeout: float,
    quiet_after: float,
    headers: dict[str, str],
    auth: dict[str, Any] | None,
) -> dict[str, Any]:
    """Probe a Socket.IO server, discovering both the event name and payload shape.

    The load-bearing part is the **catch-all handler**. We do not know what the
    server emits, so registering handlers for guessed event names would capture
    nothing and report silence as failure. A catch-all records every event by name,
    which is the single most useful thing to send back: it turns "what does your
    chat API look like" into a transcript.
    """
    try:
        import socketio as sio_lib
    except ImportError:
        print(
            "Socket.IO probing needs the client library:\n"
            "    pip install 'python-socketio[client]'\n",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    attempts: list[dict[str, Any]] = []

    for event_name in EMIT_EVENTS:
        for shape_name, shape in SOCKETIO_SHAPES:
            payload = _render(shape, question) if isinstance(shape, dict) else question
            captured: list[dict[str, Any]] = []
            started = time.perf_counter()
            client = sio_lib.Client(reconnection=False, logger=False, engineio_logger=False)

            # Catch-all: records anything the server emits, under any name.
            # `sink` and `t0` are bound as defaults rather than closed over: this
            # handler outlives the loop iteration that created it, and a late event
            # from a previous attempt would otherwise append to the wrong list.
            @client.on("*", namespace=namespace)  # type: ignore[misc]
            def _any(event: str, *args: Any, sink: list = captured, t0: float = started) -> None:
                sink.append(
                    {
                        "event": event,
                        "args": list(args),
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    }
                )

            error: str | None = None
            before_emit = 0
            try:
                # Transports are left to the library to negotiate (polling, then
                # upgrade). Forcing ["websocket", ...] raises instead of falling
                # back when the optional websocket-client package is missing —
                # which it will be on the machine this gets emailed to.
                connect_kwargs: dict[str, Any] = {
                    "namespaces": [namespace],
                    "wait_timeout": 15,
                }
                if headers:
                    connect_kwargs["headers"] = headers
                if auth:
                    connect_kwargs["auth"] = auth
                client.connect(url, **connect_kwargs)

                # Servers commonly emit a greeting on connect. That is a handshake
                # artefact, not an answer, and an elapsed-time threshold cannot tell
                # them apart because connecting itself takes time. Mark the boundary
                # structurally: everything from this index on is a response to our
                # emit. Without this, probing with the WRONG event name reports
                # success on the back of the connect greeting alone.
                client.sleep(0.4)
                before_emit = len(captured)
                client.emit(event_name, payload, namespace=namespace)

                # Stop on silence rather than on a terminator, because the
                # terminator's name is exactly what we do not know yet.
                last_count, idle_since = len(captured), time.perf_counter()
                while time.perf_counter() - started < timeout:
                    client.sleep(0.25)
                    if len(captured) != last_count:
                        last_count, idle_since = len(captured), time.perf_counter()
                    elif time.perf_counter() - idle_since > quiet_after:
                        break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                with contextlib.suppress(Exception):
                    client.disconnect()

            replies = captured[before_emit:] if not error else []
            attempts.append(
                {
                    "namespace": namespace,
                    "emit_event": event_name,
                    "payload_shape": shape_name,
                    "payload": payload,
                    "events": captured,
                    "handshake_events": before_emit if not error else 0,
                    "reply_events": len(replies),
                    **({"error": error} if error else {}),
                }
            )

            if error and "namespace" in error.lower():
                # Wrong namespace: no event name will help. Stop early and say so.
                return {
                    "transport": "socketio",
                    "accepted": None,
                    "error": error,
                    "hint": (
                        f"namespace {namespace!r} was refused. Try --namespace / "
                        f"or ask which namespace the chat is on."
                    ),
                    "attempts": attempts,
                }
            if error:
                # Connection-level failure: further event names are pointless.
                return {"transport": "socketio", "accepted": None, "error": error,
                        "attempts": attempts}
            if len(replies) >= 1:
                return {
                    "transport": "socketio",
                    "accepted": attempts[-1],
                    "events": replies,
                    "handshake_events": captured[:before_emit],
                    "attempts": attempts,
                }

    return {"transport": "socketio", "accepted": None, "attempts": attempts,
            "error": "connected, but no event name produced a reply"}


def suggest_socketio_config(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Draft an event classification from observed traffic.

    A best guess, printed with its reasoning so it is corrected rather than trusted:
    the last event of a reply is usually the terminator, an event carrying a long
    string is content, and the rest is metadata.
    """
    content_event, terminator, metadata = None, None, []
    longest = 0
    for item in events:
        blob = json.dumps(item.get("args", []), default=str)
        if len(blob) > longest and len(blob) > 80:
            longest, content_event = len(blob), item["event"]
    if events:
        terminator = events[-1]["event"]
    for item in events:
        if item["event"] not in (content_event, terminator):
            metadata.append(item["event"])

    return {
        "content_events": [content_event] if content_event else [],
        "terminator_events": [terminator] if terminator else [],
        "metadata_events": sorted(set(metadata)),
    }


def summarise(result: dict[str, Any]) -> str:
    lines: list[str] = []
    transport = result.get("transport")

    if result.get("accepted") is None:
        lines.append(f"FAILED to get a reply over {transport}.")
        if error := result.get("error"):
            lines.append(f"  {error}")
        if hint := result.get("hint"):
            lines.append(f"  {hint}")
        for attempt in result.get("attempts", [])[:8]:
            if "response" in attempt:  # HTTP
                response = attempt["response"]
                lines.append(
                    f"  {attempt['url']}  shape={attempt['request_shape']}  "
                    f"-> {response.get('status') or response.get('error')}"
                )
            else:  # Socket.IO
                outcome = attempt.get("error") or f"{len(attempt.get('events', []))} event(s)"
                lines.append(
                    f"  emit={attempt['emit_event']}  shape={attempt['payload_shape']}  "
                    f"-> {outcome}"
                )
        lines.append("")
        lines.append("Send the JSON file back anyway — the failures narrow it down.")
        return "\n".join(lines)

    accepted = result["accepted"]
    lines.append(f"SUCCESS over {transport}.")
    shape = accepted.get("request_shape") or accepted.get("payload_shape")
    body = accepted.get("request_body", accepted.get("payload"))
    lines.append(f"  payload shape: {shape}")
    lines.append(f"  payload sent:  {json.dumps(body, default=str)}")

    if transport == "socketio":
        events = result.get("events", [])
        lines.append(f"  namespace:     {accepted['namespace']}")
        lines.append(f"  emit event:    {accepted['emit_event']}")
        lines.append(f"  events received: {len(events)}")
        for item in events:
            blob = json.dumps(item.get("args", []), default=str)
            preview = blob[:90] + ("…" if len(blob) > 90 else "")
            lines.append(f"    {item['elapsed_ms']:>8}ms  {item['event']}  {preview}")
        draft = suggest_socketio_config(events)
        lines.append("")
        lines.append("  draft event classification (review before use):")
        for key, value in draft.items():
            lines.append(f"    {key}: {value}")
    elif transport == "websocket":
        frames = result.get("frames", [])
        lines.append(f"  frames received: {len(frames)}")
        for i, frame in enumerate(frames):
            keys = sorted(k for k in frame if not k.startswith("_"))
            lines.append(f"    frame {i} @ {frame.get('_elapsed_ms')}ms  keys={keys}")
    else:
        response = accepted["response"]
        lines.append(f"  status: {response['status']}  in {response['elapsed_ms']}ms")
        body = response.get("body")
        if isinstance(body, dict):
            lines.append(f"  top-level keys: {sorted(body)}")

    lines.append("")
    lines.append("Review the JSON file, then send it back. It contains the agent's")
    lines.append("reply, which may include internal content.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("url", help="ws://, wss://, http:// or https:// endpoint")
    parser.add_argument("message", help="a question the agent can answer")
    parser.add_argument("-o", "--out", default="probe-result.json")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--quiet-after",
        type=float,
        default=8.0,
        help="WebSocket: stop after this many seconds of silence (default 8)",
    )
    parser.add_argument(
        "--socketio",
        action="store_true",
        help="the URL is a Socket.IO server (Flask-SocketIO, etc), not plain HTTP",
    )
    parser.add_argument(
        "--namespace",
        default="/",
        help="Socket.IO namespace, e.g. /chat (default /)",
    )
    parser.add_argument(
        "--auth",
        metavar="JSON",
        help="JSON passed as the Socket.IO connect auth payload, e.g. '{\"token\":\"...\"}'",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="HTTP: treat the URL as a base and try common chat paths",
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="extra request header; repeatable (e.g. -H 'Authorization: Bearer ...')",
    )
    args = parser.parse_args()

    headers: dict[str, str] = {}
    for item in args.header:
        name, _, value = item.partition(":")
        if value:
            headers[name.strip()] = value.strip()

    if args.socketio:
        auth = json.loads(args.auth) if args.auth else None
        result = probe_socketio(
            args.url,
            args.message,
            namespace=args.namespace,
            timeout=args.timeout,
            quiet_after=args.quiet_after,
            headers=headers,
            auth=auth,
        )
    elif args.url.startswith(("ws://", "wss://")):
        result = probe_websocket(
            args.url,
            args.message,
            timeout=args.timeout,
            quiet_after=args.quiet_after,
            headers=headers,
        )
    elif args.url.startswith(("http://", "https://")):
        result = probe_http(
            args.url,
            args.message,
            timeout=args.timeout,
            headers=headers,
            discover=args.discover,
        )
    else:
        print(f"unrecognised scheme in {args.url!r}", file=sys.stderr)
        return 2

    result["probe_metadata"] = {
        "url": args.url,
        "message": args.message,
        "python": sys.version.split()[0],
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False, default=str)

    print(summarise(result))
    print(f"\nwritten to {args.out}")
    return 0 if result.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
