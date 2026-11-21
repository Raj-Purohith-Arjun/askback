# .claude/

Working files for Claude Code in this repository. Committed on purpose: the plan and its
status are part of the repository's state, not someone's local notes.

| File | What it is |
|---|---|
| `PLAN.md` | the numbered execution plan, and its status. The file to pick up work from. |
| `LAUNCH.md` | positioning, launch sequence, and the honest arithmetic of star counts. Internal. |
| `commands/step.md` | `/step [id]` — execute one plan step: scope check, change, gate, commit, record. |
| `commands/gate.md` | `/gate` — everything CI runs, in CI's order, including the install path. |
| `commands/validate-agent.md` | `/validate-agent <agent>` — the Phase 1 loop, once per real agent. |

`CLAUDE.md` in the repository root is the operating context and comes first in any session:
what the project is, the ten invariants, the architecture, the conventions. This directory is
*what to do next*; `CLAUDE.md` is *the rules while doing it*. On a conflict, `CLAUDE.md` wins
and the plan is wrong — say so rather than picking one.

`docs/ROADMAP.md` is the public version of `PLAN.md`: phases and reasoning, no step ids. Keep
them consistent. If a phase changes shape, both files change in the same commit.
