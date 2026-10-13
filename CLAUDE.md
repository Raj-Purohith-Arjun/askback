# CLAUDE.md

Operating context for Claude Code in this repository. Read this first, every session.

---

## 1. What this project is

A **domain-general evaluation framework for conversational agents**, packaged as an
open-source Python library (`askback`). It drives a chat agent through test conversations,
grades the replies with layered evaluators, and gates CI on the result.

The framework's *logic* is general. What it ships to run is not all neutral: the large
example is an **HR assistant** under `examples/hr/`, built on a simulator (`src/askback_sim`),
and a few defaults were tuned against it. `docs/HR-COUPLING-AUDIT.md` is the itemised list,
and it is the thing to read before claiming this tool is, or is not, domain-general.

### What it is NOT
- Not an agent. The agent in this repo (the HR simulator) is a **test fixture**, not the product.
- Not a UI test tool. It calls backend APIs directly; UI automation is out of scope.

---

## 2. Where the project stands (read before judging the code)

This repo began as a single package containing **both** a working HR-style agent **and**
an evaluation harness that could only grade that agent in-process. They were split and an
adapter seam put between them.

| Package | What it is | Ships in the wheel? |
|---|---|---|
| `src/askback/` | the framework: the product | yes |
| `src/askback_sim/` | the HR example's simulator: a dev fixture | **no** (it is importable from a source checkout) |

`askback` never imports `askback_sim`. `tests/test_boundaries.py` enforces it twice: a
static scan of the source, and a subprocess in which `askback_sim` cannot be imported and
the framework must still import and run, including the default `askback eval`.

**Keep the evaluator vocabulary.** `Check` / `EvalResult` / `ChatResponse` /
`ResponseStatus` are good designs. Port them; do not redesign them.

### Two things `askback eval` can run
- **No `--config`**: the built-in demo. A canned stand-in agent (`src/askback/demo/`) graded
  against a small neutral suite that ships as package data. It passes (8 of 8), so a first run
  is green, and it works from a plain `pip install`. It is a self-check, never a result about a
  real agent, and the command says so.
- **`--config examples/hr/config.yaml`**: the HR simulator against 101 HR cases. It is **red on
  purpose**: 81 of 101 pass and 28 critical checks fail, which are the simulator's real flaws
  (`examples/hr/README.md`). Do not make it green by relaxing an expectation (invariant 9).

**Never present any of these numbers as results about a real agent: this framework has never
been run against one.**

---

## 3. Non-negotiable invariants

Treat these as tests that must never regress. If a change violates one, stop.

1. **Unmeasurable never passes.** No retrieved context -> `skipped`. No route field ->
   `error`. Never `pass`. A green run that silently skipped its grounding checks is
   worse than a red one.
2. **Critical severity gates the build.** Any failing CRITICAL check fails the run
   regardless of pass rate.
3. **No silent scoring-mode changes.** If the model judge falls back to the heuristic,
   that case is marked degraded and the run reports it. Never blend two scoring regimes
   into one mean.
4. **Reports are redacted by default.** `--redaction full` is local-only and must never
   be the CI default. Transcripts can carry personal, medical or financial data.
5. **The core imports no provider SDK.** Judges and adapters are injected behind
   Protocols. `openai`, `anthropic` etc. live in optional extras.
6. **The core has no domain knowledge.** Domain logic goes in an example or a suite, not in
   `src/askback/`. If you are about to write "leave" or "payroll" in
   `src/askback/core/`, you are in the wrong file. Honest status: true of code paths (no HR
   branch, import or lookup), and of the default config and suite, which are neutral. **Not
   fully true of tuning**: the judge threshold, the clarification detector's weights and
   vocabulary, and the citation resolver's idea of a "document" were developed against HR text.
   They are listed in `docs/HR-COUPLING-AUDIT.md`; do not add more, and do not describe the
   core as domain-free without that caveat.
7. **Non-determinism is measured, not assumed away.** Cases declare `samples` and
   `min_passing_samples`. One draw is not a measurement.
8. **Absent input is never evidence of success.** A check concludes "nothing wrong" only
   from data it actually received. When the thing it inspects is missing - a field the
   agent did not return, a mapping that dropped it, a table nobody filled - the check
   reports `skipped` or `error`, and a default must never stand in for the missing value.
   This is invariant 1 seen from the inside: 1 says what a report may claim, 8 says how
   a check must be written so it cannot claim it. We have shipped this bug three times -
   chunk metadata dropped by the HTTP mapping (every chunk read as the caller's own
   tenant), a status that defaulted to `ANSWERED`, and a side-effecting-tool list that is
   empty unless the simulator is loaded. Each time the check was correct and its input
   was not there. When you write a check, write the test where its input is absent first,
   and assert it does not pass.
9. **A verdict that changes is data, not a regression to undo.** Never adjust a detector,
   an evaluator, a threshold, a weight or a fixture in order to preserve an existing
   baseline or keep a case green. If a change moves a verdict, measure how many, decide on
   the merits whether the new verdict is the right one, and record the number you actually
   measured. A tweak made because a case flipped is a tweak made to the answer, not to the
   check; if one is justified, it needs a fixture that justifies it on its own.
   **A detector vocabulary that has to be extended a third time to keep a baseline green is
   evidence that the mechanism is wrong, not that the list is short. Escalate; do not
   extend.** Count so far: 2 (the `open_invitation` signal, then the presence-check words).
   The greeting vocabulary is now eight named categories in `evaluation/clarification.py`;
   extend it by deciding which kind of ritual a word belongs to, never by what broke. Measure
   any change on examples written before you run them (the first such measurement: 17 of 20,
   misses named in `tests/test_clarification.py`), and keep a known miss rate rather than
   chase 100%.
10. **A plain install must work.** `pip install` of the built wheel, then `askback eval` from
    an empty directory, passes. CI builds the wheel and does exactly that (`wheel` job), and
    checks the simulator is not in it. Anything the first command needs ships as package data.

---

## 4. Architecture

```
suite file --> loader --> EvaluationEngine --> AgentAdapter --> system under test
                               |                                (HTTP, Socket.IO, demo, or yours)
                               +-- rule evaluators     free, every commit
                               +-- RAG evaluators      needs retrieved context
                               +-- safety evaluators   gating
                               +-- judge evaluator     metered, optional
                               |
                          RunReport --> redaction --> JSON (canonical) --> HTML
                                                  \-> baseline comparison
```

Everything crossing a boundary is a `typing.Protocol`. An adapter or judge implements a
shape; it does not subclass framework base classes.

### Layout
```
src/askback/
  core/          contracts, errors, registry (mechanism + EVALUATORS), config, logging, text
  datasets/      TestCase schema + YAML/JSON/CSV/XLSX loaders
  adapters/      Protocol, HTTPAdapter, SocketIOAdapter, InProcessAdapter, ScriptedAdapter,
                 payload (shared request/response mapping), factory
  evaluation/    rule_based, rag, safety, pii, escalation, citation_eval, clarification,
                 llm_judge + judge_cache + calibration, engine, gates, regression, results
  providers/     LLMProvider Protocol, registry, openai_compat (any OpenAI-compatible server,
                 OpenAI, Azure OpenAI, Anthropic)
  runner/        TestRunner (concurrency, sampling, the clarification loop), responders
  reporting/     redaction (full / ci / metrics_only), then json (canonical), html, excel
  demo/          the built-in demo agent and its example suite (package data)
  api/           the example dashboard + HTTP surface (imports askback_sim lazily)
  cli.py         the `askback` command (imports askback_sim lazily)
src/askback_sim/    HR example simulator: agents, llm, retrieval, tools, safety, engine, memory
                 (NOT in the wheel)
tools/           probe_agent.py: a STANDALONE single file (imports nothing from this repo),
                 sent to whoever can reach an agent we cannot; keep it that way
config/          askback.example.yaml: the template to copy for your own agent
examples/hr/     the HR example: config, datasets (101 cases), corpus, intents, baseline, calibration
examples/        toy-agent (HTTP), socketio-agent (a Flask-SocketIO example with a login hook),
                 python-adapter, custom-evaluator
docs/            quickstart, first-suite, adapters, evaluators, calibration, cost,
                 HR-COUPLING-AUDIT.md, BACKLOG.md (deferred work and why)
```

`examples/hr` and not `packs/hr`: a "pack" would imply an extension mechanism (something that
discovers and loads domain packs) that does not exist. Do not create `packs/` without one.

---

## 5. Commands

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -c constraints.txt -e ".[all,dev]"      # `all` brings the extras the tests need

pytest --strict-env            # CI's form: a module skipped for a missing extra is an error
pytest -q                      # same tests, prints MODULES NOT TESTED instead of failing
ruff check src tests tools examples
mypy src                       # strict; must stay clean

askback eval                                          # the built-in demo: green
askback --config examples/hr/config.yaml eval         # the HR example: red on purpose
askback --config examples/toy-agent/config.yaml eval  # quickstart; --config goes BEFORE the subcommand
askback eval --suite NAME --tag TAG --case ID         # narrow a run (repeatable)
askback eval --compare-baseline                       # also gate on regression
askback baseline save                                 # promote the newest run
askback calibrate                                     # judge vs human labels
```

There is no `validate` command: a suite is checked when `eval` loads it, and a bad one is
exit `3`. Exit codes: `0` pass, `1` a case or gate failed (pass rate, a critical check, a
suite floor), `2` regression vs baseline, `3` the run could not be judged (bad config,
missing file, exhausted judge budget). A `--config` path that does not exist is an error;
it never falls back to a default.

---

## 6. Conventions

- Python >= 3.11 (the version floor is what has been run: 3.11 and 3.14).
  `from __future__ import annotations` everywhere.
- **`mypy --strict` must pass.** No new `# type: ignore` without a comment saying why.
  Strict covers every module under `src/`. There is no exemption list; do not start one -
  a module on such a list is a module dodging the gate. The only `[[tool.mypy.overrides]]`
  entry is for `openpyxl`, a third-party package with no stubs.
- Public seams are `Protocol`. Shared data is a dataclass or Pydantic model.
- Docstrings explain **why**, not what. The existing code does this well - match it.
- Comments earn their place: a comment that restates the line gets deleted in review.
- Never catch bare `Exception` except at a documented boundary (evaluator isolation,
  judge fallback), and always log with context.
- Secrets come from the environment via `${VAR}` references. Never from a config file,
  never a default value, never in a report. **Nothing reads a `.env` file**: do not tell a
  user to copy one.
- Tests use the `ScriptedAdapter`, not mocks. If you are patching `httpx`, you are
  testing the patch. A transport is tested over a real socket against a real server
  (`examples/socketio-agent`, `examples/toy-agent`, a stdlib HTTP server in
  `tests/test_providers.py`): a mock of a protocol we had never spoken would only prove we
  agree with ourselves.
- `tools/probe_agent.py` imports nothing from this repository and must keep passing the
  isolated-mode test in `tests/test_probe.py`. Do not "tidy" it into using `askback`.
- CI actions are pinned to full commit SHAs and dev tools to `constraints.txt`
  (`tests/test_workflow.py` checks). Change both together.
- Conventional commits.

---

## 7. Notes on the HR example (`examples/hr`)

What HR changes versus a generic support bot. These describe the example's suites, and are
useful guidance if you write HR suites of your own:

- **Hallucinated policy is the top risk.** Inventing an entitlement creates a liability
  the employer never agreed to. `expected_status: no_answer` is a gating check.
- **Numbers and dates are not paraphrasable.** "20 days", "90-day probation", a salary
  figure must match exactly. Semantic similarity is not sufficient - use `required_facts`.
- **Answers are person-scoped.** Leave balance, pay and performance differ per employee
  and per role (employee / manager / HRBP). Cross-principal isolation is suite #1.
- **Answers are version- and jurisdiction-scoped.** Without an effective date and a locale the
  golden set rots silently at the next policy cycle.
- **Escalation is a safety outcome.** Harassment, grievance, whistleblowing, discrimination
  and distress disclosures must route to a human. A bot that counsels instead of escalating
  is an incident. `expected_status: escalated`, severity critical.
- **PII egress is a gating failure**, not a score. The HR config turns on the `salary_figure`
  category; the shipped default leaves it off (it reads "you paid $1,299" as a salary).
- **Reports are audit artifacts.** HR automation attracts GDPR and works-council scrutiny.
  Runs must be reproducible: pin judge model, prompt version, dataset version, corpus hash.

---

## 8. Status and open work

`docs/BACKLOG.md` is the list of deferred work, each with the reason it waits. The common
reason is that it needs traffic from a real system to be built against.

### Blocked on a real agent
The adapters' code is done and its tests pass, but only against the demo agent, the simulator
and our own example servers. Before pointing an adapter at a real system, find out:
1. Does the API expose the resolved sub-agent / route per turn?
2. Does it return the retrieved chunks and citations?
3. Is there a stable session or conversation id for multi-turn?
4. Can we get two or more test identities with separate credentials and different entitlements?
5. How does a reply end (a terminator event, or a quiet period), and what is the slowest
   observed reply?
6. How does authentication work, how long does a token live, and are there rate limits?

`tools/probe_agent.py` turns most of these into a recorded transcript. Say so in status
updates - never present demo or simulator numbers as results about the real system.

---

## 9. Working agreements for Claude Code

- Before editing, read the module's docstring. Several encode deliberate decisions.
- Change one seam at a time. Do not refactor across seams opportunistically.
- When a change is done, run `pytest --strict-env && ruff check src tests tools examples &&
  mypy src` before saying it is done.
- Do not add a dependency without saying which extra it belongs to and why the stdlib
  will not do. The dependency surface is a selling point.
- If an instruction here conflicts with what the code does, flag the conflict rather than
  silently picking one.

---

## 10. Working mode

- Do NOT produce a plan for approval unless the prompt says "plan first". This
  overrides any global instruction to plan before editing. Most steps here are
  fully specified; the plan step is reserved for work with real unknowns.
- Commit each completed step yourself, conventional-commit style, with the
  Co-Authored-By trailer. This overrides any global instruction never to commit.
  Do not commit a step that is failing its own verification.
- Report "blockers only" rather than an exhaustive edge-case list: things that
  are wrong, unsafe, or that the next step depends on. Curiosities can wait.
- Tests should be the ones that would catch a real regression, not an exhaustive
  matrix. Roughly 0.8 test lines per source line is the target; we are below it (about 0.5,
  counted with `wc -l` over `tests/` and `src/`). Transports are tested over real sockets.
