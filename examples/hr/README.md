# The HR example

A worked example and a demonstration: a small HR-style assistant, and the suites that grade it.
Everything here is **synthetic** (the corpus, the cases, the calibration records). The assistant
(`src/askback_sim`) is a development fixture and a reference for nothing. It is not in the installed
package; this example runs from a source checkout.

```bash
pip install -e ".[all,dev]"
askback --config examples/hr/config.yaml eval
```

## It is red, on purpose

On a fresh checkout this exits `1`: **81 of 101 cases pass and 28 critical checks fail.** That is the
tool working. The simulator has real flaws and the suites exist to catch them. Do not read a red
run here as a broken installation, and do not make it green by relaxing an expectation: a failing
detection that is edited away leaves the behaviour in place and removes the alarm.

For a first run that is green, and a suite you can copy, run `askback eval` with no arguments:
that grades a built-in demo agent against a small neutral suite.

| Suite | Failing | What the simulator does |
|---|---|---|
| `hallucination` | 4 of 10 | Asked about a policy that does not exist (sabbatical, cryptocurrency expenses, a certification path, a 2028 policy change), it retrieves the nearest real policy and answers from it, with citations. It says `answered` where `no_answer` is right. The citations make an invented answer look verified, which is worse than an unsourced guess. |
| `escalation` | 5 of 5 | Harassment, grievance, whistleblowing, discrimination and distress disclosures are answered from policy text and never handed to a human: no ticket, no queue. |
| `isolation` | 4 of 6 | Requests for another employee's pay, framed directly, as a manager, in bulk, or as a pivot after a normal question, are answered rather than refused. The negative control (`iso-shared-policy-readable`) passes, so refusal is not simply over-applied. |
| `multiturn` | 5 of 7 | Earlier turns are ignored: a fact stated in turn 1 is not used in turn 3, a correction is not honoured, a vague question is routed to the wrong agent, and a disclosure that should escalate mid-conversation is answered as policy. |
| `adversarial`, `reasoning` | 1 each | A confident answer that states the wrong figure (a reporting deadline, a certification prerequisite). |
| `golden`, `rag`, `routing`, `smoke` | 0 | |

By check: `expected_status` 17, `required_facts` 5, `refusal_behaviour` 5, `expected_route` 1.
These counts are asserted in `tests/test_cli.py`, so an improvement to the simulator shows up as a
test to update, not as silence.

## Files

| Path | What it is |
|---|---|
| `config.yaml` | The simulator's configuration and the evaluation settings that grade it. It enables the `salary_figure` PII category, which the shipped default leaves off. |
| `datasets/` | 101 cases in 10 files (YAML, JSON and CSV, to show the loaders). |
| `corpus/knowledge_base.jsonl` | The simulator's 30-chunk knowledge base (invented; "Acme" is a placeholder). |
| `intents.yaml` | The simulator's intent catalogue. |
| `baselines/baseline.json` | A baseline of the red run above, so `--compare-baseline` has something to compare to. |
| `calibration/seed.yaml` | Ten SYNTHETIC records for `askback calibrate`. They prove the command runs, and validate nothing about a judge. |

`examples/hr` and not `packs/hr`: a "pack" would imply an extension mechanism (something that
discovers and loads domain packs) that does not exist. This is an example you run from a checkout.
