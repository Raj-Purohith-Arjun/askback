## What and why

<!-- One paragraph. Say what the change does and what problem it solves. -->

## Checks I ran

- [ ] `ruff check src tests tools examples`
- [ ] `mypy src`
- [ ] `pytest --strict-env`

<!-- Paste the result lines, or say which could not be run and why. -->

## Invariants

The rules a change must not break are in
[CLAUDE.md §3](https://github.com/Raj-Purohith-Arjun/askback/blob/main/CLAUDE.md) and are
explained in [CONTRIBUTING.md](https://github.com/Raj-Purohith-Arjun/askback/blob/main/CONTRIBUTING.md).
They are not restated here.

- [ ] I read them, and this change breaks none of them.
- [ ] If it adds or changes a check, I wrote the test where its input is absent first, and the check does not pass.
- [ ] If it moves any verdict, I measured how many and put the number below, and did not adjust a threshold, weight, vocabulary or fixture to keep a case green.
- [ ] Docs that state the changed behaviour moved in this pull request.

<!-- Invariants this change is near, and verdicts that moved (number, and which): -->
