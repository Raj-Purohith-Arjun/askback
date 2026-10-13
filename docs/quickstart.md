# Quickstart

Ten minutes, no account, no API key, no network beyond `pip install`. You will run a
small test suite against a toy agent, watch it catch a real flaw, fix the flaw, and
save a baseline.

The toy agent is a canned FAQ bot in `examples/toy-agent/agent.py`, standard library
only. It has one deliberate defect, so the first run is red.

## 1. Install

From a clone of this repository, in a virtualenv:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[http]"
```

`http` is the extra that lets the framework talk to an agent over HTTP. Python 3.11 or
newer. This installs an `askback` command; `python -m askback` does the same thing.

## 2. Start the toy agent

In a **second terminal**, from the repository root:

```bash
python examples/toy-agent/agent.py
```

```
toy agent on http://127.0.0.1:8123/chat (fixed=False); Ctrl+C to stop
```

It takes `POST /chat` with `{"query": "...", "session_id": "..."}` and answers
`{"reply": "...", "status": "answered" | "no_answer"}`. Leave it running.

## 3. Run the suite

Back in the first terminal, from the repository root:

```bash
askback --config examples/toy-agent/config.yaml eval
```

Four cases run, and one fails:

```
  [PASS] faq-expenses             1.00  How long do I have to submit an expense claim?
  [PASS] faq-leave                1.00  How many days of annual leave do I get?
  [PASS] faq-unknown              1.00  Do I get a sabbatical after five years?
  [FAIL] faq-parking              0.73  Is there free parking for employees?

3/4 passed (75%)  mean score 0.93  critical failures 2

FAILED: pass rate 75% is below the required 80%.
FAILED: 2 critical check(s) failed, which gates the run regardless of pass rate.
    faq/faq-parking  expected_status: Turn ended as 'answered'.
    faq/faq-parking  prohibited_content: Reply contains prohibited content: ['free parking'].
```

The command exits `1`. That is the point: the agent has no parking policy, was asked
about one, and **invented** one instead of saying it did not know. A 75% pass rate
would look fine on a dashboard. The framework fails the run anyway, because a failing
critical check gates the build whatever the pass rate is.

```bash
echo $?        # 1   (Windows PowerShell: $LASTEXITCODE)
```

| Exit | Meaning |
|---|---|
| `0` | every gate passed |
| `1` | a case failed a gate: pass rate, a critical check, or a per-suite floor |
| `2` | a regression against the baseline (only with `--compare-baseline`) |
| `3` | the run could not be judged: a config error, a missing file, an exhausted judge budget |

A report was written to `examples/toy-agent/reports/` (ignored by git). Open the JSON
to see every check behind each verdict.

## 4. Fix the agent

Stop the toy agent (Ctrl+C in its terminal) and start the fixed one:

```bash
python examples/toy-agent/agent.py --fixed
```

```bash
askback --config examples/toy-agent/config.yaml eval
```

```
4/4 passed (100%)  mean score 1.00  critical failures 0
PASSED: pass rate 100% meets the required 80%.
PASSED: no critical checks failed.
```

Exit `0`.

## 5. Save a baseline and catch a regression

A baseline is the last result you accepted. Later runs are compared against it.

```bash
askback --config examples/toy-agent/config.yaml baseline save --output examples/toy-agent/baseline.json
askback --config examples/toy-agent/config.yaml eval --compare-baseline --baseline examples/toy-agent/baseline.json
```

```
Regression vs baseline (baseline.json):
  mean 1.000 -> 1.000 (+0.000)   pass rate 100% -> 100%
  No regressions detected.
```

Now restart the flawed agent (no `--fixed`) and run the comparison again. It names the
case that got worse and exits `2`. Promoting a baseline is deliberately a CLI action:
it leaves a file you commit, so a change to what "good" means is a diff someone reviews.

## What just happened

- `examples/toy-agent/config.yaml` told the framework to use the **HTTP adapter**
  against `http://127.0.0.1:8123/chat`. Nothing else about the agent was needed.
- `examples/toy-agent/suites/faq.yaml` is the suite: four short conversations and what
  each reply must satisfy. [Writing your first suite](first-suite.md) explains the
  format.
- `side_effecting_tools: []` in the config is a *statement* that this agent has no
  tools that act on the world. Leave it out and the tool-misuse check fails every case
  as critical, because the framework will not pass a check on a list nobody wrote.
- No judge model was involved. With none configured, the free, deterministic
  **heuristic judge** grades. A hosted judge is optional and metered; see
  [what it costs](cost.md).

## Next

- Point it at your own agent: [Writing an adapter](writing-an-adapter.md). For an agent
  that takes a JSON request over HTTP, the config alone is usually enough.
- Describe your own behaviour in a suite: [Writing your first suite](first-suite.md).
- Check something the built-in evaluators do not: [Writing an evaluator](writing-an-evaluator.md).
- Decide whether to trust a model judge: [Calibrating the judge](calibration.md).
- Know what a bill looks like before you turn one on: [Cost](cost.md).
