# What it costs

The short version, which this page then qualifies in detail:

- **No licence fee.** Apache-2.0. Use it, change it, ship it.
- **No data egress** from the framework itself. It runs in your environment, calls only
  the agent you point it at, and phones nothing home.
- **A hosted judge is metered.** If you switch on a model to grade answers, every graded
  response is a paid call to that provider, and *that call sends your data to the
  provider*. The caching and budget controls below keep the bill visible and bounded.

Nothing else in the framework costs money. The rest of this page is what "bounded" means.

## What is free

Everything deterministic runs locally with no network beyond your agent:

- the rule-based checks (status, facts, prohibited content, route, tools, citations);
- the retrieval and grounding checks;
- the safety checks, PII-egress and escalation gates;
- the regression comparison and every report format;
- the **heuristic judge**, a lexical scoring proxy for the semantic dimensions. It is
  deterministic, free and offline. It is also a proxy: it marks down answers that are
  correct but worded differently. It is what runs unless you configure a model.

Two costs that are not the framework's but that you will pay:

- **Your agent.** Each turn of each case is a request to the system under test. If that
  system is itself a metered model, a run costs what 100 cases times their turns times
  `samples` of your agent costs. Sampling (`samples: 5`) multiplies it by five.
- **Your CI minutes**, if you run it in CI.

## What is metered: the model judge

By default no model grades anything. Turning one on is an explicit act, in the config:

The first choice, and the one that costs nothing per call, is **a model you run yourself**:
vLLM, Ollama, LiteLLM or a company gateway, anything that speaks the OpenAI chat-completions
format. Every graded reply stays inside your network, and no API key is needed or sent unless
you set one:

```yaml
evaluation:
  judge:
    llm:
      provider: openai
      base_url: ${JUDGE_OPENAI_BASE_URL:http://localhost:11434/v1}
      model: ${JUDGE_OPENAI_MODEL:llama3}
```

A hosted provider works the same way, and is metered. OpenAI uses `provider: openai` with
`JUDGE_OPENAI_API_KEY` and no `base_url`. Azure OpenAI:

```yaml
evaluation:
  judge:
    llm:
      provider: azure_openai
      endpoint: ${JUDGE_AZURE_OPENAI_ENDPOINT:}
      api_key: ${JUDGE_AZURE_OPENAI_API_KEY:}
      deployment: ${JUDGE_AZURE_OPENAI_DEPLOYMENT:}
```

`provider: anthropic` takes `JUDGE_ANTHROPIC_API_KEY` and a `model` you name yourself: there is
deliberately no default model id.

Requires the `llm` extra (`pip install -e ".[llm]"`). The judge reads only
`JUDGE_`-prefixed environment variables, never the agent's own, so it cannot pick up the
agent's key by accident, and it never grades with the model it is grading. If you ask
for a model judge and it cannot run (no credentials, an unknown provider), the run stops
with an error. It does **not** quietly fall back to the heuristic, because that would
change the scoring regime without saying so.

### What a call is

One call per **graded turn**: a turn that carries a `reference_answer`. A turn without
one is not sent to the judge. Each call sends the question, the reference answer and the
agent's reply, plus a fixed instruction, and asks for five scores and a one-line rationale
in at most 300 completion tokens.

```
calls = (turns with a reference_answer) x samples
cost  = calls x (prompt_tokens x price_per_1k_prompt
               + completion_tokens x price_per_1k_completion) / 1000
```

### What leaves your environment

**The question, the reference answer and the agent's reply go to the judge's provider.**
If the agent's answers contain personal data (an HR agent's do), that data is sent to
that provider, under that provider's terms. "No data egress" is true of the framework
and of the heuristic judge; it is not true once a hosted judge is on. Before you turn one
on for sensitive data, check it with whoever owns your data-processing agreements, or run
a judge model inside your own boundary (any endpoint the provider config can reach).

Reports are a separate matter: they are written at `ci` redaction by default and carry no
credentials. See the README's "What a report carries".

## Controls that keep it bounded

All under `evaluation.judge` in the config.

### See it: the summary prints the spend

Every run prints the judge's calls, cache hits and estimated cost, and writes them into
the report and the job summary. The estimate is only as good as the prices you give it:

```yaml
    price_per_1k_prompt: 0.00015        # EXAMPLE figures, not a price list:
    price_per_1k_completion: 0.0006     # use your model's price per 1,000 tokens
```

**Both default to 0**, which reports tokens but says `$0.00`. That is an understatement,
not a free run. Set them from your provider's current price list before you rely on the
number. Tokens come from what the provider reports; a call that failed has no token count
and is counted as a call at zero tokens, though your provider may still bill it. Treat the
figure as an estimate, not an invoice.

To learn what your suite costs, run it once, cold, with prices set, and read the summary
line. That is a better number than any estimate in a document.

### Cap it: `budget_usd`

```yaml
    budget_usd: 5.00
```

The run stops once the judge has spent more than this, exits `3`, and reaches **no
verdict**: the cases that never ran are not a sample of the suite, so a pass rate over
the rest would mean nothing, and the report says how many were reached. The ceiling is
checked between cases, so it can be overshot by the cases in flight (up to `workers`
of them). With the prices left at 0 it can never trip, which is why the CI nightly refuses
to run without prices (below).

### Reuse it: the verdict cache

```yaml
    cache_path: .cache/judge
```

A verdict is a pure function of what the judge was asked and who was asked, so an
unchanged case is not paid for twice. The cache key is the system prompt, the user prompt
(which contains the question, the reference and the **agent's reply**), the model and the
prompt version. Change any of them and the old verdict is correctly not found. In
practice:

- rerunning an unchanged suite against an unchanged agent costs nothing;
- when the agent's reply changes, that case is judged again, and only that case;
- changing the judge model or prompt version re-grades everything, as it should.

A cache hit costs `$0.00` and is reported as a hit. Only the **scores** are cached, never
the judge's rationale: the rationale quotes the reply, and a cache directory can be
restored by other workflows, so it is not allowed to hold text. A cached check therefore
reads "Rationale unavailable".

### Never blend regimes: degradation

If a judge call fails, that response is graded by the heuristic instead, marked
**degraded**, and counted. It is never mixed into the mean unmarked, and it is never
cached. `max_degraded` (default **0**) fails the run, exit `3`, when more responses than
that were degraded, so a provider outage costs you a red run rather than silently
different scores.

### Know whether it is any good: calibration

A judge you pay for should be one you have checked. `askback calibrate` runs it over
human-labelled records and reports Cohen's kappa; below `min_kappa` (default 0.6) it is
not fit to gate a merge. It costs one call per record, cached like any other. See
[calibrating the judge](calibration.md).

## In CI

`.github/workflows/evaluate.yml` runs the free, deterministic suites on every pull
request from this repository, and the **model judge only nightly**, under a ceiling:

- It needs repository variables for both prices and fails, naming what is missing, if
  they are absent. The ceiling is meaningless without them.
- `JUDGE_BUDGET_USD` defaults to `5.00`.
- Verdicts are restored from the previous night's cache, so an unchanged suite costs
  nothing the next night.
- It never runs for a fork's pull request, which has no secrets, and it never runs on
  `pull_request_target`.

## What it costs to do nothing

Run the heuristic judge, set no `llm` block, and nothing in this framework is metered.
You get the deterministic checks, the safety gates and the regression comparison for the
price of the CPU. What you give up is semantic grading: for answers that are right but
worded differently, the heuristic is harsher than a model, so expect some false failures
on free-text cases, and use `required_facts` for the numbers and dates that must match
exactly. That is a reasonable place to stay for a long time.
