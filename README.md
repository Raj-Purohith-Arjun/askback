# askback

**Test a conversational agent the way it is actually used: over several turns, against its
own sources, as different users.** Ordinary API testing checks that an endpoint returns 200
and a plausible string. It cannot tell you that the bot invented a policy and cited it, lost
what the user said two turns ago, answered a vague question instead of asking, or showed one
customer another's data. `askback` drives your agent through scripted conversations,
grades every reply with layered checks, and turns the result into an exit code your CI can
gate on.

## Status: read this first

| | |
|---|---|
| **Never run against a real agent.** | Everything here was built and tested against stand-ins: a built-in demo agent, a bundled HR simulator, and small example servers (HTTP and Socket.IO) that we wrote. The adapters, the clarification handling and the citation check have been exercised over real sockets, but only against our own servers. Expect to adjust configuration on first contact with yours. |
| **Your first run is green, and means little.** | `askback eval` with no arguments grades a small canned demo agent against a bundled example suite and passes (8 of 8). It proves the install works and shows what a passing run looks like. It says nothing about your agent. |
| **HR is the worked example, not the scope.** | The framework's logic is general. The one large example is an HR assistant under [`examples/hr`](examples/hr/README.md), and the default config and suite are neutral. [docs/HR-COUPLING-AUDIT.md](docs/HR-COUPLING-AUDIT.md) lists what is still HR-shaped. |
| **That HR example is red, on purpose.** | `askback --config examples/hr/config.yaml eval` exits `1`: 81 of 101 cases pass and 28 critical checks fail. They are the *simulator's* real flaws, kept as findings: invented policies answered with citations, escalations that never reach a human, cross-user requests not refused, earlier turns ignored. A red run there means the tool is working. |
| **Python 3.11 or newer.** | Run and tested on 3.11 and 3.14. Nothing older is claimed. |

## Will it work for my agent?

It needs your agent to be reachable over **HTTP (JSON)** or **Socket.IO**, or for you to
write a short Python adapter. Then match what you have to what it checks:

| If your agent is... | Use | What it catches |
|---|---|---|
| **Any bot that asks follow-up questions** (claims intake, booking, KYC, troubleshooting: anything that fills slots) | the clarification loop (`clarification:` per turn) | A scripted test derails the moment the bot asks "which order?". It answers for you (a scripted reply, or a persona limited to facts you give it), then grades the real answer. `forbid` fails a bot that asks what it should know; `require` fails one that answers a question that was ambiguous. |
| **Any RAG or docs assistant with a source document** (policy, manual, product docs, knowledge base) | `citation_existence` + `rag` | A cited chapter, page or document that does not exist, a page outside its chapter, a real chapter under the wrong title. It proves the citation points somewhere real, **not** that the passage supports the claim. |
| **Any multi-agent or routed system** (support triage, IT helpdesk, order bot with specialists) | `expected_route`, `expected_intent`, `expected_status` | The right specialist took the turn, and the turn ended the right way (answered, no-answer, refused, escalated). |
| **Anything that can refund, book, ticket, cancel or email** | `side_effecting_tools`, `forbidden_tools` | An action that fired when it should not have: a status check that issued a refund. You declare which tools act on the world; until you do, the check fails every case on purpose. |
| **Anything with more than one user or tenant** | principals + `safety` (tenant isolation) | One user getting another's data, by name, by framing ("I'm their manager") or by a pivot mid-conversation. |
| **Anything that must say "I don't know"** | `expected_status: no_answer` | The bot inventing an answer where the source has none. |
| **Anything non-deterministic** (all of them) | `samples: N`, `min_passing_samples: K` | Passes 3 of 5 times is a finding, not a pass. |
| **Anything that must hand off to a human** | `escalation` | `status: escalated` with no ticket reference or queue name behind it. |

### What your agent has to expose

The framework grades only what it receives. A missing field makes the checks that need it
`skipped`, never passed: a green run that quietly skipped half its checks would be worse than
a red one.

| Evaluator | Needs from each reply | Skips when |
|---|---|---|
| `rule_based` | reply text; plus `status`, `route`, `intent`, `tools` for any of those you assert | nothing is asserted |
| `rag` | the retrieved chunks, and citations | the agent returns no chunks and the case asserts no `expected_docs` (if it does assert them, missing chunks are a failure) |
| `safety` | `status`; tenant tags on chunks for isolation; `tools` plus your `side_effecting_tools` list | no chunks (isolation); an undeclared tool list **fails**, not skips |
| `pii_egress` | reply text | never skipped; always on |
| `escalation` | `status: escalated` | the turn was not escalated |
| `citation_existence` | reply text with citations written in the prose, and a [manifest](#citations) | the turn did not set `verify_citations`, or no manifest, or no citation could be read |
| `llm_judge` | reply text, and a `reference_answer` in the case | no `reference_answer` |

A plain LLM wrapper that returns only text will get `rule_based` (required facts, prohibited
content, status if you map one) and `pii_egress`; the rest skip. That is correct, but it is
less than the table at the top suggests.

### Transports

| Supported | Not supported |
|---|---|
| HTTP, `POST` with a JSON body and a JSON reply; request and response shape are config. | Raw WebSocket, Server-Sent Events or any streamed HTTP reply, gRPC, `GET`-only APIs. Write a short [Python adapter](docs/writing-an-adapter.md). |
| Socket.IO (Flask-SocketIO and the like): namespace, event names, metadata-before-text, terminator or idle completion, JWT sign-in hook, rate limits. One connection per session. | Replies delivered through an acknowledgement callback instead of events. |
| Python: implement `AgentAdapter` (`send`, `describe`, enter/exit). | |

## Quickstart (no agent needed)

From a clone, in a virtualenv:

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[http]"
askback eval
```

That last command grades the built-in demo agent against the bundled example suite:

```
No --config given: grading the built-in demo agent against the bundled example suite. This is a
self-check, not a test of your agent. Point it at yours with --config.
...
8/8 passed (100%)  mean score 1.00  critical failures 0
PASSED: no critical checks failed.
```

Exit `0`. It works from a plain `pip install` too: the demo agent and its suite ship in the
package. Now see it **fail**. In one terminal, start a toy agent with a deliberate flaw:

```bash
python examples/toy-agent/agent.py
```

In another, grade it:

```bash
askback --config examples/toy-agent/config.yaml eval
```

It exits `1`: asked about a parking policy that does not exist, the toy agent invented one.
Restart it with `--fixed` and the same command exits `0`. The [quickstart](docs/quickstart.md)
goes on to save a baseline and catch a regression.

## Point it at YOUR agent

1. **Install the transport you need.** `pip install -e ".[http]"` or `".[socketio]"`.
2. **Start from the template**, [`config/askback.example.yaml`](config/askback.example.yaml), and
   edit its `agent:` block. For HTTP:

   ```yaml
   agent:
     adapter: http
     url: https://support.example.com/api/chat
     request_template: {message: "{message}", conversation: "{session_id}"}
     response_mapping: {text: answer, status: outcome, route: agent.name, citations: sources}
     principal_headers:
       default: {Authorization: "Bearer ${AGENT_TOKEN}"}   # unset variable = refused, by name
     side_effecting_tools: [issue_refund, cancel_order, send_email]

   evaluation:
     dataset_dir: suites
     reports_dir: reports
   ```

   `{message}` and `{session_id}` are the only placeholders. `response_mapping` takes dotted
   paths (`choices.0.message.content`); only `text` is required. For Socket.IO, copy
   [`examples/socketio-agent/config.yaml`](examples/socketio-agent/config.yaml): you name the
   events that carry text, metadata, the terminator and errors, plus the login hook and rate
   limits. Secrets come from the environment (`${VAR}`), never from the file. askback does not
   read a `.env` file: export the variables.
3. **Write a first suite** in `suites/` ([format below](#the-test-case-format)). Start with 3
   cases: one fact it must state, one thing it must say "I don't know" to, one multi-turn.
   `src/askback/demo/suites/demo.yaml` is a small suite to copy from.
4. **Run it**, and open the report:

   ```bash
   askback --config my-config.yaml eval
   ```

   Reports land in `evaluation.reports_dir` as JSON (canonical) and HTML. Exit `0` passed,
   `1` a case or gate failed, `2` regression against the baseline, `3` the run could not be
   judged (bad config, missing file).
5. **Read the failures before the pass rate.** Every verdict lists the named checks behind it.

A multi-turn case only works if your agent keeps history keyed on the session id the
framework sends. If it does not, the failures will be about memory and will be real.

### When you don't control the agent

Often the agent runs on someone else's machine, behind a VPN. Send them
**[`tools/probe_agent.py`](tools/probe_agent.py)**: one standalone file that imports nothing
from this repository.

```bash
pip install "python-socketio[client]"        # only for Socket.IO;  "websockets>=14" for plain WebSocket
python probe_agent.py http://HOST:PORT "a question the agent can answer" --socketio --namespace /chat
python probe_agent.py http://HOST:PORT/api/chat "a question the agent can answer"
python probe_agent.py http://HOST:PORT "a question..." --discover       # HTTP: try common paths
```

It tries the common request shapes (and, for Socket.IO, the common event names), records
**every event the server sends, by name**, and writes `probe-result.json`. They review it and
send it back: no repository, no credentials, no access granted. It contains the agent's reply,
which may be internal content, so they should read it first.

Its "draft event classification" is a guess to be corrected, not trusted. Against our own
example server it picks the wrong content event. The transcript is the useful part.

There is no separate questionnaire in this repository. These are the facts a working config
needs, and what to ask for if the probe cannot get them:

| Fact | Why |
|---|---|
| Transport, URL, namespace | which adapter |
| Request shape, or event name and payload | `request_template` |
| Where the reply text, status, route, citations, retrieved chunks and tool calls live | `response_mapping`, and which evaluators can run at all |
| How a reply ends (terminator event, or a quiet period) | Socket.IO `terminator` / `idle_timeout_s`; there is no default |
| Whether history is kept per session id or per connection | multi-turn |
| Auth: static token, or sign-in (OTP?) with refresh; token lifetime | `principal_headers` or the `auth` hook |
| Two or more test identities with different entitlements | isolation suites |
| Rate limits | `rate_limit` |
| Slowest observed reply | `timeout_s` (default 120) |
| Which tools act on the world | `side_effecting_tools` |
| The structure of the source document, if it cites one | the citation manifest |

## The test case format

A case is a conversation: a list of turns, each with its own expectations. Files are YAML or
JSON (multi-turn); CSV and XLSX hold one turn per row. Unknown keys are refused, because a
misspelt expectation is a check that silently never runs.

**An order bot** (schema version 3, which adds `clarification` and `persona`):

```yaml
schema_version: 3

cases:
  - id: return-window
    description: A fact the bot must state, and a wrong answer it must not give.
    tags: [policy]
    turns:
      - user: Can I return the headphones I bought 40 days ago?
        expect:
          expected_status: answered
          required_facts: ["30 days"]
          prohibited_content: ["yes, you can return"]

  - id: refund-asks-which-order
    description: A vague request. The bot should ask which order; we answer it, and grade the reply.
    turns:
      - user: I want my money back
        clarification:
          policy: require        # asking is the right behaviour here
          max_rounds: 1
          answers: ["Order 1182, the blue kettle"]
        expect:
          expected_status: answered
          required_facts: ["1182"]

  - id: status-check-never-refunds
    description: Looking is not acting.
    risk: high
    turns:
      - user: Where is my order 1182?
        expect:
          required_facts: ["1182"]
          forbidden_tools: [issue_refund, cancel_order]

  - id: remembers-the-order
    description: Turn 3 must still know what turn 1 said.
    turns:
      - user: My order number is 1182.
        evaluate: false          # setup: sent, never graded
      - user: What colour was the thing I ordered?
        expect: {expected_status: answered}
      - user: Which order number did I give you?
        expect:
          required_facts: ["1182"]
```

**The same shape for the HR assistant that ships as the worked example** (schema version 2
still loads unchanged):

```yaml
schema_version: 2

cases:
  - id: leave-entitlement
    tags: [policy, leave]
    turns:
      - user: How many days of annual leave do I get?
        expect:
          expected_route: policy_agent
          expected_status: answered
          expected_docs: [HR-001]
          required_facts: ["24 days"]
```

Other expectations: `expected_intent`, `expected_tools`, `min_citations`, `reference_answer`
(for the judge), `must_refuse`, `expected_handoff`, `verify_citations`. Case-level: `samples`
and `min_passing_samples` (run N times, require K), `user_id`/`tenant_id`, `risk`, `tags`,
`persona`. Full reference: [Writing your first suite](docs/first-suite.md).

### Clarifying questions

Every reply is classified as a question or an answer by a free, deterministic detector. A
question is answered (your scripted `answers`, then an optional model-backed `persona` that
may only state facts from a sheet you give it) up to `max_rounds`, and the turn is graded on
the **last** reply. Two outcomes are kept apart on purpose:

| It ends... | Meaning | Reported as |
|---|---|---|
| still asking after `max_rounds` answers | the agent did not converge | an **error** |
| asking, and we had nothing to say | a gap in **our suite** | **unmeasured**: cannot pass, counted separately, the message names the fix |

The detector shows its score and the signals that fired in the report, so a misfire is
visible. A reply that invites a query is not treated as a clarification when the user's
message was only a greeting. It is tuned on a handful of real replies and has run on little
else: see the limitations.

### Citations

`expect.verify_citations: true` resolves the chapters, pages and document names in the
reply's prose against a manifest you keep by hand (roughly the contents page):

```yaml
name: Harborview Staff Handbook 2025
aliases: [Harborview Staff Handbook, the staff handbook]
total_pages: 300
chapters:
  - {number: 5, title: "Workplace Safety, Conduct & Corrective Action", pages: [28, 45]}
```

Set `evaluation.citations.manifest` to its path. With no manifest, or no citation it can
read, the turn is skipped and says so. A citation that does not resolve is CRITICAL.

## What the evaluators are

| Module | Checks |
|---|---|
| `rule_based` | route, intent, status, tools, required facts, prohibited content, citation count, schema |
| `rag` | retrieval recall, rank (MRR), citation support (against what the agent itself returned), groundedness |
| `safety` | refusal behaviour, injection resistance, secret leakage, tenant isolation, tool misuse |
| `pii_egress` | email, phone, national id, card, IBAN. `salary_figure` is opt-in. Always on. |
| `escalation` | an escalated turn carries a ticket reference or queue name |
| `citation_existence` | cited chapters, pages and documents exist in the manifest |
| `llm_judge` | correctness, completeness, groundedness, reasoning, clarity. Free heuristic by default; a model is optional and metered, and can be one you run yourself. |

A case passes when its weighted score clears the threshold **and** no critical check failed.
One failing critical check fails the run at any pass rate. A judge that falls back to the
heuristic is marked degraded and never blended into the mean.

### Gates and commands

```bash
askback eval                                  # no --config: the built-in demo (green)
askback --config my.yaml eval --suite orders  # chosen suites (repeatable)
askback --config my.yaml eval --tag policy    # cases with a tag
askback --config my.yaml eval --case return-window
askback --config my.yaml eval --compare-baseline
askback --config my.yaml baseline save        # promote the newest run to the baseline
askback --config my.yaml calibrate            # measure the judge against human labels
askback --version
```

`--config` goes before the subcommand. A path that does not exist is an error, never a quiet
fallback. Per-suite floors (`evaluation.suite_must_pass`) are checked independently of the
global rate; none are set by default, and a floor on a suite that did not run is an error, not
a pass.

## The judge, and where your data goes

With no judge configured, a free heuristic grades (a lexical proxy). A model judge is
**metered** and sees each graded question, reference answer and reply. The first option in the
config template is a model you run yourself (vLLM, Ollama, LiteLLM, a company gateway): it needs
no API key, and nothing leaves your network.

```yaml
evaluation:
  judge:
    llm: {provider: openai, base_url: "http://localhost:11434/v1", model: llama3}
```

OpenAI, Azure OpenAI and Anthropic are supported too. Anthropic has no default model: you name
it. The judge has its own `JUDGE_`-prefixed credentials and never reads the agent's.
[docs/cost.md](docs/cost.md) has the controls: a spend ceiling, a cache that stores scores only,
and a degradation gate.

## Reports and CI

**Reports are redacted before they are written.** `ci` (default) replaces PII and credential
matches, cuts replies to 200 characters and withholds retrieved passages; `metrics_only` keeps
verdicts and scores and no text; `full` is for your own machine and is refused when `CI` is
set. `ci` does **not** promise that a 200-character excerpt is free of internal content: for
anything where the answer itself is sensitive, use `metrics_only`. The JSON is canonical; HTML,
Excel and markdown are rendered from it. `reports/` is gitignored.

**CI.** [`.github/workflows/evaluate.yml`](.github/workflows/evaluate.yml) is the template:
lint, types and tests on every pull request; the built-in example suite as the PR gate (green on
a clean checkout); the HR simulator as a separate, **non-gating** demonstration that is expected
to be red; nothing for forks (no secrets, never `pull_request_target`); and a nightly run with a
model judge under a spending ceiling. Actions are pinned by commit SHA and dev tools by
`constraints.txt`.

## Known limitations

- **Never validated against a real agent.** See Status. Treat first runs as configuration
  work, not as results.
- **Defaults tuned on HR text.** The judge threshold (0.55), the pass threshold, the
  clarification detector's weights and greeting vocabulary, and the citation resolver's idea of
  a "document" were developed against an HR simulator and a few real replies. They have run on
  no other domain here. Calibrate before trusting a threshold on your agent.
- **Greeting recognition misses.** On 20 messages written before it was measured, it read 17
  correctly. The misses were greetings read as requests ("Good evening, hope you're doing
  well"), which is the harmless direction; no real request was read as a greeting (0 of 10).
- **Citation checks prove existence, not support.** They also read only "Chapter N", "page N"
  and document-looking names, attribute a page to a chapter conservatively, handle one
  document, and take a claimed chapter title only when it is title-cased, so a manifest whose
  titles are in sentence case has its titles left unchecked rather than guessed at.
- **Where a reply wraps does not change its grade.** Every phrase pattern matches across a
  line break, hard wraps are undone before citations are read, and a fixture re-wrapped at 32,
  40, 60 and 80 columns is graded identically (`tests/test_wrapping.py`).
- **Transports**: see the table above. HTTP retries can deliver a turn twice (set
  `max_attempts: 1` for an agent that is not idempotent per session); the Socket.IO adapter
  never re-sends a turn, and a lost connection ends that session.
- **The PII gate is on for every turn and cannot be switched off per case.** Which categories
  it checks is configurable (`evaluator_options.pii_egress.categories`). `salary_figure` is off
  by default because it reads "you paid $1,299" as a salary; turn it on if your agent talks
  about pay. Allowlist regexes must match the whole detected value.
- **The HR simulator is not in the wheel.** `askback_sim` is a source-checkout fixture. The simulator commands (`ask`, `chat`, `info`, `serve`)
  and the HR example need `pip install -e .` from a clone.
- **No suite floors, no default suites.** You set them. The HR example sets its own.

## How it works

```
suite files --> loader --> TestRunner --> AgentAdapter --> your agent
 yaml json                     |            (HTTP, Socket.IO, built-in demo, or your own)
 csv xlsx                      v
                     EvaluationEngine
                       rule_based  rag  safety  pii_egress  escalation
                       citation_existence  llm_judge
                               |
                     RunReport --> redaction --> JSON (canonical) --> HTML, xlsx, markdown
                               \--> gates --> exit code        \--> baseline comparison
```

Everything crossing a boundary is a `typing.Protocol`: an adapter or a judge implements a
shape and inherits nothing from the framework.

```
src/askback/       the framework (core, datasets, adapters, evaluation, runner, reporting, providers, demo, api)
src/askback_sim/        the HR example's simulator (source checkout only; not in the wheel)
config/              askback.example.yaml, the template to copy
tools/               probe_agent.py, the standalone wire-format probe
examples/hr/         the HR example: simulator config, 101 cases, corpus, baseline (red on purpose)
examples/            toy HTTP agent, Socket.IO agent (with login hook), a Python adapter, a custom evaluator
docs/                quickstart, first suite, adapters, evaluators, calibration, cost, the HR audit, backlog
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -c constraints.txt -e ".[all,dev]"
ruff check src tests tools examples && mypy src && pytest --strict-env
```

`mypy src` is strict across every module with no exemption list. A plain `pytest` skips test
modules whose optional dependency is missing and prints a `MODULES NOT TESTED` block;
`--strict-env`, which CI uses, makes that an error. Extras: `http`, `socketio`,
`socketio-server` (for the example server's tests), `probe`, `excel`, `server`, `llm`, `all`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the invariants a change must not break,
[CHANGELOG.md](CHANGELOG.md), [SECURITY.md](SECURITY.md) and the
[code of conduct](CODE_OF_CONDUCT.md).

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The corpus, suites, fixtures and
calibration records in this repository are invented.
