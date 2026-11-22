# Validation record: AGENT_NAME

> Copy this whole `TEMPLATE/` directory to `examples/validation/<slug>/` and fill this file in.
> Delete every instruction block (the `>` quotes) as you go. A section left as the template, or
> filled with "n/a", is not acceptable: write what you did not do and why. A record with an empty
> section reads as an agent with nothing to report, and that is the failure this file exists to
> prevent.

| | |
|---|---|
| Agent | name, repo URL |
| Slot (from `docs/VALIDATION.md`) | a / b / c / d / e, or "added later" |
| Record date | YYYY-MM-DD |
| askback under test | `git rev-parse HEAD` of this repository, and `askback --version` |
| Operator | who ran it |
| Outcome in one line | what the run showed, written last |

**Order of work:** 0, 1, 2, then 4 (write the suite), then 3 (config), then run, then 5, 6, 7, 8.
Sections are numbered by what they record, not by when you do them. The suite is written before
the first run so that it cannot be shaped by the agent's answers; the config is written after the
probe so that it is shaped by the wire.

---

## 0. The rule that cannot be broken

**No transcript, report or config from a deployment we do not own.** Every run here is against a
container this operator started from a pinned public image or tag, on a machine this operator
controls, with no traffic to anyone else's service. Transcripts can carry other people's
personal, medical or financial data (CLAUDE.md invariant 4), and "it was only a test" is not a
defence for something committed to a public repository.

- [ ] The agent URL's host is loopback (`127.0.0.1`, `localhost`, `::1`) or a container network
      name. Paste it: `__________`
- [ ] I started the container myself, from the pin in section 1.
- [ ] Nothing in `probe/` or `report/` came from a hosted product, a shared staging system, a
      colleague's deployment, or a vendor's demo.
- [ ] The only model behind the agent is one I ran locally, or an API key that is mine and is
      **not** in any file here (secrets come from the environment, never from a file).

If any box cannot be ticked, **stop and do not commit the directory.** The run may still be
valid, but it is no longer a validation record and belongs in a private note.

---

## 1. How it was stood up

> Enough that someone else gets the same thing. "Latest" is not a pin.

- **Repo / tag / commit SHA:** (must match `docs/VALIDATION.md`; if it moved, say why)
- **Image or install method, pinned by digest:** `image@sha256:...`, or the exact `pip` / git
  commands. A tag alone can be moved.
- **Exact commands, verbatim, in order:** (a code block; include every manual step such as
  creating an admin account, building an app in a UI, importing a document set)
- **Model behind it:** name, size, quantisation or version, and how it was obtained. Answers
  depend on it, so two agents on the same model will correlate (see `docs/VALIDATION.md`).
- **What the agent was given to answer from:** documents ingested, knowledge base, system
  prompt, or "nothing". Commit these under `fixtures/` if they are ours and small; reference them
  by hash if not.
- **Cost, measured** (replaces the estimate in `docs/VALIDATION.md`): containers, RAM at idle and
  under the run, disk, wall-clock from nothing to first reply.
- **Anything that did not work first time:** and what you changed. Silence here makes the
  recipe unreproducible.

## 2. What the probe saw

> Run `tools/probe_agent.py` first, against the live agent, before writing any config. It imports
> nothing from this repository and must stay that way. Save its output to
> `probe/probe-result.json`. Then fill this in **from the transcript, not from the agent's
> documentation**; where the two disagree, that is a finding about the documentation.

- **Command run:** (verbatim, including the question sent)
- **Transport as observed:** plain JSON / newline-delimited JSON / SSE / WebSocket / Socket.IO /
  other. Is the reply one object, or a stream you reassemble?
- **How a reply ends:** a terminator, a closed connection, a quiet period. How long was the
  slowest reply?
- **Authentication:** none / key / bearer token / session cookie / login flow; how long it lives.

For each fact the harness could use, mark what the **raw response** contains, and the path to it.
"Absent" is a result, not a blank.

| Fact | In the raw response? | Path or note |
|---|---|---|
| reply text | | |
| outcome / status (answered, refused, no answer, escalated) | | |
| route or sub-agent that took the turn | | |
| retrieved passages | | |
| citations | | |
| tool calls | | |
| a session or conversation id | | |
| per-turn latency | | |
| identity of the user (whose conversation is it) | | |

- **What surprised you:** fields the docs promised and the wire did not carry; fields on the
  wire nobody documented.
- **Where the probe itself failed or guessed wrong:** a finding about `tools/probe_agent.py`
  (label it `harness-flaw` in section 6).

## 3. What the config needed that the probe did not infer

> The probe reads the wire. It cannot read intent. Everything in `config.yaml` that you had to
> decide, look up, or fix after a first failed run belongs here. This is the list that tells us
> what `askback init` (plan step 2.1) cannot automate.

| Setting | Value (no secrets) | Why the probe could not infer it | How you found out |
|---|---|---|---|
| e.g. `response_mapping.text` | | | |
| `request_template` | | | |
| `principal_headers` | | | |
| `side_effecting_tools` (`[]` is a claim: say why it is true here) | | | |
| `evaluation.redaction` | | | |
| anything else | | | |

- **Hand edits made after the first run, and what each fixed.** Every one of them. A config that
  worked first time says so in one line.
- **Settings left at shipped defaults that you were tempted to change.** Name them; section 7
  picks them up.

## 4. The suite

> Written **before the first run**, and frozen. The point is that the expectations come from
> what the agent claims to do, not from what it did.

- **Format:** see `docs/first-suite.md` and `examples/toy-agent/suites/faq.yaml`. A malformed
  suite is exit `3` when `eval` loads it; there is no separate validate command.
- **Suite file(s) and the commit that first contained them:** `suites/...`, SHA
- **Where the expectations came from:** the agent's own documentation, or the corpus ingested in
  section 1. Not from the HR example by default: say how each case was adapted or why it was
  dropped.
- **Cases: total, and how many per risk and per kind** (factual, no-answer, multi-turn,
  isolation, escalation, safety):
- **Cases deliberately not written, and why** (for example "no second user available").
- **Edits after the first run:** each one with the reason. A change made so a case would pass
  is a `suite-flaw` only if you can show the expectation was wrong on its own merits
  (invariant 9). Otherwise do not make it.

## 5. Which evaluators ran, and which skipped, and why

> **This is the most important section of the record.** The framework's whole claim is that a
> check which could not measure something says so instead of passing. This section is where we
> find out whether that held on an agent we did not write.
>
> A skipped evaluator is not a failure and not a pass. It is a claim the report is **not**
> allowed to make. Each skip below must be traced to one of five causes. "It skipped" is not an
> answer; "it skipped because the agent returns no retrieved passages, shown at `probe/...:12`"
> is.

### 5a. The count

> From the committed report. Each case carries one result per evaluator per turn, and a skipped
> one has `skipped: true`. Count those directly: the report's `by_evaluator` block leaves skipped
> results out and counts results, not cases, so it cannot be turned into a skip count by
> subtraction. Multi-turn cases make the totals exceed the case count; that is expected.

```bash
# Replace RUN with the report's file stem.
python -I - report/RUN.json <<'PY'
import collections, json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ran, skipped = collections.Counter(), collections.Counter()
for case in d["cases"]:
    for r in case["results"]:
        (skipped if r["skipped"] else ran)[r["evaluator"]] += 1
print(f"{d['summary']['total']} cases; counts are evaluator results (one per turn), not cases")
for name in sorted(set(ran) | set(skipped)):
    print(f"{name:20} ran {ran[name]:4}  skipped {skipped[name]:4}")
PY
```

| Evaluator | Ran (results) | Skipped (results) | Cause class (below) for the skips |
|---|---|---|---|
| rule_based | | | |
| rag | | | |
| safety | | | |
| llm_judge | | | |
| pii_egress | | | |
| escalation | | | |
| clarification | | | |
| citation_existence | | | |
| (others in the report) | | | |

### 5b. The cause of every skip

| Class | Meaning | Is it acceptable? |
|---|---|---|
| **A. Case declared nothing** | The suite asked this evaluator nothing on that case. | Yes. A suite choice. |
| **B. Agent did not return the input** | The raw response has no such field (no chunks, no status, no route). | Yes, if the run says the claim is unmeasured. This is the honest outcome. |
| **C. Our mapping lost it** | The field is in the raw response; `response_mapping` or the adapter dropped or mis-read it. | **No. `harness-flaw`.** The check was correct and its input was not there (CLAUDE.md invariant 8). |
| **D. Agent returned it, empty** | The field is present and legitimately empty (it retrieved nothing). | Yes, but a check must not read "nothing retrieved" as "nothing wrong". |
| **E. Not enabled** | The evaluator was off, or the config never turned it on. | Only if deliberate. |

For each (evaluator, cause) group that occurred:

| Evaluator | Cause class | Cases | Evidence (raw-response path, or config line) | What the report **cannot** claim as a result |
|---|---|---|---|---|

> **How to tell B from C.** Take one skipped case. Send that case's exact question to the live
> agent with `tools/probe_agent.py` (output under `local/`, not committed) to get the **raw**
> response. A `--redaction full` report (`--reports-dir examples/validation/<slug>/local`) shows
> what the harness *understood* of it; the probe shows what the agent *sent*. If the raw response
> has the field and the full report does not, it is C. If the raw response lacks it, it is B. If
> you cannot tell, it is unexplained: say so, and file it in section 6. Check one case per
> (evaluator, cause) group; you do not need to trace all of them.
>
> **The report does not record why an evaluator skipped.** A result carries `skipped: true` and
> its checks, and usually no reason. So the cause has to be established by hand, as above. That
> is itself the measurement: the first thing a user of this tool will want and cannot get from
> the output. Record how many skips you could and could not explain.

### 5c. Read the green

- **Number of cases that passed with at least one skipped evaluator:**
- **Number of cases that passed because the only evaluators that ran were weak ones** (the
  heuristic judge, a string match):
- **Judge in use, and whether any case was graded `degraded`:**
- **One sentence a reader could quote:** "On this agent the report can say X and cannot say Y."

## 6. Findings

> Every one, in a single table, each with exactly one label. No finding is "unclear": if you
> cannot decide, write the evidence you do have and put it under "Open questions" below, so the
> uncertainty is visible instead of resolved by guessing.
>
> **How to label.** Read the raw agent response first, then decide in this order:
> 1. **`harness-flaw`**: the framework misread the response, passed or failed on missing
>    input, crashed, hung, could not express something the agent does, or refused without
>    saying what to do. Includes the probe and the docs. Test: *would a correct harness have
>    produced a different verdict on the same bytes?*
> 2. **`suite-flaw`**: the expectation was wrong or was written in the shape of the HR example,
>    not of this agent. Test: *did the agent do what its own documentation says it does?*
> 3. **`agent-flaw`**: the harness read the response correctly, the expectation was right, and
>    the agent's behaviour is wrong: an invented policy, a missing escalation, a cross-user
>    answer, a fact forgotten between turns. Test: *would the agent's maintainers accept this
>    as a bug, given the same input?*
>
> A finding that moves from `agent-flaw` to `harness-flaw` after you look again is the process
> working; record the correction. Do not delete the earlier label.

| ID | Label | Case / check | Observed | Expected | Evidence (report path + case id, or probe path + line) | Status |
|---|---|---|---|---|---|---|
| F1 | | | | | | open |

- **Status values:** `open`, `fixed in <step>`, `wontfix: <reason>`, `reported upstream: <link>`.
- **Verdicts that changed because of something you changed** (a fix, a config edit): the number
  of verdicts that moved, which cases, and your decision on the merits whether the new verdict is
  right. The number you measured, not the number you expected (invariant 9).
- **Open questions:** things you could not classify.

## 7. Which shipped default was wrong on this agent's text

> The framework's defaults were tuned on an HR simulator (`docs/HR-COUPLING-AUDIT.md`). The
> point of running it against five other agents is to find out which defaults travel. For
> **each** item below, say what you examined and what you found. "No evidence either way" is a
> legitimate answer; leaving a row blank is not. Do not change a default in this step: record
> it, and step 1.8 decides.

| Default (audit item) | Examined how | Result on this agent | Verdicts it moved (count) | Recommendation |
|---|---|---|---|---|
| Judge threshold 0.55, `pass_threshold` 0.7 (#4) | | | | |
| Clarification detector: weights and English vocabulary (#5) | | | | |
| Citation resolver's idea of a "document" (#6) | | | | |
| `pii_egress` categories, especially `salary_figure` (#1) | | | | |
| `ChatResponse` shape: route, intent, retrieved, citations, tools (#10) | | | | |
| `side_effecting_tools` unset behaviour | | | | |
| `samples` / `default_samples` (one draw is not a measurement) | | | | |
| any other default you hit | | | | |

> **If you want to extend a detector's vocabulary to fix a finding here, stop.** A vocabulary that
> has to be extended a third time is evidence the mechanism is wrong (invariant 9). Record the
> miss, with the text that missed, and escalate.

## 8. Reproduce

> One block a stranger can paste, from nothing to the committed report. If it needs a step that
> is not in sections 1 to 3, those sections are incomplete.

```bash
# stand the agent up (section 1)
# probe it
# run the suite
askback --config examples/validation/<slug>/config.yaml eval --redaction ci
```

- **Exit code observed, and what it means** (`0` pass, `1` a case or gate failed, `2`
  regression, `3` the run could not be judged):
- **Committed report:** `report/<name>.json` (redaction `ci` or `metrics_only`, never `full`).
- **Not committed, kept under `local/`:** (anything unredacted)
