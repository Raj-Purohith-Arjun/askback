# Contributing

Thank you for helping. This is an evaluation framework: its job is to say, truthfully,
whether a conversational agent behaves. So the bar for a change is less "does it work"
than "can it make a report say something false". Read the invariants below before
you write code; they are what review checks first.

By contributing you agree that your work is licensed under the
[Apache License 2.0](LICENSE) and that you will follow the
[code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities as described in
[SECURITY.md](SECURITY.md), not in an issue.

## Set up

Work in a virtualenv with every optional extra and the dev tools. Without the extras
whole test modules cannot import, and pytest's summary line will not tell you.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -c constraints.txt -e ".[all,dev]"
```

Python 3.11 or newer (3.11 and 3.14 are what CI runs). `constraints.txt` pins ruff, mypy and
pytest to the versions the checks were verified against, so a tool release cannot turn CI red
for a reason that is not in your diff.

## Run the checks

Everything CI runs, in the order to run it:

```bash
ruff check src tests tools examples
mypy src
pytest --strict-env
```

- `mypy src` is **strict** and has no exemption list. Do not add a `# type: ignore`
  without a comment saying why, and do not add a module to an override list.
- `pytest --strict-env` turns a module skipped for a missing dependency into an error
  and asserts the collection floor in `tests/expected_tests.json`. A plain `pytest`
  skips quietly and prints a `MODULES NOT TESTED` block; use it for a quick loop, not
  for "done".
- Tests use the `ScriptedAdapter`, not mocks. If you find yourself patching `httpx`,
  you are testing the patch. (The HTTP adapter's own tests use `respx` to fake the
  network, and drive the real adapter.)
- The suite needs no network and leaves nothing behind that git tracks.

`askback eval` with no arguments grades the built-in demo and **must exit 0**: a red result
is a regression. `askback --config examples/hr/config.yaml eval` is **expected to exit 1**: it
grades a simulator that has known failures (see [examples/hr/README.md](examples/hr/README.md)),
and a red result there is the harness working. Do not make it green by editing a dataset,
lowering a threshold or downgrading a severity.

## The invariants a pull request must not break

These are tests we will not trade away. If your change violates one, stop and open an
issue to discuss it first.

1. **Unmeasurable never passes.** No retrieved context means `skipped`; no route field
   means `error`. Never `pass`. A green run that silently skipped its grounding checks
   is worse than a red one.
2. **Critical severity gates the build.** Any failing `CRITICAL` check fails the run,
   whatever the pass rate.
3. **No silent scoring-mode changes.** If the model judge falls back to the heuristic,
   that case is marked degraded and the run reports it. Two scoring regimes are never
   blended into one mean.
4. **Reports are redacted by default.** `--redaction full` is local-only and is never
   the CI default.
5. **The core imports no provider SDK.** Judges and adapters sit behind `Protocol`s;
   `openai`, `anthropic`, `httpx` and friends live in optional extras and are imported
   lazily.
6. **The core has no domain knowledge.** Nothing about leave, payroll or any other
   domain goes in `src/askback/core/`. Domain logic belongs in a pack or a suite.
7. **Non-determinism is measured, not assumed away.** A case declares `samples` and
   `min_passing_samples`; one draw is not a measurement.
8. **Absent input is never evidence of success.** A check concludes "nothing wrong"
   only from data it actually received. When the thing it inspects is missing (a field
   the agent did not return, a mapping that dropped it, a list nobody filled in) it
   reports `skipped` or `error`, and a default must never stand in for the missing
   value. This bug has shipped three times: chunk metadata dropped by the HTTP mapping,
   a status that defaulted to `answered`, and a side-effecting-tool list that was empty
   unless the simulator was loaded. **Write the test where the input is absent first,
   and assert it does not pass.**
9. **A verdict that changes is data, not a regression to undo.** Never adjust a detector,
   evaluator, threshold, weight or fixture to keep a baseline or a case green. Measure how
   many verdicts moved, decide on the merits, and record the number.
10. **A plain install must work.** The built wheel, installed in a clean environment, runs
    `askback eval` from an empty directory and passes. Whatever that first command needs
    ships as package data.

Also enforced by tests:

- **`askback` never imports `askback_sim`.** `tests/test_boundaries.py` scans the source
  and also runs the framework in a process where the simulator cannot be imported.
  `askback_sim` may import `askback`. The CLI and `askback/api/` are the only places
  allowed to know the simulator exists, and they import it lazily.
- **Verdict caches hold scores, never text.** The judge's rationale quotes the reply,
  and the cache is restorable by other workflows.

## Adding things

### An evaluator

An evaluator turns one reply into a list of named checks. See
[docs/writing-an-evaluator.md](docs/writing-an-evaluator.md) for a worked example.

1. Subclass `askback.evaluation.base.Evaluator`, set `name`, implement `evaluate`.
2. Register it with `@EVALUATORS.register("name")` and import its module from
   `askback/evaluation/__init__.py` so registration happens.
3. Give each check a severity. `CRITICAL` is for things worth failing a build over.
4. If the data the check needs can be absent, return no checks (the case is `skipped`)
   or a failing check that says what is missing. Test that first (invariant 8).
5. Name it in `evaluation.evaluators` in the config to turn it on, with any options
   under `evaluation.evaluator_options`.
6. If it matches phrases, compile them through `core.text.tolerate_line_breaks` and add the
   fixture to `tests/test_wrapping.py`. A pattern written with single spaces stops matching
   where a reply happens to wrap, and the signal goes missing without anything failing.

### An adapter

An adapter is how the harness reaches a system under test. See
[docs/writing-an-adapter.md](docs/writing-an-adapter.md).

1. Implement the `AgentAdapter` protocol in `askback/adapters/base.py`: `__enter__`,
   `__exit__`, `send`, `describe`. A protocol, not a base class; nothing to inherit.
2. `send` must be safe to call from several threads at once, and must report only what
   the system returned. A field it did not expose stays empty.
3. Register it in `askback/adapters/factory.py` and add its name to `VALID_ADAPTERS`.
4. Test it against the `ScriptedAdapter`'s behaviour: the same suite file must run
   unchanged through both.

### A suite

[docs/first-suite.md](docs/first-suite.md) covers the format. A suite file is data, not
code, and goes under `examples/hr/datasets/` (graded against the HR simulator), in
`src/askback/demo/suites/` (the neutral built-in example), or in another folder under
`examples/`. Use invented names and invented policy text only: never text from a real
organisation's documents, and never a real person's name.

## Test floors

`tests/expected_tests.json` records the minimum number of tests each module must
collect, so a module that silently stops being collected breaks the run.

- Adding tests never breaks it. Raise a number when a module has grown enough that its
  floor stops meaning anything.
- Lower a floor only when tests are **deliberately removed** because the code they
  covered is gone, and do it in the same commit as the removal. The commit message must
  name the tests and say why.
- Never lower one to make a run pass.

## Commits and pull requests

- [Conventional commits](https://www.conventionalcommits.org): `feat:`, `fix:`,
  `docs:`, `test:`, `refactor:`, `chore:`, `ci:`, with a scope where it helps, and a
  `!` plus a `BREAKING CHANGE:` footer for anything that changes a config key, an
  endpoint, a report field or an exit code.
- Make the smallest diff that does the job. Do not refactor across unrelated seams in
  the same pull request.
- Comments and docstrings explain **why**, not what. A comment that restates the line
  gets deleted in review.
- Do not catch a bare `Exception` except at a documented boundary (evaluator isolation,
  judge fallback), and always log with context.
- Secrets come from the environment through `${VAR}` references. Never from a committed
  file, never as a default value, never in a report.
- Do not add a dependency without saying which extra it belongs to and why the standard
  library will not do. A small dependency surface is a feature.
- State what you ran. "pytest --strict-env, mypy src and ruff pass" is what a reviewer
  wants to read.

Pull requests from a fork run lint, types and unit tests. The evaluation job that holds
secrets does not run for forks, by design, and the job log says so.
