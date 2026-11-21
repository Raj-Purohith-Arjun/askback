---
description: Execute one step from .claude/PLAN.md — by id, or the next unblocked one.
argument-hint: "[step id, e.g. 0.3 — omit for the next unblocked step]"
---

Execute exactly one step of the plan. Argument: `$ARGUMENTS` (a step id such as `0.3`, or
empty, which means the first unchecked step in the index).

Do not plan first; the steps are specified. `CLAUDE.md` §10 governs.

## 1. Read before touching anything

- `.claude/PLAN.md` — the step's five parts. The index also tells you what is already done.
- `CLAUDE.md` §3 (the invariants) and the specific invariants the step's **Watch** line names.
- The docstring of every module in the step's **Touches** list. Several encode decisions that
  look like accidents.

If the step is already `[x]`, say so and stop. If an earlier step in the same phase is
unchecked, say which and ask whether to do that one instead — the order is load-bearing within
a phase, not across phases.

## 2. Check the step is still right

A step written earlier can be wrong now. Before changing code, confirm:

- Its **Why** is still true.
- Its **Touches** list is sufficient. If the change needs a file outside the list, **stop**:
  say which file and why, and let the step be re-scoped. Do not widen it silently — a step
  whose scope grew during execution is a step whose revert is no longer safe.
- Nothing in the repository already does it. This codebase is larger than it looks and several
  things are half-present.

## 3. Do it

- Smallest change that satisfies **Done when**. No opportunistic refactoring, no drive-by
  tidying of a neighbouring function, no renaming.
- Tests: the ones that would catch a real regression. If the step adds a check or an evaluator,
  **write the test where its input is absent first, and assert it does not pass** (invariant 8).
- Documentation moves in the same commit as the behaviour. Several sentences about versions,
  defaults and limitations appear in `README.md`, `CONTRIBUTING.md`, `CLAUDE.md`,
  `docs/quickstart.md` and `START_HERE.txt` at once; grep for the claim, not the file.
- If a change moves any verdict in any suite, **count how many**, decide on the merits which
  verdict is correct, and put the number in the commit message. Never adjust a threshold, a
  weight, a vocabulary or a fixture to keep a baseline green (invariant 9).

## 4. Prove it

```bash
ruff check src tests tools examples && mypy src && pytest --strict-env
```

Then the step's own **Done when** command, run exactly as written. If **Done when** is an
observation rather than a command, state what you observed and how.

Also run, when the step touched the relevant thing:

```bash
askback eval                                          # must stay green
askback --config examples/hr/config.yaml eval         # must stay red, with the same findings
```

A changed HR result is data to report, not a failure to fix.

## 5. Commit and record

One commit, conventional style, with the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
trailer. The body says what moved and, where relevant, how many verdicts moved.

Then mark the step `[x]` in the `.claude/PLAN.md` index with the short sha. That edit is part of
the same commit where it can be, and the immediately following one where it cannot.

## 6. Report

Blockers only (`CLAUDE.md` §10): what is wrong, unsafe, or what the next step now depends on.
Three lines is a good length. Then name the next unchecked step and stop — do not start it.

If the step could not be completed, leave it unchecked, say precisely where it stopped, and
commit nothing partial.
