# Roadmap

Where this project is going, in the order it is going there. Each phase states what a
stranger can do at the end of it that they could not do at the start. A phase is done when
that sentence is true, not when its steps are checked off.

The execution detail — step ids, the diff each one touches, the command that proves it —
is in [`.claude/PLAN.md`](../.claude/PLAN.md). This file is the public version: what and
why, not how.

---

## Where it stands today (v0.1.0, unreleased)

| | |
|---|---|
| Framework | 12.3k lines under `src/askback`, 7.7k lines of tests, 769 passing |
| Gates | `ruff`, `mypy --strict` with no exemption list, `pytest --strict-env` — all clean |
| Validated against | the built-in demo agent, the bundled HR simulator, two example servers we wrote. **No real agent.** |
| Installable by a stranger | **No.** Not on PyPI, no tag, no release. The README's first instruction is `git clone`. |
| Time from `pip install` to a finding about *your* agent | unbounded: you must write a config and a suite first |
| Distribution surface | a CLI, and a workflow file to copy by hand |

The engineering is ahead of the distribution by a wide margin. Everything below is about
closing that gap, in the order that costs least and returns most.

---

## The wedge: what this is for, that the alternatives are not

The field is crowded — DeepEval, Promptfoo, Ragas, Inspect AI, Langfuse, Phoenix, TruLens,
and the managed platforms. Being "an eval framework" is not a position. This one is
narrower and it should say so in one line:

> **The alternatives evaluate a pipeline you own, from inside your Python process.
> `askback` tests a deployed agent you may not own, over the wire it actually speaks, across
> several turns, as more than one user.**

Four things follow from that, and all four are either rare or absent elsewhere:

1. **It answers back.** The agent asks "which order?" mid-test; the harness answers (scripted,
   or a persona confined to a fact sheet) and grades the reply that follows. A scripted test
   suite derails at that question. This is the project's name and its best idea, and nothing
   else in the field handles it.
2. **It speaks the agent's transport.** HTTP and Socket.IO today, with the request and response
   shape in config, plus a standalone probe to send to whoever can reach a system you cannot.
   The others assume you can `import` the thing under test.
3. **It tests more than one identity.** Cross-principal and cross-tenant isolation as a first
   class suite, not a prompt-injection string list.
4. **It refuses to score what it did not measure.** Missing input is `skipped` or `error`,
   never `pass`. That invariant, enforced in tests, is the whole credibility of a green run.

Everything in this roadmap either sharpens that wedge or removes a reason not to try the tool.
Anything that does neither is a non-goal, listed at the bottom.

---

## Phase 0 — A stranger can install it

*Cost: days. Blocks every other phase, including any launch.*

Right now the single largest defect in this repository is that `pip install askback` does
nothing. Nothing else is worth doing first.

- Claim the `askback` name on PyPI and publish 0.1.0 through **trusted publishing** (OIDC, no
  long-lived token in the repo), with attestations.
- Tag `v0.1.0`, cut a GitHub release whose notes are the CHANGELOG section.
- Add the Python versions that work to CI. The suite passes on 3.13 today and CI tests only
  3.11 and 3.14; most users are on 3.12 or 3.13 and `requires-python` already admits them.
  Either test them or stop claiming them.
- Badges that are checks, not decoration: PyPI version, Python versions, CI, licence, coverage.
- Repository metadata: description, topics, a social preview image. A link shared without a
  preview image converts at a fraction of one with it.
- Community scaffolding: issue templates (bug / wire-format mismatch / evaluator proposal), a
  PR template that lists the invariants, `CODEOWNERS`, Dependabot, CodeQL, OpenSSF Scorecard.
- Coverage measured and published. The repo's own target is 0.8 test lines per source line; it
  is at about 0.52. Measure the real thing instead of the proxy and set a floor that ratchets.

**Done when:** on a clean machine, `pip install askback && askback eval` passes, and the
README's first command is that, not `git clone`.

---

## Phase 1 — First contact with a real agent

*Cost: 2–3 weeks, most of it reading failures. The highest-value work in this document.*

The README's first table says the framework has never been run against a real agent. That
honesty is the right call and it is also, today, the reason a serious reader closes the tab.
The fix is not to soften the sentence. It is to make it untrue.

There are a dozen conversational agents on GitHub that anyone can run in a container:
chat-over-your-docs assistants, support-bot starters, RAG templates, open deployments of
OpenWebUI-class front ends, Rasa and Chainlit and Dify apps. Pick five with different wire
formats. For each: stand it up, write a real config and a 10–20 case suite, run it, and
**record what happened** — in `docs/VALIDATION.md`, one section per agent:

- what the adapter needed that the agent did not expose, and what skipped as a result;
- which defaults were wrong on text that is not HR text (the judge threshold, the
  clarification detector's weights, the citation resolver's idea of a document — all three are
  named as HR-tuned in `docs/HR-COUPLING-AUDIT.md`);
- every finding the suite produced, and whether it was a real flaw in the agent or a flaw in
  the harness. Both outcomes are publishable. The second kind is the point of the exercise.

Fix what the exercise breaks. Expect the clarification detector, the greeting rule and
`response_mapping` to take most of the damage. Keep the known miss rates rather than chasing
100% — invariant 9 governs: a verdict that changes is data, not a regression to undo.

**Done when:** `docs/VALIDATION.md` describes five real agents, the README's Status table
reads "validated against N real agents, here is what it found and what it got wrong," and the
compatibility matrix below is real.

| Agent / stack | Transport | Verified | Findings |
|---|---|---|---|
| *(to be filled by Phase 1 — an empty row is better than an implied one)* | | | |

This phase is also the only honest route to a launch. "A framework I wrote" is not a story.
"I pointed a new harness at five open-source chatbots and all five invented a policy and cited
it" is a story, and it is the kind that travels.

---

## Phase 2 — Transports people actually run, and a finding in sixty seconds

*Cost: 3–4 weeks. This is the adoption curve.*

First, the transport gap, because it is the one that makes the tool unusable rather than
inconvenient. Raw WebSocket, Server-Sent Events and streamed HTTP replies are all listed as
unsupported, and **most deployed chat agents stream**. Phase 1 will show how many of five real
agents needed one of them; that number sizes the work, and the work comes before any
convenience feature, since a convenience that cannot connect is worth nothing. The hard part is
not the protocol, it is deciding what "the reply" is when it arrives in pieces, and saying so
in writing.

Then the time-to-first-finding problem.

Today the path to first value is: install, read the config template, learn the suite format,
discover your agent's wire format, write a suite, run. Every one of those is a place to quit.
Collapse it to one command.

- **`askback init <url>`** — probe the endpoint (the logic is already in
  `tools/probe_agent.py`), infer transport, request shape and response mapping, and write a
  config with the guesses marked as guesses. A wrong guess the user can see and correct is
  worth more than a questionnaire.
- **`askback scan <url>`** — generate a starter suite with no suite file at all: a handful of
  cases that need no domain knowledge, because they test behaviour every agent must have.
  Does it refuse a question its sources cannot answer. Does it remember turn 1 at turn 3. Does
  it fabricate under pressure. Does it hand over another user's data when asked nicely. Does it
  leak PII. Then run them and print the findings. No corpus, no golden answers, no config.
- **Suite generation from a source.** Given a corpus, a sitemap or a docs directory, write
  cases whose `required_facts` come from the source and whose `expected_status: no_answer`
  cases come from gaps in it. The HR example already demonstrates the shape; make it a command.
- **A thirty-second asciinema cast in the README**, of exactly this: a URL in, a real finding
  out. One recording is worth more than the README's first three tables.

Design constraint, non-negotiable: a generated suite must be marked as generated and must
never be presented as a measurement of correctness it cannot make. `scan` finds smoke, not
truth. Say so in its output, the way `askback eval` with no config already says so.

**Done when:** a stranger with a chatbot URL gets a real finding, or a clear statement of what
could not be measured, without opening an editor.

---

## Phase 3 — Where the users already are

*Cost: 2–3 weeks. Each item is a distribution channel, not a feature.*

- **A GitHub Action**, in its own repository, versioned `v1`:
  `uses: Raj-Purohith-Arjun/askback-action@v1`. A tool whose product is an exit code and whose
  README is about CI, that cannot be added to a workflow in three lines, is leaving its main
  channel unused.
- **A pytest plugin** (`pytest-askback`): one pytest test per case, failures as assertion
  output, `-k` for selection. This is DeepEval's whole wedge and it is cheap here — the runner
  and the result objects already exist. It also puts `askback` in the test run teams already
  have, rather than asking for a new one.
- **A stable programmatic API.** `askback.__all__` currently exports four names and no way to
  run an evaluation. Export one: a function that takes a config and a suite and returns a
  `RunReport`. People wrap tools they can import.
- **SARIF and JUnit XML output.** JUnit so every CI system shows per-case results natively.
  SARIF so findings land in GitHub's code-scanning UI, annotated on the pull request. SARIF
  in particular is a disproportionate amount of polish for a day of work.
- **A PR comment**: pass rate, new failures against baseline, and the named checks behind each
  one. The report already carries all of it; the job summary already renders it.
- **Evaluator plugin discovery.** A custom evaluator is registered by import today, so nobody
  can ship one as a package. Discovery through entry points is how domain expertise gets into
  the tool without domain knowledge getting into the core — and the loaded plugins must be named
  in the report context, or a run stops being reproducible.

**Done when:** adding `askback` to a repository is three lines of YAML, and its failures appear
where that repository's other failures appear.

---

## Phase 4 — The depth that makes it the strongest of its kind

*Cost: 6–10 weeks, and it should run after Phase 1, because most of it needs real traffic to be
built against. `docs/BACKLOG.md` already says so for several items; this phase is that backlog
with the blocker lifted.*

- **Citation support, not just existence.** Today a real chapter cited for an unrelated claim
  passes. Closing it needs the cited passage and an entailment judgement. It is the single
  biggest gap between what the citation check does and what a reader assumes it does.
- **Multi-document manifests**, roman-numeral and `Ch.` forms, and a warning when a
  sentence-case manifest silently disables the title check. A check that stops checking without
  saying so is the worst failure mode this project has, by its own invariants.
- **The clarification detector, calibrated.** Record near-miss scores so there is something to
  calibrate against, run PII and secret checks on intermediate clarifying questions, and render
  rounds in the HTML report. All three are in the backlog waiting on exactly the traffic Phase 1
  produces.
- **Statistical honesty.** `samples`/`min_passing_samples` already refuse to call one draw a
  measurement. Go further: confidence intervals on pass rates, and a gate that can distinguish
  "worse" from "noisier". Inspect AI's error bars are a large part of why serious evaluators
  take it seriously.
- **An adversarial generator.** Promptfoo's star count owes a great deal to shipping hundreds of
  attack vectors. The safety evaluators here are good and the inputs are hand-written. Generate
  them: injection, role confusion, authority framing, cross-tenant pivots, data-exfiltration
  phrasings — as a suite, under the same rule as Phase 2, marked as generated.
- **OpenTelemetry export**, so a run lands in whatever the team already watches.
- **A judge calibration report** as a first-class artifact: agreement with human labels, per
  dimension, with the threshold that follows from it. `askback calibrate` computes it; publish
  it for the shipped defaults, on real data, and let users compare theirs.

**Done when:** the Known Limitations section is shorter than it is today, and every line
remaining is there by decision rather than by deferral.

---

## Phase 5 — Ecosystem and launch

*Cost: 2 weeks, after the rest. Do not front-run it.*

- **An MCP server.** `askback` as a tool an assistant can call: run a suite, read a report,
  explain a failure. In 2026 this is both genuinely useful and one of the few remaining
  low-competition shelves to be on.
- **A documentation site** from `docs/` (mkdocs-material, published from CI), with the
  comparison page as a first-class entry: when to use this instead of DeepEval, Promptfoo,
  Ragas, Inspect — and when not to. Written fairly. A comparison that never recommends the
  alternative is read as marketing and discounted entirely.
- **The launch itself.** Mechanics, targets and the honest arithmetic of star counts are in
  [`.claude/LAUNCH.md`](../.claude/LAUNCH.md). The short version: the artifact that travels is
  the Phase 1 findings report, not the framework. Do not launch before Phase 1 is true.

---

## Non-goals

Named so they can be declined quickly rather than argued each time.

- **Model benchmarking.** Scoring a base model on MMLU-style tasks. That is `lm-evaluation-harness`
  and Inspect; it is a different job and this tool is worse at it.
- **Tracing and production observability.** Langfuse, Phoenix and the platforms own this.
  Exporting a run to them (Phase 4) is in scope; becoming one is not.
- **A hosted dashboard as the product.** The dashboard in `src/askback/api` is the HR example's
  viewer. Keeping the product a CLI and a library is a feature: it is the reason it can run
  inside a network that will never let a report leave.
- **A prompt playground.** Comparing prompts across models is Promptfoo's job.
- **A `packs/` extension mechanism**, until something actually discovers and loads packs.
  `examples/hr` is named that on purpose.
- **Agent frameworks, memory, or anything the agent under test is made of.** This repository's
  agent is a fixture.

---

## Version milestones

| Version | Means | Gate |
|---|---|---|
| **0.1.0** | installable; the demo is green from a plain install | Phase 0 |
| **0.2.0** | has met real agents; the limitations are measured, not assumed | Phase 1 |
| **0.3.0** | speaks streaming transports; a finding without writing a suite | Phase 2 |
| **0.4.0** | runs where CI and pytest already run | Phase 3 |
| **0.9.0** | citation support, calibrated detector, intervals, generated adversarial suites | Phase 4 |
| **1.0.0** | the config format and the Protocols are stable and documented as such; deprecation policy published | after 0.9 has survived outside contact |

1.0 is a promise about compatibility, nothing else. It is not a quality claim and should not be
reached for to signal one.
