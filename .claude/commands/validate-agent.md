---
description: Phase 1 — point askback at one real open-source agent and record what happened.
argument-hint: "<agent name or repo url> [local url if it is already running]"
---

Run the framework against one real agent — `$ARGUMENTS` — and produce a validation record.
This is plan steps `1.3`–`1.7`, once per agent. Read `.claude/PLAN.md` §1.2 for the record's
shape before starting.

**The output of this command is a measurement, not a fix.** Do not change anything under
`src/` while running it. Everything that needs fixing is collected and spent in step `1.8`. A
fix applied mid-measurement contaminates the measurement, and the measurement is the point.

## 1. Stand it up

Run it locally, in a container where possible. Pin the version or commit and write it down;
"latest" is not reproducible. Never point this at a deployment you do not own — a probe
transcript carries the agent's replies, and the suites in this repository are designed to ask
for other people's data on purpose.

Do not go far down a rabbit hole getting it running. If it does not come up in a reasonable
effort, record *that* — a conversational agent that cannot be stood up in an hour is itself a
finding about this exercise's reach — and move to the next agent.

## 2. Probe before configuring

```bash
python tools/probe_agent.py <url> "a question the agent can answer" [--socketio --namespace /x]
python tools/probe_agent.py <url> "a question..." --discover      # HTTP: try common paths
```

Keep `probe-result.json`. Read the recorded events yourself; the draft classification is
documented as a guess and is known to pick the wrong content event against our own example
server. The transcript is the useful part.

Record, from the transcript alone, before writing any config:

- the transport, and whether a reply ends with a terminator event or a quiet period;
- where the reply text lives; and whether **status, route, intent, retrieved chunks, citations
  and tool calls** are present at all. Most agents return text and nothing else.
- whether history is kept per session id or per connection. Multi-turn depends on it.

## 3. Write the config and a small suite

Config in `examples/validation/<agent>/config.yaml`, from `config/askback.example.yaml`.
Secrets from the environment, never in the file.

10–20 cases in `examples/validation/<agent>/suites/`, and they must include:

- one fact the agent must state, taken from its own shipped source;
- one `expected_status: no_answer` — a question its sources cannot answer;
- one multi-turn case where turn 3 needs turn 1;
- one clarification case, if it asks follow-up questions at all;
- one cross-user or cross-tenant case, if it has any notion of users.

Write expectations from the agent's own documentation and corpus, not from its replies. A
suite written by reading the replies first grades the agent against itself and will pass.

## 4. Run it and read the failures before the pass rate

```bash
askback --config examples/validation/<agent>/config.yaml eval --format json,html
```

Then go through every verdict. For each finding, label it:

- **`agent-flaw`** — the agent did the wrong thing. Quote the reply and the named checks.
- **`harness-flaw`** — the agent was fine and we graded it wrongly. The most valuable label
  in this exercise. Capture the exact reply as a fixture candidate.
- **`suite-flaw`** — our expectation was wrong or unmeasurable as written.

And record separately, because it is the honest headline:

- **what skipped, and why.** A large `skipped` count against a text-only agent is the correct
  result (invariants 1 and 8). Do not infer a status from reply text to reduce it. If you find
  yourself wanting to, that wish is itself a finding — write it down.
- **which shipped default was wrong on this text.** The judge threshold (0.55), the
  clarification detector's weights and greeting vocabulary, and the citation resolver's idea of
  a document were all tuned on HR text and are named in `docs/HR-COUPLING-AUDIT.md`. This is
  the first evidence from anywhere else. Say what it says.

## 5. Record

Fill the template in `docs/VALIDATION.md`, and commit under
`examples/validation/<agent>/`: the config, the suite, the probe transcript and the report —
redacted at `ci`, and check it before committing. Someone must be able to reproduce every
finding from what you committed plus one `docker run`.

Commit message: `docs(validation): <agent> — N findings (A agent, B harness, C suite)`.

## 6. Report

Blockers only, plus three numbers: findings by label, evaluators skipped, defaults contradicted.
Then name the next agent on the `1.1` shortlist and stop.

If the exercise suggests a change to the harness, **write it in the record and leave the code
alone.** Step `1.8` spends the list, with verdict counts, in one place where the trade-offs are
visible together.
