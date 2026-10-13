# Calibrating the judge

The judge turns an answer into five scores, and those scores decide whether a case
passes. Nobody has yet checked that they agree with a person who owns the answer.
Until someone has, every judged score is unvalidated, whichever judge produced it:
the offline heuristic or a hosted model.

`askback calibrate` is the check. It runs the configured judge over records a
human has labelled and measures how often, and how far beyond chance, they agree.

```bash
python -m askback --config examples/hr/config.yaml calibrate        # the SYNTHETIC seed set
python -m askback --config my.yaml calibrate --records calibration/records.yaml
```

Exit `0` when the judge clears the bar, `1` when it does not, `3` when it could not
be measured (a missing file, a model judge that failed on a record). That is the
same convention as `eval`, so it can be a CI gate.

## What it measures

For each labelled dimension of each record, the human said *acceptable* or
*unacceptable*. The judge's score is cut at the pass threshold `eval` gates with
(`evaluation.evaluator_options.llm_judge.threshold`), so what is validated is the
decision that fails builds, not the raw number. The command reports, overall and
per dimension:

- **Cohen's kappa**, which is what is gated on.
- Raw agreement, for context only.
- Every disagreement, by record id and dimension, so someone can open the record
  and see where the judge is wrong. Replies are not printed: calibration records
  drawn from real runs contain HR data, and CI logs are not where it belongs.

### Why kappa and not "agreement %"

Most answers a healthy agent gives are acceptable. A judge that says *acceptable*
every time then agrees with the humans nine times in ten and has learned nothing.
Kappa subtracts the agreement that chance alone would produce, given how often
each side says yes: that judge scores **0**, a judge that matches every label
scores **1**, and one that is worse than chance goes negative.

### Why 0.6

`evaluation.judge.min_kappa` defaults to **0.6**. The usual reading of kappa puts
0.6 to 0.8 at "substantial" agreement and anything under it at moderate or worse.
Below about 0.6 the judge disagrees with the people who own the answers often
enough that a build failing on its score is as likely to be the judge's error as
the agent's, and a build passing on it proves little. **A judge under that line is
not fit to gate a merge.** Use it to find problems, read its disagreements, and do
not let it block or approve anything until it clears the bar. Raise `min_kappa`
for a stricter gate; do not lower it to turn a red calibration green.

The gate is on **overall** kappa. The per-dimension table is there to be read:
`groundedness` is the dimension that fails a build outright, and a judge that is
good on average and poor on that one is not safe either.

Kappa can be **undefined**: if the humans and the judge both used the same label
on every item there is nothing to agree beyond chance about. That is reported as
undefined and fails the gate. A set with only acceptable answers cannot calibrate
anything.

## Building the real set

The seed set is a demonstration. The real one needs HR, so book that time early; it
is the long-lead item, not the code.

1. **50 records**, drawn from actual run output (`reports/*.json`), not written by
   hand. A hand-written reply is the reply its author expected; a real one is the
   one the agent gave, including the answers that look right and are not.
2. **Draw for variety, and include the failures.** Take answers across suites, plus
   every borderline one you can find. A set that is nearly all clear passes
   produces an inflated agreement and a kappa that says little. Aim for at least a
   quarter *unacceptable* on some dimension.
3. **Labelled by someone from HR who owns the answer.** Not the engineer who wrote
   the agent, and not a model. The labeller decides what *acceptable* means for a
   figure or a date, and in this domain a figure is not paraphrasable: "about a
   month" is not "30 days".
4. **Label before looking at the judge's score.** Labels adjusted after a run, to
   make a disagreement go away, measure nothing.
5. **Record who and when.** Every record carries a `labeller` and a `date`, because
   a policy changes and a label from before the change is stale.

### The file

```yaml
schema_version: 1

# Whose run output these were drawn from, and the prompt version it ran under.
labelled_against:
  judge: model:azure_openai/gpt-4o-mini
  prompt_version: "1"

records:
  - id: hr-leave-014
    user: How many days of annual leave do I get?
    reply: "..."                    # what the agent said
    reference_answer: "..."         # the judge scores against it, so it is required
    retrieved:                      # what the agent retrieved; may be empty
      - {doc_id: HR-001, title: Annual Leave Policy, text: "..."}
    status: answered                # optional; answered unless the reply was a refusal etc.
    labels:                         # at least one; unlabelled dimensions are skipped
      correctness: acceptable
      completeness: acceptable
      groundedness: unacceptable
    labeller: test-labeller (People Operations)
    date: 2026-10-10
    synthetic: false                # true marks a record that validates nothing
```

The loader is strict: an unknown key, a label that is not `acceptable` or
`unacceptable`, a dimension that does not exist, a duplicate id, or a bad date is
refused by name. A typo in a label would otherwise be a label that was never read.

A dimension the human did not label is left out of the statistics, never assumed
acceptable.

## What invalidates a calibration

**Changing the judge model, or `PROMPT_VERSION`, invalidates the calibration, and
the baseline with it.**

- A kappa describes one judge: this model, reading this prompt. It says nothing
  about another model, or about the same model after the prompt changed. When
  either changes, the last number is void and `calibrate` must be run again before
  the judge is trusted. `JUDGE_PROMPT_VERSION` (in `evaluation/llm_judge.py`) is
  bumped for exactly this reason whenever the scale, parsing or dimensions change.
- Every score in the regression baseline was produced by the old judge. Compared
  with a candidate graded by the new one, a score difference may be the instrument
  changing, not the agent. Re-save the baseline after recalibrating.

The command prints the judge and its prompt version beside the result, and notes
when the records were drawn from a different judge than the one being measured.
Labels are human, so a different judge can still be measured against them, but
records sampled from one judge's run are that judge's borderline cases, and
another judge's agreement on them may read better or worse than it deserves.

A model judge is metered. Calibrating costs one call per record, cached on disk
like any other verdict, and the cost is printed. The cache keeps scores only, so a
disagreement listed from a cached verdict has no rationale beside it; delete the
cache entry (or the cache) to pay for the call again and read one. If a call fails the record is
graded by the heuristic, marked degraded, and left out of the statistics: counting
it would validate the fallback and call it the model. That exits `3`.

## The seed set

`examples/hr/calibration/seed.yaml` holds ten **SYNTHETIC** records: three clear passes, one
reworded pass, four clear failures and two borderline. They were written and
labelled by Claude Code, not by anyone from HR, and the replies were composed by
hand. They exist so the command can be run and tested. A kappa computed from them
says the command works. It says nothing about the judge, and the command prints
that every time it reads them.
