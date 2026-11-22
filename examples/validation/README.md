# Validation records

One directory per real agent that `askback` has been pointed at, each with the same shape so the
results compare. The shortlist and the reasons for it are in `docs/VALIDATION.md`. These are not
tests and nothing here runs in CI.

```
examples/validation/
  TEMPLATE/            copy this; do not fill it in place
    RECORD.md          the record: 8 numbered sections, every one required
    config.yaml        the askback config for the agent
    suites/            the suite, written before the first run
    probe/             probe-result.json from tools/probe_agent.py
    report/            the committed report (redacted: ci or metrics_only)
    local/             NOT committed (created by you): full-redaction runs, raw dumps
  <slug>/              ollama, open-webui, privategpt, onyx, dify; same layout
```

The rule that matters: **never commit a transcript from a deployment you do not own.** Every
agent here is a container the operator starts locally from a pinned public release. RECORD.md
section 0 is a checklist for it and is the first thing to fill in.

What is ignored (see `.gitignore`): `*/local/`, anything named `.env*`, and the agent's own
data volumes if you mount them inside this tree. The `report/` directory is committed on
purpose, which is why it is not called `reports/` (that name is ignored repository-wide).

The most important field in a record is section 5: which evaluators ran, which skipped, and the
cause of each skip. Read it before the others.
