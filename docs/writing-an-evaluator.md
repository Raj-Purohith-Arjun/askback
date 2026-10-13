# Writing an evaluator

An evaluator turns one reply into a list of named checks. The framework ships several
(`rule_based`, `rag`, `safety`, `llm_judge`, `pii_egress`, `escalation`); you write one
when you need to assert something they do not, such as "answers about money carry a
disclaimer" or "no reply is longer than a screen".

Keep checks small and independently meaningful. A report that says
`disclaimer_present: fail` is actionable. One that says `compliance: fail` is not.

## The smallest evaluator

```python
from askback.core.registry import EVALUATORS
from askback.evaluation.base import Evaluator


@EVALUATORS.register("shout")
class ShoutEvaluator(Evaluator):
    name = "shout"
    description = "Replies are not in capitals."

    def evaluate(self, case, turn, response):
        shouting = response.reply.isupper()
        return self.build([self.check("not_shouting", not shouting)])
```

`evaluate` is handed three things:

| | |
|---|---|
| `case` | the whole case: who is asking (`user_id`, `tenant_id`), `tags`, `risk`, `metadata` |
| `turn` | the turn being graded: `turn.user` and `turn.expect`, the expectations this reply must satisfy |
| `response` | what the agent returned: `reply`, `status`, `route`, `citations`, `retrieved`, `tool_calls`, and more |

It returns an `EvalResult`. You rarely build one by hand: `self.check(...)` makes a
`Check` and `self.build([...checks])` turns a list of them into a result. The result
passes only if **every** check passed, and its score is the mean of the checks' scores.

`self.check(name, passed, ...)` takes optional `detail` (what a human reads in the report),
`severity`, `score` (partial credit, default 1.0 or 0.0), `expected` and `actual`.

## Severity

A check is `INFO`, `WARNING` (the default) or `CRITICAL`.

A **critical** failure fails the case whatever its score, and fails the run whatever the
pass rate. Use it for things worth failing a build over: a leaked secret, an invented
policy, a missing safety disclaimer. A warning lowers the score and nothing more. Do not
mark everything critical; a gate that always fires is a gate people learn to ignore.

## The rule that matters most: absent input is not success

Before you write the check, write the test where its input is **missing**, and assert it
does not pass. The evaluator below shows the three cases a check has to decide between.

[`examples/custom-evaluator/disclaimer.py`](../examples/custom-evaluator/disclaimer.py)
requires a disclaimer phrase in answers to cases carrying certain tags:

```python
@EVALUATORS.register("disclaimer")
class DisclaimerEvaluator(Evaluator):
    name = "disclaimer"
    description = "Answers to tagged cases carry a required disclaimer."

    def __init__(self, phrase="", tags=(), **options):
        super().__init__(**options)
        if not phrase.strip():
            raise ConfigError("evaluator_options.disclaimer.phrase is required")
        if not tags:
            raise ConfigError("evaluator_options.disclaimer.tags needs at least one tag")
        self.phrase = normalize(phrase)
        self.tags = set(tags)

    def evaluate(self, case, turn, response):
        if not self.tags & set(case.tags):
            return self.build([])                    # (1) not my business: skipped

        if response.status in (ResponseStatus.UNKNOWN, ResponseStatus.ERROR):
            return self.build([self.check(           # (2) cannot read it: a failure
                "disclaimer_present", False, severity=Severity.CRITICAL,
                detail=f"The reply was {response.status.value}, so it could not be checked.",
            )])

        if response.status is not ResponseStatus.ANSWERED:
            return self.build([])                    # (3) a refusal needs no disclaimer

        present = self.phrase in normalize(response.reply)
        return self.build([self.check(
            "disclaimer_present", present, severity=Severity.CRITICAL,
            detail="Disclaimer present." if present else "Answer has no disclaimer.",
            expected=self.phrase,
        )])
```

1. **Nothing to assert is `skipped`, not `pass`.** `self.build([])` returns a skipped
   result: it is left out of the case's weighted score instead of dragging it up with a
   free 1.0, and the report says the check did not run.
2. **A reply you cannot read is a failure.** The agent returned `unknown` or `error`, so
   there is no evidence the disclaimer is there. Passing would let a broken integration go
   green. If you cannot tell, say so loudly: a failing check with a `detail` that names
   what was missing.
3. **A reply that is not an answer is skipped**, for the same reason as (1): the question
   the check asks does not apply.

The same discipline applies to anything else a check reads. If it reads
`response.retrieved` and the agent returned none, the honest result is "unmeasurable", not
"nothing wrong". A default value must never stand in for the missing one.

Failing at construction is better than failing quietly later: an empty `phrase` would
match every reply, so the constructor refuses it rather than let the check pass on every
case.

## Options from config

Everything in `evaluation.evaluator_options.<name>` is passed to your constructor as
keyword arguments, so an evaluator is configured without code:

```yaml
evaluation:
  evaluators: [rule_based, safety, disclaimer]
  evaluator_options:
    disclaimer:
      phrase: "this is not financial advice"
      tags: [financial]
  weights:
    disclaimer: 1.0
```

`evaluators` is the list that runs, in order; name every one you want, the built-ins
included. `weights` sets how much each counts toward the case's score. Give a pure gate
(an evaluator that only vetoes) a weight of `0.0`, as `pii_egress` and `escalation` do:
it then fails a case through its critical checks and does not lift or lower the score.

## Registering it

`@EVALUATORS.register("name")` puts the class in a table, and the config names it from
there. Registration happens when the module is imported, so something has to import it.
There is no plugin discovery yet. Two ways:

- **Contributing a built-in:** put it under `src/askback/evaluation/` and import it from
  `askback/evaluation/__init__.py`.
- **Using your own:** import your module, then call the framework from your own entry
  point:

  ```python
  import my_evaluators                      # registers "disclaimer"
  from askback.cli import main

  raise SystemExit(main(["--config", "my-config.yaml", "eval"]))
  ```

Names are unique. Registering a name twice is an error that names the first owner.

## Testing an evaluator

Use `ScriptedAdapter`: canned replies behind the real adapter protocol. A failing check
then points at your evaluator and not at a model's mood.
[`examples/custom-evaluator/run.py`](../examples/custom-evaluator/run.py) runs the
disclaimer evaluator over three scripted replies:

```bash
python examples/custom-evaluator/run.py
```

```
buy      PASS
sell     FAIL
weather  PASS
```

and `tests/test_examples.py` tests it the way this page teaches, absent input first:

- an `unknown` or `error` reply must **not** pass and must not be skipped;
- a missing disclaimer fails, and a present one (in any case, with any whitespace) passes;
- a case the evaluator does not apply to is skipped, not passed;
- a refusal is skipped;
- an empty `phrase` is refused when the evaluator is built.

Notice the first test is about input that is *missing*. That is the one that catches the
bug that has shipped most often in this project.

## A note on exceptions

If `evaluate` raises, the engine catches it, logs it with the case id, and records a
failing `<name>_error` check with a score of 0 for that evaluator. The run carries on and
the failure shows in the report. That check is a warning, not critical, so on its own it
lowers the case's score rather than vetoing it: a case can still pass on the strength of
the other evaluators. Treat a `_error` check as a bug to fix, not noise, and do not catch
bare `Exception` inside an evaluator to be safe. That is what hides the bug.

## Checklist

- [ ] Subclass `Evaluator`; set `name` and `description`; register it.
- [ ] Every check has a severity chosen on purpose.
- [ ] Missing input returns a skipped result or a failing check, never a pass.
- [ ] A test where the input is absent, written first.
- [ ] Bad configuration fails at construction, with the config key in the message.
- [ ] Nothing domain-specific in `askback/core/`.
