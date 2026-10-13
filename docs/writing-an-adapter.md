# Writing an adapter

An adapter is how the framework reaches the system you are testing. Suites, evaluators,
gates and reports never learn what is behind it, so the same suite file grades a
simulator in your process and a service on the other side of a load balancer.

You have two routes, and usually the first is enough.

1. **Configure the built-in `http` adapter** for any agent that takes a JSON request over
   HTTP and returns JSON. No code.
2. **Write an adapter** for anything else: a library call, a CLI, a message queue, a gRPC
   service, an SDK.

## Route 1: the `http` adapter

```bash
pip install -e ".[http]"
```

`agent:` in the config file says where the agent is, who each turn is sent as, how to
build the request, and where to find the answer in the response.

```yaml
agent:
  adapter: http
  url: https://agent.internal.example/v1/chat

  # Who each turn is sent as. One entry per principal label a suite uses (a case's
  # `user_id`). A label with no entry fails the case; it never falls back to anonymous,
  # because an isolation test that ran as the wrong user passes for the wrong reason.
  principal_headers:
    default: {}
    employee: {Authorization: "Bearer ${AGENT_TOKEN_EMPLOYEE}"}
    manager:  {Authorization: "Bearer ${AGENT_TOKEN_MANAGER}"}

  # How to ask. {message} is required; {session_id} is filled in too.
  request_template:
    input: "{message}"
    thread: "{session_id}"

  # Where the answer is. Dotted paths; numbers index into lists.
  response_mapping:
    text: choices.0.message.content      # the only mandatory one
    status: outcome
    route: meta.agent
    citations: sources
    retrieved: context
    tools: tool_calls
    session: thread

  # The tools of this agent that act on the world (raise a ticket, send mail, write a
  # record). [] is a statement that there are none.
  side_effecting_tools: [create_ticket]

  timeout_s: 30
  max_attempts: 3
  backoff_s: 0.5
```

What to know before you point it at something real:

- **Secrets come from the environment.** `${AGENT_TOKEN_EMPLOYEE}` is expanded when the
  config loads. A header whose whole value expands to nothing (`X-Api-Key: ${KEY}` with
  `KEY` unset) is **refused by name** rather than sent as an unauthenticated request whose
  401 you would misread as an agent failure. The check is on the whole value, so
  `Bearer ${TOKEN}` with `TOKEN` unset becomes `Bearer ` and is sent; check your variables
  before a long run.
- **Identity in the body** instead of a header: `principal_fields` merges per-principal
  fields into the top level of the request. It must list exactly the same labels as
  `principal_headers`, so a principal cannot be sent with no identity.
- **Report only what the agent returned.** A field the agent does not expose stays empty:
  no citations, no retrieved chunks, no route. The evaluators that need it then call the
  case unmeasurable instead of grading an invention. In particular a status that is
  absent or unrecognised becomes `unknown`, never `answered`; defaulting to success is how
  a broken integration goes green. The status strings understood are `answered`,
  `no_answer`, `refused`, `escalated` and `error`.
- **A turn may be delivered twice.** Transport errors and 408/409/425/429/500/502/503/504
  are retried up to `max_attempts` times, honouring `Retry-After` (capped at 60 seconds).
  If a response is lost after the agent acted, the agent sees the message again. For an
  agent that is not idempotent per session, set `max_attempts: 1` and accept transient
  failures as failed cases. Any other 4xx is not retried: it means the request is wrong.
- **`side_effecting_tools` is not optional in practice.** Left unset, the tool-misuse
  check fails every case as critical ("No tool declarations are registered"), because the
  framework will not pass a check on a list nobody wrote. Only you know your agent's tools.
- **Reports never contain credentials.** The report records the endpoint without userinfo
  or query string, and never a header value.

Then run it:

```bash
askback --config my-config.yaml eval --suite smoke
```

## Route 2: write an adapter

An adapter is a class with four methods. It is a `typing.Protocol`, so you do not inherit
from anything in the framework:

```python
class AgentAdapter(Protocol):
    def __enter__(self) -> AgentAdapter: ...
    def __exit__(self, exc_type, exc, traceback, /) -> None: ...
    def send(self, message: str, *, session_id: str, principal: str = "default") -> ChatResponse: ...
    def describe(self) -> dict[str, str]: ...
```

[`examples/python-adapter/run.py`](../examples/python-adapter/run.py) is a complete,
runnable one around a plain Python function, and it is the quickest way to see the shape:

```bash
python examples/python-adapter/run.py
```

```python
class FunctionAdapter:
    def __init__(self) -> None:
        self._history: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def __enter__(self) -> FunctionAdapter:
        return self                      # open your client here, once per run

    def __exit__(self, exc_type, exc, traceback, /) -> None:
        return None                      # and close it here

    def send(self, message, *, session_id, principal="default") -> ChatResponse:
        with self._lock:
            history = self._history.setdefault(session_id, [])
            earlier = list(history)
            history.append(message)
        reply, knew = my_agent(message, earlier)
        return ChatResponse(
            reply=reply,
            conversation_id=session_id,
            status=ResponseStatus.ANSWERED if knew else ResponseStatus.NO_ANSWER,
        )

    def describe(self) -> dict[str, str]:
        return {"adapter": "function", "agent": "my_agent v1"}
```

The contract, method by method:

**`__enter__` / `__exit__`** open and release whatever the adapter needs for a whole run.
The runner enters the adapter once, so an HTTP adapter holds one pooled client for every
case rather than paying a connection per turn. `__exit__` runs whether the run finished or
blew up, and must not swallow an exception (return `None`, not `True`). If you expect
the adapter to be entered by more than one run at once, subclass
`askback.adapters.base.AdapterLifecycle`, which counts entries so the first run to
finish does not close it under the others.

**`send`** delivers one user turn and returns what the system said.

- `session_id` is chosen by the framework. Turns that share one are one conversation and
  must see each other's history; turns with different ids must not, which is what stops
  test cases contaminating one another. Each sample of a case gets a fresh session.
- `principal` is a **label**, never a credential. Resolve it to whatever the system
  needs (a token, a user and tenant). It is recorded in reports, so a credential here
  would leak. An unknown label should raise, not fall back to a default identity.
- **`send` must be safe to call from several threads at once.** The runner sends
  different sessions from different workers simultaneously. Anything shared across calls
  (a client, a token cache, a session map) has to tolerate that. Turns within one session
  arrive in order, never interleaved. Nothing enforces this; breaking it shows up as
  cross-case contamination, not as an exception. (Run with `--workers 1` to rule it out
  while debugging.)
- **Report only what the system returned.** Leave a field it did not expose empty: no
  retrieved chunks, no citations, no route. Do not invent a status to fill the gap; use
  `ResponseStatus.UNKNOWN` when you cannot map one. This is the rule that keeps a
  missing field from reading as a pass, and it has been broken three times in this
  project's own adapters.
- **Raise when the turn could not be delivered** or its reply could not be read. The
  runner records a failed case. Never synthesise a reply to stand in for one.

**`describe`** is provenance for the report: which system this was. Return whatever would
make two runs differ for reasons other than the agent's behaviour (endpoint, model,
deployed version, corpus revision). Keep the keys stable from run to run, because a
renamed key compares as a different system. Strings only, and **never a secret**: it is
written into every report, and the regression comparison reads it to decide whether two
runs are comparable at all.

### Using it

A custom adapter is handed to the runner in Python, as the example does:

```python
cases = load_cases("suites/")
report = TestRunner(FunctionAdapter(), EvaluationEngine.from_config({})).run(cases)
verdict = evaluate_gates(report, {})
sys.exit(verdict.exit_code)
```

The `askback eval` command builds its adapter from config and knows two names, `in_process`
(the bundled simulator) and `http`. To make `askback eval` pick up a new one, add it to
`askback/adapters/factory.py` and to `VALID_ADAPTERS`, and send a pull request.

If your agent declares tools that act on the world, call
`declare_side_effecting_tools(lambda: ["create_ticket"])` (from
`askback.evaluation.safety_eval`) before the run, or `lambda: []` to say there are none.
Without a declaration the tool-misuse check fails every case.

### Testing an adapter

Test it the way the framework tests its own: against behaviour, not against mocks.

- Run one suite file through your adapter and through `ScriptedAdapter` (canned replies, a
  real implementation of the same protocol). The verdicts should agree.
- Test the failure paths on purpose: a timeout, a malformed payload, an unknown principal,
  a reply with a field missing. Each should raise or mark the field absent; none should
  produce a passing case.
- Call `send` from several threads at once for different sessions and assert that no
  history crossed over.
