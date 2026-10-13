# HR coupling: what is generic, what is HR, and what changed

How much of this repository assumes the agent is an HR assistant, and what kind of assumption
it is. This began as an audit of a repository whose only dataset, config, corpus and CI job
were HR. It is now a status report: each finding says whether it was fixed, and what is left.

Two kinds of coupling need different fixes:

- **(a) An example in the wrong place.** HR content that is genuinely sample data. Fix: move it
  under `examples/` and ship something neutral by default.
- **(b) Generic code with HR in its defaults.** The mechanism is general; a default, a vocabulary,
  a threshold or a name assumes HR. Fix: change the default or make it configurable. Moving
  files does not help.

**Summary.** The framework's logic is general: `src/askback/` has no HR branch, import or
lookup. What it ships to *run* is now neutral by default (a built-in demo agent and a small
suite), and the HR material lives under `examples/hr/` and is run only by asking for it. What
remains HR-shaped is **tuning**, not logic: some thresholds and the clarification detector's
weights and vocabulary were developed against HR text, and have run on little else.

## (b) Generic code with HR in its defaults

| # | Item | Status |
|---|---|---|
| 1 | **`pii_egress` `salary_figure`.** A pay word near an amount of 1,000 or more counted as a salary leak, always on and CRITICAL, with no per-category switch. Measured: "You paid $1,299 for the laptop" and "We will pay you $5,000 in compensation for the lost parcel" each failed with `salary_figure=1`. | **Fixed.** Categories are selectable (`evaluator_options.pii_egress.categories`); `salary_figure` is off by default; reports are still scrubbed of every category. Measured on the 101 HR cases: **0 verdicts change** with it off (81 of 101 pass either way). The HR example lists it explicitly. |
| 2 | **`config/app.yaml` was two configs in one**, and a bare `eval` ran the HR simulator. | **Fixed.** `config/askback.example.yaml` is the generic template; `examples/hr/config.yaml` is the simulator's. With no `--config`, `askback eval` runs the built-in demo. A `--config` path that does not exist is an error. |
| 3 | **Defaults keyed to shipped suites.** `default_suites` and `suite_must_pass` named the HR suites, so a user who deleted them tripped their own gate (exit 2). | **Fixed** for anything you copy: the template and the demo name no suite and set no floor. The HR example keeps its own, by name. |
| 4 | **Thresholds tuned on the simulator**: `llm_judge.threshold` 0.55 and `pass_threshold` 0.7. | **Open, documented.** The template keeps them with a comment. Calibrate per agent (`docs/calibration.md`). |
| 5 | **Clarification detector.** Signal weights were chosen against four HR replies and a greeting; the greeting vocabulary is English; `source_reference` matches "policy manual". | **Open.** Measured on 20 messages written before the vocabulary was run: 17 of 20 (the misses are greetings read as requests; 0 of 10 real requests were read as greetings). Not validated on any other domain. |
| 6 | **Citation resolver vocabulary.** A "document" is recognised by words like manual, handbook, policy, guide, code; only "Chapter N" is read. | **Open.** A docs assistant citing "API reference, section 3" is not seen. |
| 7 | **Naming.** The project was named for the example it began as. | **Fixed.** Distribution, import package, console script, environment prefix (`ASKBACK_*`) and base exception (`AskbackError`) are all `askback`. The simulator commands (`ask`, `chat`, `info`, `serve`) still live in the product CLI, but now refuse to run without `--config examples/hr/config.yaml`. |
| 8 | **Product code carries the simulator.** `src/askback/api/` (a dashboard) serves the simulator's corpus and intents, with HR sample prompts, and imports `askback_sim` lazily. | **Open.** Nothing in the evaluation path uses it, but it is simulator tooling inside the product package. Its titles were renamed. |
| 9 | **HR examples in product docstrings.** | **Fixed** in `src/askback/` (schema, redaction, PII, errors, calibration, the API docstring), reworded to neutral. |
| 10 | **`ChatResponse` is router- and RAG-shaped.** `route`, `intent`, `retrieved`, `citations` and `tool_calls` assume a multi-agent RAG system; an agent that returns only text gets most evaluators skipped. | **Open, by design** (invariant 8: skipped, never passed). The README's table says which evaluator needs which field. |

## (a) Examples

| Item | Status |
|---|---|
| The 101 HR cases (8 YAML suites, `routing.json`, `smoke.csv`), the 30-chunk corpus, the intent catalogue, the HR baseline, the judge-calibration records | **Moved** to `examples/hr/` (`datasets/`, `corpus/`, `intents.yaml`, `baselines/`, `calibration/`). |
| The simulator, `src/askback_sim/` | **Left in place, and excluded from the wheel.** Importable from a source checkout. |
| The findings about the simulator (previously a separate findings document) | Summarised in `examples/hr/README.md`. |
| CI | The PR gate is the built-in example suite, which is green. The HR simulator runs as a separate non-gating job labelled a demonstration. |
| `examples/toy-agent` | **Open.** A canned FAQ bot whose topics are leave, expenses and probation; it works, and reads as HR. |
| `examples/socketio-agent` | **Open.** Its behaviour is generic, but it reports `route: policy_agent` and cites `HR-001`. |
| `tests/` fixtures (an HR corpus in `conftest.py`) | Fine for a test suite. Not shipped. |

`examples/hr`, not `packs/hr`: a "pack" would imply an extension mechanism that discovers and
loads domain packs. None exists.

## What is generic

The schema and loaders; the `AgentAdapter` protocol and the HTTP and Socket.IO adapters; the
runner, sampling, redaction, baselines, regression and gates; `rule_based`, `rag`, `safety`
(tenant isolation, injection, secret leakage), `escalation`, `llm_judge` (its prompt has no domain
words), `citation_existence` (the structure) and the clarification loop (the mechanism). The
built-in demo agent and its suite, the config template and the probe contain no HR content, and a
test checks that for the demo files.

## Not checked

Non-HR behaviour has been observed only for the items measured above. The clarification detector,
the citation resolver and the heuristic judge have run against no non-HR text in this repository,
so "transfers" is a judgement from how they are built, not a measurement.
