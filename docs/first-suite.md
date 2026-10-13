# Writing your first suite

A suite is a file of test conversations. Each **case** is one conversation with your
agent: the messages a user sends, and what each reply must satisfy. The framework sends
the messages, collects the replies, and runs evaluators over them.

The suite from the [quickstart](quickstart.md), `examples/toy-agent/suites/faq.yaml`, is
a complete example. This page explains how to write one of your own.

## The shape

```yaml
schema_version: 2

cases:
  - id: faq-leave
    description: A question the agent can answer, with the fact it must state.
    tags: [policy, leave]
    risk: medium
    turns:
      - user: How many days of annual leave do I get?
        expect:
          expected_status: answered
          required_facts: ["24 days"]
```

- The **file name is the suite name**: `faq.yaml` gives suite `faq`. Put suites in a
  directory and point `evaluation.dataset_dir` at it, or pass `--dataset path/to/file`.
- `schema_version: 2` is required in YAML and JSON. A file without it is refused with the
  command that converts it (see "Other formats").
- A case needs an `id` (unique within its file) and at least one turn. Everything else
  is optional.
- Case fields: `description`, `tags` (select with `--tag`), `risk` (`low`, `medium` or
  `high`), `user_id` and `tenant_id` (who is asking, see below), `samples` and
  `min_passing_samples` (see below), `metadata`.
- **An unknown key is an error**, not ignored. A misspelt expectation is a check that
  silently never runs, so the loader refuses it and tells you where.

## Turns and expectations

A turn is one user message, and `expect` says what the reply to it must satisfy. Every
expectation is optional: an evaluator checks only what the turn asks for.

| Expectation | The reply must |
|---|---|
| `expected_status` | end as `answered`, `no_answer`, `refused` or `escalated`. **Critical.** |
| `required_facts` | contain each string. Case and whitespace are ignored. Wrap one in `/slashes/` for a regular expression. **Critical.** |
| `prohibited_content` | contain none of these strings (or `/regex/`). **Critical.** |
| `must_refuse` | be a refusal. Critical. |
| `expected_route` | come from this route. **Critical.** Needs an agent that reports its route. |
| `expected_intent` | be classified as this intent, when the agent reports one. |
| `expected_tools`, `forbidden_tools` | call these tools, or none of these. |
| `expected_docs` | retrieve these document ids, which needs the agent to return its retrieved chunks. |
| `min_citations` | carry at least this many citations. |
| `reference_answer` | be graded against this ideal answer by the judge. |
| `expected_handoff` | for an `escalated` turn: show a ticket reference or queue name. Defaults to true. |

Two of these are worth a sentence each:

- **`expected_status: no_answer` is how you test for invented answers.** Ask about
  something the agent has no policy on, and require it to say so. An agent that
  confidently answers instead has made up an entitlement. That is the most expensive
  failure a conversational agent has, and it is what the toy agent's `faq-parking`
  case catches.
- **Numbers and dates are not paraphrasable.** Use `required_facts` for "24 days",
  "30 days", a deadline, a limit. Semantic similarity is not enough.

## Multi-turn conversations

A case's turns are sent in order, in one session, so the agent sees the earlier ones.

```yaml
  - id: followup-keeps-context
    turns:
      - user: How many days of annual leave do I get?
        expect:
          required_facts: ["24 days"]
      - user: And how many can I carry over?
        expect:
          required_facts: ["5 days"]
```

A turn that is only there to set the scene is marked `evaluate: false`. It is sent and
never graded, and it may not carry an `expect` (that would be dropped silently, so it is
an error).

```yaml
      - user: My employee id is E-1042.
        evaluate: false
```

A case passes when every graded turn passes. A turn that errors ends the conversation
there, as a failure.

## Defaults

A top-level `defaults:` block is merged under every turn's `expect`, so a suite about
one route does not repeat it:

```yaml
schema_version: 2
defaults:
  expected_route: policy_agent
cases:
  - id: a
    turns:
      - user: ...
        expect: {required_facts: ["24 days"]}
  - id: b
    turns:
      - user: ...
        expect:
          expected_route: null      # opt this turn out of the default
```

A key the turn writes always wins, including an explicit `null`. A default never stands
in for something the author said.

## Who is asking

`user_id` on a case is a **principal label**: a name the adapter resolves to whatever
identity the agent needs (a token, a header, a user and tenant). It defaults to
`test_user`. For an HTTP agent each label must be listed under `agent.principal_headers`
in the config, or the case fails, because quietly running as someone else would make an
isolation test pass for the wrong reason.

`tenant_id` (default `default`) is the tenant the case expects to be acting in. Use a
second label and tenant to write isolation cases: user A asks for user B's data and must
be refused.

## Non-determinism: `samples`

A model-backed agent can answer differently each time, and one draw is not a
measurement. Say how many times to run a case and how many must pass:

```yaml
  - id: faq-leave
    samples: 5
    min_passing_samples: 4
    turns: [...]
```

Each sample is a fresh session. The case passes when `min_passing_samples` of them do
(default: all of them), and a case that passed some samples but not all is marked
`FLAKY` in the output and the report. A `min_passing_samples`
above `samples` is refused when the file loads. `evaluation.default_samples` sets the
number for cases that do not say; raise it only when you are measuring flakiness, because
it multiplies run time, and judge cost, by N.

## Other formats

CSV and XLSX are one row per case, so one turn per case: `id`, `message`, and each
expectation as a column (lists comma-separated). Use them when the people writing the
cases live in a spreadsheet. Anything multi-turn goes in YAML or JSON. A suite written
before `schema_version` converts with `python scripts/migrate_v1_v2.py <file or dir>`.

The full field list is in `src/askback/datasets/schema.py`.

## Running it

```bash
askback --config my-config.yaml eval                    # every suite in dataset_dir
askback --config my-config.yaml eval --suite faq        # one suite
askback --config my-config.yaml eval --tag leave        # cases with a tag
askback --config my-config.yaml eval --case faq-leave   # one case
```

A run passes when the overall pass rate clears `evaluation.suite_pass_rate` (default
0.8) **and** no critical check failed **and** every suite named in
`evaluation.suite_must_pass` clears its own floor. Put the suites that carry real risk in
`suite_must_pass`: a large easy suite can otherwise hold the overall rate up while a
small, important one rots.

## What makes a good suite

- **Cover what the agent must not do** as carefully as what it must. Hallucination,
  refusal, escalation and isolation cases are the ones that catch incidents.
- **Include a negative control.** A bot that refuses everything passes every refusal
  case. Add a case that asks something legitimate and requires an answer.
- **Pin every expected fact to the source of truth**, and have the person who owns the
  answer confirm it. An unverified expectation is worse than none.
- **Keep it small and meaningful.** A hundred cases that all test the same thing are one
  test with a hundred lines.
- **Use invented data.** Never put real names, real policy text or real employee data in
  a suite you commit.
