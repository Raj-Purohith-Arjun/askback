---
description: Run every gate CI runs, in CI's order, and report only what failed.
---

Run the full gate. No code changes in this command — if something fails, report it and stop.

```bash
ruff check src tests tools examples
mypy src
pytest --strict-env
askback eval                                   # built-in demo: must pass, 8/8
askback --config examples/hr/config.yaml eval  # HR example: must be RED, exit 1
```

Then the install path, which the `wheel` job checks on every pull request and which local
development never exercises:

```bash
python -m build --wheel --outdir /tmp/dist
python -c "
import glob, zipfile
n = zipfile.ZipFile(glob.glob('/tmp/dist/*.whl')[0]).namelist()
assert any(x.endswith('askback/demo/suites/demo.yaml') for x in n), 'demo suite missing'
bad = [x for x in n if x.split('/')[0] == 'askback_sim']
assert not bad, f'the simulator is in the wheel: {bad[:3]}'
print('wheel ok')"
python -m venv /tmp/clean && /tmp/clean/bin/pip -q install /tmp/dist/*.whl
mkdir -p /tmp/empty && cd /tmp/empty && /tmp/clean/bin/askback eval --quiet --format json
```

## What each result means

| | |
|---|---|
| `mypy src` not clean | a gate failure, not a style note. Strict, no exemption list, and do not start one. |
| a `pytest` skip under `--strict-env` | an error: a missing extra, not a passing test |
| `askback eval` red | a real regression in the framework or the bundled suite |
| HR example green, or fewer findings | **investigate before celebrating.** It is red on purpose; 81 of 101 pass and 28 critical checks fail. Fewer findings usually means a check stopped checking. |
| HR example findings changed at all | data. Report the count and which cases moved; do not restore the old number. |
| the wheel carries `askback_sim` | invariant 10 broken: the simulator must not ship |

## Report

Only what failed, with the command and the first useful line of output. If everything passed,
one line saying so plus the HR example's finding count, so a drift in it is visible over time.
