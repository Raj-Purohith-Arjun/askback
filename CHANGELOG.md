# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may break a
config key, an endpoint or a report field, and when it does the entry says so.

## [0.1.0] - unreleased

The first release. It starts from a single package that held an HR-style agent and an
evaluation harness that could only grade that agent in process, and ends with a
framework that grades any agent through an adapter, plus the simulator it was built
against. Everything below is relative to that starting point.

### Release notes: do these before you rely on it

Two operational items. Neither is a code change you have to make; both are clean-up that
the release does not do for you.

**1. Delete `.cache/` before pointing the nightly at a real agent.**
Until this release the judge's verdict cache stored the model's one-line *rationale* next
to its scores, and the rationale quotes the reply it graded. The cache now stores scores
only, and entries written before still carry a rationale on disk. They are never read, and
nothing scrubs them. Everything under the cache directory (`.cache/judge` by default, or
`evaluation.judge.cache_path`) was written by the old code, so remove it, on every
machine that ran a model judge:

```bash
rm -rf .cache                 # Windows: rmdir /s /q .cache
```

It costs one re-paid run of verdicts. A cache written by the nightly is restorable by
pull-request workflows, including from forks, which is why this matters before the
nightly grades anything real.

**2. Delete the `judge-verdicts-*` caches from GitHub's store; do not wait for expiry.**
The nightly's cache key was renamed from `judge-verdicts-` to `judge-scores-`, so the
current workflow never restores the old entries. They still exist, though, and a workflow
file from an old branch can still name them. With the GitHub CLI, as someone allowed to
delete caches on the repository (the `repo` scope):

```bash
gh cache list --key judge-verdicts- --limit 100
gh cache list --key judge-verdicts- --limit 100 --json id --jq '.[].id' | xargs -n1 gh cache delete
```

`gh cache list` shows what is there; the second command removes it. Repeat if the list
was longer than 100. Or use the repository's **Actions → Caches** page.

### Changed (breaking)

- **`askback eval` with no `--config` no longer runs the HR simulator.** It runs the built-in
  demo. The HR example moved to `examples/hr/` (config, 101 cases, corpus, intents, baseline,
  calibration records) and is run with `--config examples/hr/config.yaml`. It is red on purpose.
- **`--config` pointing at a file that does not exist is an error.** It used to fall back to an
  empty config. The simulator commands (`ask`, `chat`, `info`, `serve`) need `--config`.
- **`config/app.yaml` is gone.** Its generic half is `config/askback.example.yaml`; the
  simulator's half is `examples/hr/config.yaml`. No suite floors and no `default_suites` ship in
  anything you are meant to copy.
- **`salary_figure` is off by default** in `pii_egress`. It read a pay word near an amount of
  1,000 or more as a salary, and so failed ordinary text such as "you paid $1,299" as CRITICAL.
  The HR example turns it on. Reports are still scrubbed of every category.
- **`askback_sim` is not in the wheel.** It is the HR example's simulator, a development
  fixture. It is importable from a source checkout. The empty `sim` extra is removed.
- **Python 3.11 or newer.** That is what has been run (3.11 and 3.14).
- **Anthropic has no default model**, and an OpenAI-compatible server at a custom `base_url` has
  none either: name the model.
- **`calibrate` has no default `--records`**; set `evaluation.calibration_records` or pass it.
- The `Dockerfile`, `docs/PLAN.md`, `ROADMAP.md` and `docs/FINDINGS.md` are removed. The findings
  about the simulator are summarised in `examples/hr/README.md`.
- **A test case is a conversation.** `TestCase` holds who is asking and a list of
  `Turn`s, each with its own `Expectations`. Suite files declare `schema_version: 2`;
  a file without it is refused with the command that converts it
  (`scripts/migrate_v1_v2.py`). A suite-level `defaults:` block merges under every
  turn's `expect`, and an unknown key at any level is refused. `Evaluator.evaluate` and
  `EvaluationEngine.evaluate` take the turn; `Judge.score` takes the turn instead of the
  case.
- **Exit codes** are `0` pass, `1` a gate failed, `2` regression against the baseline,
  `3` the run could not be judged. Previously `2` meant an operator error.
- **The baseline is a summary, not a report.** Each case's verdict, score, route and
  status, the run's totals and its context, one case per line. Re-save yours with
  `askback baseline save`. A baseline file in the old whole-report form still loads, but it
  is far larger and keeps reply text, so replace it.
- **Reports are redacted by default** (`ci`): PII and credential matches replaced by their
  category, text cut to 200 characters, retrieved passages withheld. `--redaction full`
  keeps everything and is refused when `CI` is set. Use `metrics_only` for regulated data.
- **The package split into `askback` and `askback_sim`.** The framework is `askback`; the
  simulator is `askback_sim`. `askback` never imports `askback_sim`, enforced by a test. Run
  it with `python -m askback` or the `askback` console script.
- **The dashboard can no longer write anything.** The routes that rewrote the corpus
  (`PUT`/`DELETE /api/corpus/{id}`), the intent catalogue (`PUT /api/routing/intents`) and
  the baseline (`POST /api/eval/baseline`) are deleted and return 404 or 405, whatever the
  configuration. Edit the files on disk and reload. Promote a baseline with
  `askback baseline save`, where it leaves a file to commit. CORS no longer allows `PUT`
  or `DELETE`, and the Knowledge and Routing panels are read-only views.
- **`server.enable_editor` is now `server.allow_runtime_reload`**, environment variable
  `ASKBACK_ALLOW_RUNTIME_RELOAD`, and defaults to **off**. It guards the one route that is
  left, `POST /api/system/reload`. The old key and `ASKBACK_ENABLE_EDITOR` are ignored, so
  an instance that set them to `true` loses its Reload button until the new one is set.
  `/api/system/info` returns `reload_allowed` in place of `editable`.
- **The judge's verdict cache stores scores only.** A cache hit reads "Rationale
  unavailable: cached verdict, scores only." in the check detail.
- **A failing critical check fails the run at any pass rate.** `askback eval` used to gate
  on pass rate alone, so a run with six failing critical checks exited 0. Per-suite floors
  (`evaluation.suite_must_pass`) are checked on their own, and a floor on a suite that did
  not run is an error, not a pass.
- **A judge failure is no longer absorbed.** A failed model call is graded by the heuristic
  for that response, marked degraded, counted, and never cached; `max_degraded` (default 0)
  fails the run when more were degraded, exit `3`. Previously the fallback was silent.

### Added

- **Clarifying questions.** A reply that is a question is no longer graded as an answer.
  A free, deterministic detector scores every reply from weighted signals and records the
  score and signals on the result; the runner answers (scripted answers, then an optional
  model-backed persona limited to a closed fact sheet) up to `max_rounds` and grades the
  last reply. Schema version 3 adds a per-turn `clarification` block (`policy: allow |
  forbid | require`) and a case-level `persona`; v2 files load unchanged. A turn still
  asking after `max_rounds` is an error; a turn we had nothing to say to is *unmeasured*:
  it cannot pass and is counted on its own. The persona's tokens are reported apart from
  the judge's. A reply that invites a query is not a clarification when the user's own
  message was only a greeting (`detection.suppressed` shows when that applied). See
  `config/askback.example.yaml`, `evaluation.clarification`.
- **Socket.IO adapter** (`agent.adapter: socketio`, extra `socketio`). Speaks the real
  protocol through `python-socketio`; event names, namespace and payload shape are config.
  One connection per session, reused for every turn and never reopened; a turn is never
  sent twice, whatever fails after the emit. Replies are assembled from classified events
  (content, metadata, terminator, error), complete at a terminator or an explicit idle
  timeout. A pluggable login hook (`CachedTokenAuth`) renews tokens, and a sliding-window
  rate limiter holds the run under the agent's limits. Tested against a real
  Flask-SocketIO server (`examples/socketio-agent`), extra `socketio-server`.
- **Citation existence** (`expect.verify_citations`, evaluator `citation_existence`). Reads
  chapter, page and document citations out of the reply's prose and resolves them against a
  hand-kept manifest of the source document (`evaluation.citations.manifest`): a chapter that
  does not exist, a page outside its chapter or beyond the document, a real chapter under the
  wrong title, an unknown document. It proves the agent pointed somewhere real, not that the
  passage supports the claim. With no manifest, or no readable citation, it skips visibly and
  never passes.
- **`tools/probe_agent.py`**, a standalone single-file wire-format probe (extra `probe`) for
  agents you cannot reach yourself. It imports nothing from this package.
- **A built-in demo.** `askback eval` with no `--config` grades a small canned demo agent
  against a bundled neutral example suite, both shipped as package data, and passes. A plain
  `pip install` therefore has a working first command.
- **`config/askback.example.yaml`**, the template to copy for your own agent. It names no
  suite and sets no floor.
- **`askback --version`**, printing the one version the package and its distribution
  metadata both read.
- **Any OpenAI-compatible server as the judge or persona**, with no API key needed
  (`base_url`, or `JUDGE_OPENAI_BASE_URL` / `JUDGE_OPENAI_MODEL`).
- **`pii_egress` categories** are selectable (`evaluator_options.pii_egress.categories`).

- **Adapters.** The `AgentAdapter` protocol and three implementations: `HTTPAdapter`
  (a request template, per-principal headers or body fields, dotted response paths,
  bounded retries that honour `Retry-After`, one pooled client per run), `InProcessAdapter`
  (the simulator), and `ScriptedAdapter` (canned replies, for testing your own evaluators).
  `agent.adapter` in the config chooses one. A status an agent does not report is
  `ResponseStatus.UNKNOWN`, never `answered`.
- **Multi-turn conversations.** All of a case's turns run in order under one session;
  setup turns marked `evaluate: false` are sent and not graded.
- **Sampling.** `samples` and `min_passing_samples` run a case N times in fresh sessions
  and require K passes; a case that passed some and not all is marked flaky.
  `evaluation.default_samples` sets the default.
- **Judge hardening.** An on-disk verdict cache, token and cost accounting from
  `price_per_1k_prompt` and `price_per_1k_completion`, a `budget_usd` ceiling checked
  between cases (exit `3`, no verdict), and a judge built from its own `JUDGE_`-prefixed
  configuration so it never grades with the agent's model.
- **`askback calibrate`.** Measures the judge's agreement with human labels (Cohen's
  kappa, overall and per dimension) and exits `1` below `min_kappa` (default 0.6).
  `examples/hr/calibration/seed.yaml` holds ten **synthetic** records, which make the command testable
  and validate nothing.
- **Safety evaluators.** `pii_egress` (email, phone, national id, card, IBAN, salary
  figure in a reply, always on, records categories and never the matched text) and
  `escalation` (an escalated turn must show a ticket reference or queue name). Both are
  zero-weight gates that veto through critical checks.
- **`isolation` and `escalation` suites**, including a negative control so that a bot
  that refuses everything cannot pass every isolation case.
- **`agent.side_effecting_tools`**, so a run against a deployed agent can declare which of
  its tools act on the world. Without it the tool-misuse check fails every case.
- **Reporting.** A redaction layer in front of every writer, a lossless JSON round trip,
  per-suite baseline deltas split into newly failing and newly passing, and a `md`
  one-page run summary.
- **CI.** `.github/workflows/evaluate.yml`: checks on every pull request, a deterministic
  evaluation for same-repository pull requests, a notice instead of an evaluation for
  forks, and a nightly model-judge run under a spending ceiling. It never uses
  `pull_request_target`.
- **Release files and documentation.** Apache-2.0 `LICENSE` and `NOTICE`, `SECURITY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, a rewritten README, a quickstart, guides to
  writing a suite, an adapter and an evaluator, a cost page, and runnable examples under
  `examples/`. `askback` ships a `py.typed` marker, and the package declares its
  licence.
- **Test-suite integrity.** `--strict-env` turns a test module skipped for a missing
  optional dependency into an error, and `tests/expected_tests.json` records the
  collection floor, so a module that stops being collected breaks the run.
- `mypy --strict` over every module under `src/`, with no exemption list.

### Fixed
- A turn on which every evaluator skipped scored 1.0 and passed. Unmeasured never passes.
- `evaluator_options` with a block whose settings were all commented out (YAML `null`) crashed the
  engine.
- A tenant-isolation check that could not fail over HTTP: the adapter dropped chunk
  metadata, so every chunk read as the caller's own tenant.
- A tool-misuse check that passed whenever the simulator was not loaded. It now fails
  critical when no tool declaration exists.
- `agent.side_effecting_tools` was refused by the HTTP adapter's own key check, so no run
  against a deployed agent could declare its tools and none could pass the tool-misuse
  check. It is now accepted.
- Every phrase pattern in the harness matched a single literal space, so a hard wrap inside a
  phrase silently removed that signal: a clarifying question scored 1.15 instead of 0.90, an
  answer -0.3 instead of -0.6, and a credential or an IBAN split across two lines was not
  found at all. Patterns are now compiled through `core.text.tolerate_line_breaks`, citation
  parsing undoes hard wraps first, and `tests/test_wrapping.py` re-wraps every fixture at four
  widths and requires an identical verdict. Measured on the HR example: 0 verdicts change.
- A claimed chapter title is read only when it is title-cased, so a colon that introduces
  prose ("Chapter 5:\nStaff must not remove ...") is no longer read as a wrong title.
- A suite floor breach named no cases under `--quiet`.
- An isolation case that expected a refusal for a correct answer (`iso-wrong-tenant`) is
  now a negative control.

### Removed

- The `PUT`, `DELETE` and `POST` dashboard routes listed under *Changed*, the corpus
  store's write path, and the dashboard's save, delete and promote controls.
- The v1 single-message suite format.

### Security

- The judge verdict cache no longer persists the model's rationale, which quotes the reply
  it graded (see the release notes above).
- The dashboard has no route that writes the corpus, the intent catalogue or the baseline.
  An instance that anyone could reach could previously rewrite all three.
- Reports are redacted by default, and the PII gate records categories and counts only.
- Every fixture, corpus, suite and calibration record in the repository is invented and names
  no real organisation or person (see `NOTICE`). The test canaries for the secret-leak checks
  are deliberately key-shaped; they are not keys.

### Known limitations

- **Never run against a real agent.** Every number in this repository describes the built-in
  demo agent, the HR simulator or an example server. `askback --config
  examples/hr/config.yaml eval` exits `1` with 28 critical failures: those are findings about
  the simulator (see `examples/hr/README.md`), not defects to close by editing a dataset.
- `.github/workflows/evaluate.yml` has been checked as a file (`tests/test_workflow.py`) and its
  commands run locally, but it has never run on GitHub. Confirm a real run before relying on it.
- Python 3.11 and 3.14 are what has been run; nothing else is claimed.
- The clarification detector's weights, the greeting vocabulary and the citation resolver's
  vocabulary were developed against HR text and a few real replies, and have run on little else.
- A chapter title claimed in sentence case is not read as a claimed title, so a manifest whose
  titles are written that way has its titles left unchecked. The chapter, page and document
  checks still run.
- Custom evaluators are registered by import. There is no plugin discovery yet.
- The calibration set (`examples/hr/calibration/seed.yaml`) is synthetic. A model judge's
  agreement with people who own the answers has not been measured.
