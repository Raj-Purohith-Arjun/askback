# PLAN.md

The execution plan. One step at a time, in order, each one shippable on its own.

`docs/ROADMAP.md` says what and why, for the public. This file says exactly what to change and
what command proves it. Claude Code reads this file to pick up work; see `.claude/commands/`.

---

## How a step works

Every step below has the same five parts, and the contract is the same every time:

- **Why** — the reason it is worth doing. If this stops being true, delete the step.
- **Touches** — the files it is allowed to change. A step that needs to change a file outside
  its list has been mis-scoped: stop and say so rather than widening it.
- **Do** — the change.
- **Done when** — a command, or an observation a human can make. Not "it looks right".
- **Watch** — the invariant or trap this step is near. `CLAUDE.md` §3 holds the invariants;
  numbers here refer to those.

Rules for whoever is executing, human or model:

1. **One step per commit**, conventional-commit style, with the `Co-Authored-By` trailer.
2. **The gate runs before the commit, every time**: `ruff check src tests tools examples &&
   mypy src && pytest --strict-env`. A step that fails its own gate is not committed.
3. **Status lives in this file.** Mark a step `[x]` in the index when its commit lands *and its
   Done-when has been observed*, with the short sha. A plan whose status is in someone's head is
   not a plan. If the commit has landed but the Done-when cannot yet be observed, the step is
   `[~]`, not `[x]` (rule 7).
4. **A step that turns out to be wrong gets struck, with the reason** — not silently dropped.
   The reason is the most valuable line in the file.
5. **Never relax an expectation to make a step pass** (invariant 9). If a step's change moves
   a verdict, measure how many moved, decide on the merits, and record the number.
6. **Do not do two steps in one commit** because they are both small. The point of the
   sequence is that any one of them can be reverted alone.
7. **`[~]` means: work landed, verification blocked externally.** The commit or commits are in,
   the gate passed, and the step's Done-when cannot be observed until something outside the
   repository happens (a registry accepting a publisher, a release being published, a third
   party responding). The index line for a `[~]` step must carry the shas that landed *and* the
   specific external event it is waiting on. Three limits, so the marker cannot become a place
   to hide:
   - `[~]` is never `[x]`. It becomes `[x]` only when the Done-when is observed, never on the
     expectation that it will be. Unverified never passes (invariant 8, applied to the plan).
   - It is not a parking state for a step whose gate failed. A failing gate means no commit
     (rule 2), so there is nothing to mark.
   - Later steps may proceed past a `[~]` step only if they do not depend on its Done-when.
     If one does, say so on that step.

---

## Index

Markers: `[ ]` not done · `[~]` work landed, verification blocked externally (rule 7) · `[x]`
done and observed.

Phase 0 — a stranger can install it
- [~] `0.1` Publish to PyPI through trusted publishing — **merged with 0.2 and done FIRST** (see 0.1). Landed in `179eb7d`, `a8772fd`. Waiting on: tag v0.1.0 pushed and a GitHub release published (which fires the pypi job in release.yml). The trusted publisher is registered and verified on pypi.org. Done-when (`pip install askback` from a fresh container) cannot be observed until the package exists on PyPI.
- [ ] `0.2` Tag v0.1.0 and cut the release — **merged into 0.1**; one release, one pass
- [ ] `0.2b` Make CI honest: checks on push, no masked failures, a nightly that says it did not run — **jumped the queue ahead of 0.4** (see 0.2b)
- [ ] `0.3` Widen the Python matrix to what actually works
- [ ] `0.4` Badges and repository metadata
- [ ] `0.5` Issue and PR templates, CODEOWNERS
- [ ] `0.6` Dependabot, CodeQL, OpenSSF Scorecard
- [ ] `0.7` Coverage measured, floor set, ratchet documented
- [ ] `0.8` README: install first, clone second

Phase 1 — first contact with a real agent
- [ ] `1.1` Pick five real agents and record the shortlist
- [ ] `1.2` Validation harness and the shape of a validation record
- [ ] `1.3`–`1.7` One real agent each (repeat the same step five times)
- [ ] `1.8` Fix what contact broke, measuring each verdict change
- [ ] `1.9` Rewrite the README Status table against the evidence
- [ ] `1.10` Publish the findings report

Phase 2 — a finding in sixty seconds
- [ ] `2.0` Streaming transports: SSE, WebSocket, streaming HTTP  ← **sized by Phase 1**
- [ ] `2.1` `askback init <url>`: a config from a probe, with the guesses marked
- [ ] `2.2` The universal smoke suite (no domain knowledge required)
- [ ] `2.3` `askback scan <url>`: init + smoke + run, in one command
- [ ] `2.4` Suite generation from a corpus or docs directory
- [ ] `2.5` The thirty-second cast in the README

Phase 3 — where the users already are
- [ ] `3.1` A stable programmatic API
- [ ] `3.2` `pytest-askback`
- [ ] `3.3` JUnit XML output
- [ ] `3.4` SARIF output
- [ ] `3.5` The GitHub Action (separate repository)
- [ ] `3.6` The pull-request comment
- [ ] `3.7` Evaluator plugin discovery (entry points)

Phase 4 — depth (needs Phase 1 first; see `docs/BACKLOG.md`)
- [ ] `4.1` Citation support, not just existence
- [ ] `4.2` Manifest formats, and a warning when the title check goes silent
- [ ] `4.3` Clarification detector: record near misses, grade intermediate turns
- [ ] `4.4` Confidence intervals, and a gate that knows noise from regression
- [ ] `4.5` The adversarial generator
- [ ] `4.6` OpenTelemetry export
- [ ] `4.7` A published judge calibration report

Phase 5 — ecosystem
- [ ] `5.1` The MCP server
- [ ] `5.2` The documentation site
- [ ] `5.3` The comparison page
- [ ] `5.4` Launch (see `.claude/LAUNCH.md`)

---

# Phase 0 — a stranger can install it

## 0.1 Publish to PyPI through trusted publishing

**Why.** `pip install askback` currently fails. Every table, badge and launch post downstream
of this assumes it works. The name is unclaimed as of this writing, which is a standing risk:
names in this field go quickly.

**Touches.** `.github/workflows/release.yml` (new), `pyproject.toml`, `CHANGELOG.md`,
`MANIFEST.in`.

**Scope correction, 2026-11-21.** `MANIFEST.in` was **added to Touches after the fact**. The
original list omitted it, but the sdist decision below depends on it (`MANIFEST.in` is what
prunes `tests/`), so the step was mis-scoped as written. It is corrected here, visibly, rather
than silently; by this plan's own rule, a step that needs a file outside its list has been
mis-scoped, and this one was.

**Do.**
- New workflow, triggered on a published release, with `permissions: id-token: write` and
  nothing else. Build with `python -m build`, publish with `pypa/gh-action-pypi-publish`
  pinned to a commit sha, attestations on. **No API token anywhere** — configure the PyPI
  publisher as the trusted publisher for this repository and workflow.
- A `test-pypi` dry run on `workflow_dispatch` before the real one.
- Verify the built sdist as well as the wheel: the existing `wheel` job checks the wheel
  carries `demo/suites/demo.yaml` and does not carry `askback_sim`. The sdist's contents are
  not checked at all today; add it or state that the sdist is unsupported.
- `pyproject.toml` has a `PLACEHOLDER` comment on `authors`. Resolve it before the first
  upload; metadata on PyPI is not editable after the fact.

**Reordered, 2026-11-21.** 0.1 and 0.2 are merged and released *now*, before 0.3–0.8. Reason:
the badges in 0.4 and the README rewrite in 0.8 both point at a package that has to be live, and
a pending PyPI publisher does not reserve the name (PyPI says so), so the window between
registering and publishing is a standing risk. The release is v0.1.0 as it stands: no version
bump. 0.3–0.8 then ship as v0.1.1 or later, with a real install to point at.

**Sdist contents** (decided in this step, frozen per version). The test suite is deliberately
absent from the sdist: it tests the repository (examples, config, `.github`, the simulator), and
with every test file included the extracted sdist failed at collection. `MANIFEST.in` prunes
`tests/`; `release.yml` fails the build if a `tests/` path appears. Run the tests from a clone.

**Done when.** In a fresh container with no checkout:
`pip install askback && cd $(mktemp -d) && askback eval` exits 0.

**Watch.** Invariant 10. The release workflow must not be the only thing proving a plain
install works — the existing `wheel` job stays on every pull request.

## 0.2 Tag v0.1.0 and cut the release

**Why.** No tags exist. A repository with no release reads as unfinished whatever the code says,
and `pip install askback==` has nothing to resolve.

**Touches.** `CHANGELOG.md`, a git tag, the GitHub release.

**Do.** Tag `v0.1.0` at the commit that passed 0.1's check. Release notes are the CHANGELOG
section verbatim, plus the two sentences the README's Status table leads with: never run against
a real agent, the first run is green and means little. Leading with the caveat in a release note
is unusual and it is the correct call here; it is also the thing that makes the Phase 1
announcement land.

**Done when.** `pip install askback==0.1.0` works, and the release page states the limitation.

## 0.2b Make CI honest before a badge points at it

**Why.** 0.4 adds a CI badge, and the badge as it stood was green while nothing ran. Found by
reading all 39 runs of `evaluate.yml` (`docs/` has no copy; the finding is summarised here):
`checks` was pull-request-only, so the 769-test suite had never run on GitHub; the one job that
did run on a schedule, `nightly`, failed at "Switch the model judge on" on every run and
`continue-on-error: true` turned that into a successful workflow run; and 14 runs never started
at all (an account billing lock) yet still read green. That is invariants 1 and 8 broken in our
own CI. It jumped the queue because a badge over a CI that cannot fail is worse than no badge.

**Touches.** `.github/workflows/evaluate.yml`, `tests/test_workflow.py`, `README.md` (the CI
paragraph and the badge URL), `.claude/PLAN.md`, `docs/HR-COUPLING-AUDIT.md` (one row).

**Do.**
- `checks`, `wheel` and `eval` also run on a push to `main`.
- Remove `continue-on-error` from `nightly`. Exit 1 (the simulator's known findings) is reported
  inside the step and does not fail the job; any other exit, or no report, is red.
- Remove `continue-on-error` from `demo-hr` too, with the same explicit handling: exit 1 (red by
  design, 28 critical findings) passes **only if a report exists**; exit 2 (regression against
  the committed baseline), exit 3 (could not be judged) and exit 0 (a deliberately red example
  turned green: a check stopped checking) all fail. No job in the file may carry
  `continue-on-error`, and a test says so.
- The nightly runs only when `vars.JUDGE_PROVIDER` is set. When it is not, `nightly-skipped` runs
  instead and says, in its name, an annotation and the run summary, that nothing was graded and
  why. A half-configured judge (provider set, prices missing) still fails loudly in
  "Switch the model judge on".
- The README badge is pinned to `branch=main&event=push`, so the scheduled runs cannot colour it.
- The workflow header states the rule in terms of invariants 1 and 8, and a test fails if a job
  other than `demo-hr` gets `continue-on-error`.

**Done when.** A pull request's `checks` jobs ran on GitHub and are green on their own merits
(or red for a real reason, recorded), and a run of the nightly with `JUDGE_PROVIDER` unset shows
`nightly-skipped`, not a green `nightly`.

**Watch.** The billing lock is the owner's, not this step's: do not "fix" it in the repository.
`demo-hr` is now a gate on pull requests (regression, unjudgeable, unexpected green). It is the
job most likely to fail for a reason in the PR rather than in the code: read its summary before
assuming the change is wrong. **Scope correction:** `docs/HR-COUPLING-AUDIT.md` was added to
Touches after the fact, because its CI row called this job non-gating, which is no longer true.

## 0.3 Widen the Python matrix to what actually works

**Why.** CI tests 3.11 and 3.14. `requires-python` admits `>=3.11`, so 3.12 and 3.13 are
claimed and untested — and the suite passes on 3.13 today, measured. Most users are on one of
those two. Either test them or narrow the claim; a claim you have not run is the kind of thing
this repository's own documentation refuses to make elsewhere.

**Touches.** `.github/workflows/evaluate.yml`, `pyproject.toml` (classifiers), `README.md`,
`CONTRIBUTING.md`, `CLAUDE.md` §6, `docs/ROADMAP.md`.

**Scope correction, 2026-11-21.** `docs/ROADMAP.md` was **added to Touches after the fact**.
Its Phase 0 bullet stated that CI tests only 3.11 and 3.14, which this step makes false, so the
original list was incomplete. Two stale-but-true mentions were deliberately left: the
`pyproject.toml` comment and `constraints.txt` both record what has actually been run, and are
updated when four green jobs exist, not before. The `CHANGELOG.md` 0.1.0 entries are historical
and stay as written.

**Do.** Matrix `["3.11", "3.12", "3.13", "3.14"]`. Add the classifiers. Correct every sentence
that says "3.11 and 3.14 are what CI runs" — there are several, in README, CONTRIBUTING and
CLAUDE.md, and they must move together.

**Done when.** Four green `checks` jobs, and no prose left claiming a version that is untested.

**Watch.** `tests/test_workflow.py` asserts things about this workflow. Run it.

## 0.4 Badges and repository metadata

**Why.** The decision to read a README is made above its first paragraph. Topics are how this
repository gets found by someone who did not come looking for it; a social preview image is
most of the difference in how a shared link converts.

**Touches.** `README.md`, `docs/ROADMAP.md` (the one-line wedge), repository settings (manual),
`.github/` (image asset).

**Do.**
- Badges, under the title, that are all live checks: PyPI version, Python versions, CI status,
  licence, coverage. No "awesome" badges, no star-count badge.
- One sentence before the badges, not after. The current opening paragraph is good and long;
  put a single-line version above it.
- Topics: `llm-evaluation`, `agent-evaluation`, `conversational-ai`, `chatbot-testing`,
  `rag-evaluation`, `llm-testing`, `ci`, `python`, `llmops`, `red-teaming`, `multi-turn`.
- Description: the wedge in one line, under 120 characters so it is not truncated in search
  results. **That line does not exist yet.** `docs/ROADMAP.md` carries the wedge as a
  two-sentence blockquote, not a one-liner, so write the line first, in this step, into
  `docs/ROADMAP.md` beside the blockquote, and use that exact text for the repository
  description, the single line above the badges, and the social preview image. One line, one
  home; check its length with a command, not by eye.
- A social preview image, 1280×640. Plain: name, the one-line wedge, a terminal fragment
  showing a real failure. Not a logo exercise.

**Done when.** A link to the repository renders with a preview image, and the description
states what the tool does without the reader opening it.

## 0.5 Issue and PR templates, CODEOWNERS

**Why.** The first external report will be a wire-format mismatch — someone's agent whose
response the adapter cannot map. That report is worth a great deal and is useless without the
transcript. Ask for it in the template.

**Touches.** `.github/ISSUE_TEMPLATE/*.yml`, `.github/pull_request_template.md`,
`.github/CODEOWNERS`.

**Do.**
- Issue forms: **bug**; **wire-format mismatch** (which asks for `probe-result.json`, with the
  README's warning that it contains the agent's reply and should be read first);
  **evaluator proposal** (which asks what the check would report when its input is absent —
  invariant 8, asked at the door); **documentation**.
- A PR template whose checklist is the gate commands and the invariants a change must not
  break, linking `CONTRIBUTING.md` rather than restating it.
- `CODEOWNERS`, so review is assigned rather than hoped for.

**Done when.** Opening an issue offers the four forms, and the mismatch form asks for a probe
transcript.

## 0.6 Dependabot, CodeQL, OpenSSF Scorecard

**Why.** Actions are pinned to shas, which is correct and means nothing updates unless
something updates it. The buyers this tool is aimed at — the ones who care about redaction
modes and a dependency surface of one package — check Scorecard and code scanning before they
check features.

**Touches.** `.github/dependabot.yml`, `.github/workflows/codeql.yml`,
`.github/workflows/scorecard.yml`, `README.md` (badge).

**Do.** Dependabot for `pip` and `github-actions`, grouped, weekly. CodeQL on pull requests
and a schedule. Scorecard with its badge published. All new actions pinned to full shas with
the release as a comment, per `CLAUDE.md` §6, and `constraints.txt` updated in the same commit
when a dev tool moves.

**Done when.** `tests/test_workflow.py` still passes with the new workflows present, the
Scorecard badge is live, and Dependabot's first pull request is a sha bump with the comment
updated.

**Carried over from 0.1 (deliberately deferred, do not lose these).**
- Pin `build` and `twine` in `release.yml`'s build job. They are installed unpinned today,
  as in the pull-request `wheel` job; Dependabot can bump them once they are pinned (via
  `constraints.txt` or a requirements file for the release job).
- A pull-request sdist check. The sdist is checked in `release.yml` only; the PR `wheel` job in
  `evaluate.yml` checks the wheel alone. Decided sufficient for v0.1.0. Revisit if a change breaks
  the sdist and is caught only at release time.

**Watch.** `tests/test_workflow.py` may assert over all files in `.github/workflows`. Read it
before adding files; extend the assertion to the new ones rather than exempting them.

## 0.7 Coverage measured, floor set, ratchet documented

**Why.** `CLAUDE.md` §10 sets a target of 0.8 test lines per source line and notes the repo is
at about 0.5. Line ratio is a proxy; measure the thing itself. A floor that only goes up turns
coverage into a ratchet instead of a debate.

**Touches.** `pyproject.toml`, `.github/workflows/evaluate.yml`, `README.md`,
`CONTRIBUTING.md`, `CLAUDE.md` §10.

**Do.** `pytest-cov` in the `dev` extra, `--cov=askback` in CI only (not in `addopts`, so a
local `pytest` stays fast), `fail_under` at the measured number rounded down, published as a
badge. Then **name the three modules with the least coverage in `CONTRIBUTING.md`** as the
standing good-first-issue list. Do not raise the floor and the coverage in one commit.

**Done when.** CI fails on a drop, the badge is live, and §10's prose cites a measured
percentage rather than a line ratio.

## 0.8 README: install first, clone second

**Why.** The Quickstart's first line is `python -m venv` followed by `pip install -e ".[http]"`
from a clone. After 0.1 that is the contributor's path, not the user's.

**Touches.** `README.md`, `docs/quickstart.md`, `START_HERE.txt`, `GETTING_STARTED.md`.

**Do.** Quickstart becomes `pip install askback` then `askback eval`. The clone instructions
move to Development. Keep the "your first run is green and means little" note exactly where it
is — it is the best paragraph in the README. Check the four onboarding files do not now
contradict each other; they overlap heavily today.

**Done when.** The first command a reader sees is one that works without git.

---

# Phase 1 — first contact with a real agent

> This phase is deliberately repetitive: five agents, the same step five times. The repetition
> is the method. Nothing here is a refactor; everything is a measurement.

## 1.1 Pick five real agents and record the shortlist

**Why.** Five agents chosen for coverage of *wire formats and failure modes*, not for
convenience. Choosing them in the open means the choice can be criticised.

**Touches.** `docs/VALIDATION.md` (new).

**Do.** Shortlist, with a sentence each on why it is in and what it is expected to exercise.
Aim across: a plain HTTP JSON chat endpoint; a Socket.IO or streaming one (to find out what
breaks, given streaming is unsupported — the result may be a documented refusal, which is a
finding); one with retrieval and citations; one with any notion of users or sessions; one
behind a framework with an opinionated API (Rasa, Chainlit, Dify, OpenWebUI-class). Record
version or commit for each; "latest" is not reproducible.

**Done when.** Five named agents with pinned versions, and for each the question its run is
meant to answer.

## 1.2 Validation harness and the shape of a validation record

**Why.** Five ad-hoc write-ups will not be comparable. Fix the shape before the first run, and
the five become a table.

**Touches.** `docs/VALIDATION.md`, `examples/validation/` (new), `.gitignore`.

**Do.** A per-agent directory holding the config, the suite, the probe transcript and the
report, plus a template record: how it was stood up; what the probe saw; what the config
needed; **which evaluators ran, which skipped, and why**; findings, each labelled
`agent-flaw`, `harness-flaw` or `suite-flaw`; which shipped default was wrong on this text.
Do not commit transcripts containing anything from a real deployment you do not own — these
are all locally-run containers, and that must stay true.

**Done when.** The template exists and a reader could fill it in for a sixth agent without
asking a question.

## 1.3 – 1.7 One real agent each

**Why.** Each run answers a question no test in this repository can: what does the harness do
when the thing on the other end was not written by us.

**Touches.** `examples/validation/<agent>/`, `docs/VALIDATION.md`. **Not** `src/`.

**Do.** Stand it up. Probe it. Write the config. Write 10–20 cases, including at least one
`no_answer`, one multi-turn, one clarification, and one cross-user if it has users. Run.
Record. **Fix nothing in `src/` during this step** — collect, in one place, everything that
needs fixing. A fix applied mid-measurement contaminates the measurement.

**Done when.** The record is complete, including the evaluators that skipped and why.

**Watch.** Invariants 1 and 8. The expected and correct outcome of several of these runs is a
large `skipped` count, because the agent returns text and nothing else. That is the honest
result and it must not be papered over by inferring a status from the text.

## 1.8 Fix what contact broke, measuring each verdict change

**Why.** The output of 1.3–1.7 is a list of defects. This is the step that spends it.

**Touches.** `src/askback/**`, `tests/**`, `docs/HR-COUPLING-AUDIT.md`, `docs/BACKLOG.md`.

**Do.** One commit per defect. For each: a fixture from the real reply that exposes it, the
fix, and **the count of verdicts it moves across every suite in the repository** written in the
commit message. If a change moves verdicts in the HR example, that is data: decide which verdict
is right and say why. Do not touch the HR baseline to keep it quiet (invariant 9).

Expect the damage to concentrate in: the clarification detector's weights and greeting
vocabulary; `response_mapping` against shapes nobody designed for a mapper; the citation
resolver's idea of a document; the judge threshold on non-HR text. Three of those four are
already named as HR-tuned in `docs/HR-COUPLING-AUDIT.md` — update that file as each is
re-tuned on wider evidence, and keep the audit honest rather than empty.

**Done when.** Every `harness-flaw` in `VALIDATION.md` is fixed, deferred in `BACKLOG.md` with
a reason, or declined with one. The gate is green. No baseline was edited to achieve it.

**Watch.** Invariant 9, and its escalation clause: the clarification vocabulary has been
extended twice. **If this step wants a third extension, the mechanism is wrong — stop and
escalate rather than adding words.**

## 1.9 Rewrite the README Status table against the evidence

**Why.** "Never run against a real agent" becomes false here, and the replacement must be as
precise as the original was.

**Touches.** `README.md`, `CLAUDE.md` §2 and §8, `docs/ROADMAP.md`.

**Do.** Replace the row with what is now true: validated against five agents, named, with a
link to the record; here is what skipped against each; here is what it got wrong and what was
fixed. Fill the compatibility matrix in `docs/ROADMAP.md`. **Keep "your first run is green and
means little."** Resist the pull to upgrade the tone: the credibility of this project is the
precision of these tables, and five agents is five agents, not validation in general.

**Done when.** No sentence in the repository claims the framework has never met a real agent,
and none claims more than five agents' worth of evidence.

## 1.10 Publish the findings report

**Why.** This is the artifact that travels. Not the framework — the findings.

**Touches.** `docs/FINDINGS.md` (new) or a blog post, plus a README link.

**Do.** Write up what five open-source chatbots actually did: the invented policies with
citations, the escalations that reached nobody, the cross-user answers, the turn-1 facts
forgotten by turn 3. Reproduce every claim with the config, the suite and the report
committed in `examples/validation/`. Name the harness's own misses in the same document. A
findings report that reports only the other side's failures is read as marketing; one that
reports its own is read as evidence.

**Done when.** A reader can reproduce any single finding in the document with two commands.

---

# Phase 2 — a finding in sixty seconds

## 2.0 Streaming transports: SSE, WebSocket, streaming HTTP

**Why.** The README lists raw WebSocket, Server-Sent Events and any streamed HTTP reply as
unsupported, and offers "write a short Python adapter" instead. **Most deployed chat agents
stream.** This is not a missing feature among many; it is the most likely single reason a
reader with a real agent cannot use the tool at all, and no amount of Phase 2 convenience helps
someone whose transport is unsupported.

It sits here, after Phase 1 and before `init`, for two reasons: Phase 1 tells you how many of
five real agents needed it (that number decides how much this is worth), and `init`'s inference
is wasted work if it can only infer transports we cannot speak.

**Touches.** `src/askback/adapters/sse.py`, `adapters/websocket.py` (new),
`adapters/http.py` (streamed replies), `adapters/factory.py`, `pyproject.toml` (extras),
`examples/` (a streaming example server), `tests/`, `README.md` (the Transports table).

**Do.**
- SSE and streaming HTTP first; they are the common case and both reduce to "accumulate chunks
  until the stream ends, then grade the assembled reply".
- Plain WebSocket after. `tools/probe_agent.py` already asks for `websockets>=14` and can probe
  one, so the wire knowledge is partly here.
- **Decide and document what "the reply" means when a reply arrives in pieces.** The assembled
  text is the obvious answer and is probably right, but a stream that revises itself (a draft
  replaced by a final) grades differently depending on the choice, and the clarification
  detector runs on whatever you pick. Write the decision in the module docstring, with the
  reasoning, the way the rest of this codebase does.
- Completion is the hard part, exactly as it is for Socket.IO: a terminator, a sentinel event,
  or a quiet period. There is no default there and there should be none here either.
- Test over a real socket against a real server (`CLAUDE.md` §6): a mock of a protocol we have
  never spoken only proves we agree with ourselves.

**Done when.** A streaming example server ships under `examples/`, is graded over a real
connection in the test suite, and the README's "Not supported" column has lost three entries
rather than gained a caveat.

**Watch.** Invariant 8. A stream that ends early, or ends without its terminator, must produce
an `error`, never a graded partial reply — a truncated answer scored as a complete one is the
exact shape of bug this project's invariants exist to prevent. Write that test first.

## 2.1 `askback init <url>`

**Why.** The probe already infers most of a config and writes `probe-result.json`; a human then
transcribes it. Close the loop. Its draft event classification is explicitly documented as a
guess that gets the content event wrong against our own server — so the command's job is to
write the guess down *as a guess*, not to hide it.

**Touches.** `src/askback/cli.py`, a new `src/askback/init/` (or `config/scaffold.py`),
`tests/`, `docs/quickstart.md`.

**Do.** Probe, infer transport, request template and `response_mapping`, write a config in
which every inferred value carries a comment saying it was inferred and what the evidence was.
Print which evaluators can run given what the agent returned, and which cannot and why — that
list is the most useful output of the command and it is free, since the engine already knows.
`tools/probe_agent.py` stays standalone and importing nothing from the package
(`tests/test_probe.py` enforces it): share logic by copying it, or not at all.

**Done when.** `askback init <url>` against `examples/toy-agent` writes a config that runs, and
against the Socket.IO example writes one whose wrong guess is visible as a comment.

## 2.2 The universal smoke suite

**Why.** First value today requires domain knowledge the user has and the tool does not. These
cases need none: they test properties every conversational agent must have.

**Touches.** `src/askback/demo/suites/` or a new `src/askback/smoke/`, package data in
`pyproject.toml`, `tests/`.

**Do.** A suite that ships as package data: refuses a question outside any plausible source;
remembers turn 1 at turn 3; does not fabricate under pressure ("are you sure? my manager said
otherwise"); refuses another user's data asked for politely; leaks no PII; ends an escalation
somewhere real. Every case documented with what a failure means and what it does not.

**Done when.** `askback eval --suite smoke` runs against any adapter with no suite file, and
each case's description says what it cannot conclude.

**Watch.** Invariant 1, hard. Several of these are unmeasurable without fields a plain text
agent does not return, and they must skip loudly, not pass. Write the absent-input test first
(invariant 8).

## 2.3 `askback scan <url>`

**Why.** `init` + smoke + `eval` in one command is the demo, the README's first example, and the
thirty seconds the cast records.

**Touches.** `src/askback/cli.py`, `tests/test_cli.py`, `README.md`.

**Do.** One command: probe, infer, run the smoke suite, print findings and the unmeasured
count, exit on the usual codes. The output must state, in its own words, that a scan finds
smoke and not truth — the way `askback eval` with no config already announces it is grading a
demo. That sentence is what keeps the feature honest.

**Done when.** `askback scan http://localhost:PORT` against the deliberately-flawed toy agent
reports its invented policy, with no config and no suite file.

## 2.4 Suite generation from a corpus or docs directory

**Why.** The step from smoke to a real suite is the remaining cliff. Facts come from the source;
so do the gaps.

**Touches.** new `src/askback/generate/`, `src/askback/cli.py`, `tests/`, `docs/first-suite.md`.

**Do.** Given a corpus (`jsonl`, as `examples/hr/corpus` already is), a docs directory or a
sitemap: cases whose `required_facts` are extracted spans, `expected_docs` the source document,
and `expected_status: no_answer` cases built from questions the corpus demonstrably cannot
answer. Every generated case marked `generated: true` with its source span recorded, and the
loader must refuse to treat a generated case as a golden one without a human's edit — a
generated expectation nobody read is a measurement of the generator.

**Done when.** Generating against `examples/hr/corpus` produces a suite that finds a subset of
the HR example's known findings, and the overlap is written down.

## 2.5 The thirty-second cast

**Why.** One recording outperforms the README's first three tables for a first-time reader, and
those tables are load-bearing later. Let the cast do the convincing and the tables do the
qualifying.

**Touches.** `README.md`, `docs/` (asset).

**Do.** asciinema (or a GIF rendered from it, committed small), of exactly: start the toy agent,
`askback scan`, read the finding. Real timing, no edits, no titles. Placed above the Status
table and below the one-line description.

**Done when.** It is in the README and runs under 40 seconds.

---

# Phase 3 — where the users already are

## 3.1 A stable programmatic API

**Why.** `askback.__all__` exports four names and no way to run anything. People wrap what
they can import, and 3.2, 3.5 and 5.1 all need this seam anyway.

**Touches.** `src/askback/__init__.py`, `src/askback/api.py` or `runner/__init__.py`, `tests/`,
`docs/`.

**Do.** One documented function: config in (path or object), suite selection, `RunReport` out,
no `sys.exit`, no printing, no report written unless asked. Document it as the stable seam and
say which other names are internal. The CLI becomes a caller of it.

**Done when.** A test exercises the full path through the API with no CLI involvement, and the
CLI has no evaluation logic the API lacks.

## 3.2 `pytest-askback`

**Why.** DeepEval's position is "pytest for LLM apps". This repository has the runner and the
result objects; the plugin is thin. It also puts findings in the test run teams already have,
rather than asking for a new one.

**Touches.** a `pytest11` entry point, `src/askback/pytest_plugin.py`, `tests/`, `docs/`.

**Do.** Collect a suite file as a pytest module; one test per case (or per turn, decided and
documented); failure output is the named checks behind the verdict, which is the repository's
best existing idea about reporting. `skipped` maps to pytest skip, `error` to error, degraded
judge to a warning. `samples`/`min_passing_samples` must survive the mapping — a plugin that
quietly runs one draw breaks invariant 7.

**Done when.** `pytest` over `examples/toy-agent`'s suite fails with a readable reason, and the
sampling semantics are preserved and tested.

## 3.3 JUnit XML output

**Why.** Every CI system renders JUnit natively. Cheapest integration in this phase.

**Touches.** `src/askback/reporting/junit_report.py`, `cli.py --format junit`, `tests/`.

**Do.** Render from the canonical JSON, as the other formats do. Redaction applies first,
unchanged: a JUnit file is an artifact like any other.

**Done when.** `--format junit` is valid, and a CI run shows per-case results in its own UI.

## 3.4 SARIF output

**Why.** SARIF puts findings in GitHub's code-scanning UI, annotated on the pull request. A
large amount of visible polish for a small amount of work.

**Touches.** `src/askback/reporting/sarif_report.py`, `cli.py`, `.github/workflows/`, `tests/`.

**Do.** Each failing check is a result; rule ids from the evaluator and check names; severity
from the existing severity, with CRITICAL mapped to error. Locations point at the suite file
and case id — the case is the thing a reader should open, not a source line.

**Done when.** The eval job uploads SARIF and a deliberately broken case appears as an
annotation on the pull request.

## 3.5 The GitHub Action

**Why.** A CI gating tool that cannot be added in three lines of YAML is not using its main
channel. Separate repository, tagged `v1`, with a moving major tag.

**Touches.** a new repository, plus a README section here.

**Do.** A composite action: inputs for config, suites, format, redaction, baseline; outputs for
exit code, pass rate and report path; SARIF upload and the pull-request comment optional. Its
own CI runs it against `examples/toy-agent` so the action is tested, not just published.

**Done when.** A three-line workflow in a scratch repository gates on a finding.

## 3.6 The pull-request comment

**Why.** The report already carries pass rate, new failures against baseline and named checks;
the job summary already renders a version. A comment puts it where the discussion is.

**Touches.** the action repository, `src/askback/reporting/markdown_report.py`.

**Do.** Sticky comment, updated rather than appended. Default to the `ci` redaction and never
inline a reply excerpt without it — a pull-request comment is public on a public repository,
which makes this the single easiest place in the project to leak a transcript (invariant 4).

**Done when.** A pull request shows one comment that updates on push, and a run with
`metrics_only` posts no text from any reply.

## 3.7 Evaluator plugin discovery

**Why.** A custom evaluator is registered by importing it, so a third party cannot ship one as
a package — they have to get their import executed, which in practice means a fork or a wrapper
script. `examples/custom-evaluator` shows the seam works; discovery is what makes it an
ecosystem. It is also the cheapest way to get domain expertise into the tool without putting
domain knowledge in the core (invariant 6).

**Touches.** `src/askback/core/registry.py`, `pyproject.toml`, `tests/`,
`docs/writing-an-evaluator.md`.

**Do.** Discover through an entry-point group (`askback.evaluators`), loaded once at startup,
with the loaded plugins and their versions **named in the run report's context** — a report
that was graded by code the reader cannot see is not reproducible, and reproducibility is the
reason the context block exists. A plugin that fails to load is an error at startup, not a
silently absent evaluator (invariant 8 again: a check that is not there must not look like a
check that passed).

**Done when.** A package outside this repository registers an evaluator, it runs, and its name
and version appear in the report context. The config can still refuse an unknown evaluator
name — discovery widens what is available, never what is silently accepted.

---

# Phase 4 — depth

Each of these is in `docs/BACKLOG.md` with "needs real traffic" as the reason it waits. Phase 1
is that traffic. Do not start Phase 4 before Phase 1 is finished; building them on the
simulator's replies fits them to the simulator, which is the mistake the backlog exists to
prevent.

- **`4.1` Citation support.** The cited passage plus an entailment judgement. Today a real
  chapter cited for an unrelated claim passes, and a reader assumes otherwise. Needs the judge
  calibrated (4.7) and the resolver returning the passage, not just resolving the reference.
- **`4.2` Manifest formats.** Multiple documents, roman numerals, `Ch.`, and — first, because it
  is a correctness bug by this repository's own standard — a **warning when a sentence-case
  manifest silently disables the title check**. A check that stops checking without saying so
  is the worst failure mode here.
- **`4.3` Clarification detector.** Record near-miss scores so there is anything to calibrate
  against; run `pii_egress` and the secret checks over intermediate clarifying questions, not
  only the final reply; render rounds and the unmeasured count in HTML. All three are already
  specified in the backlog.
- **`4.4` Confidence intervals.** Pass rate with an interval; a regression gate that separates
  "worse" from "noisier". Error bars are a large part of why Inspect is taken seriously by
  people who evaluate for a living.
- **`4.5` The adversarial generator.** Injection, role confusion, authority framing
  ("I'm their manager"), cross-tenant pivots, exfiltration phrasings — generated, marked
  generated, under 2.4's rule. The safety evaluators are already good; the inputs are
  hand-written and few.
- **`4.6` OpenTelemetry export.** A run lands in what the team already watches. Optional extra;
  the core imports no provider SDK (invariant 5) and this is not an exception to it.
- **`4.7` A published calibration report.** `askback calibrate` exists. Publish its result for
  the shipped defaults on real labels: agreement per dimension, and the threshold that follows.
  The 0.55 threshold is currently a number inherited from HR text, and it is one of the three
  HR-coupled defaults named in the audit.

---

# Phase 5 — ecosystem

- **`5.1` MCP server.** `askback` as a tool an assistant can call: run a suite, read a report,
  explain a failure in terms of its named checks. Needs 3.1. Low-competition shelf, real
  utility, and the explain path is where this project's reporting model actually shines.
- **`5.2` Documentation site.** mkdocs-material from `docs/`, published from CI. Do not
  restructure the prose while moving it; the writing is the asset.
- **`5.3` Comparison page.** When to use this instead of DeepEval, Promptfoo, Ragas, Inspect,
  and **when not to**. Each alternative gets a sentence a maintainer of it would not dispute, and
  at least two rows where it wins. A comparison that never recommends the alternative is
  discounted entirely, and this project cannot afford to be discounted on honesty — that is the
  one thing it is selling.
- **`5.4` Launch.** `.claude/LAUNCH.md`. Not before 1.10 exists.
