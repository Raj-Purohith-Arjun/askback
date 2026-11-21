# LAUNCH.md

How this gets in front of people, and the honest arithmetic behind the number you are aiming
at. Internal; not published.

---

## The arithmetic, first

The stated goal is a thousand stars overnight. It is worth being precise about what produces
that, because the answer changes what to build.

Repositories that gain a thousand stars in a day have, with very few exceptions, exactly one
thing in common: **the top of Hacker News, `/r/programming`, `/r/LocalLLaMA` or a large
account on X, on the same day.** Code quality is not what gets them there. What gets them
there is a *story with a number in it* that a reader can retell in one sentence. Code quality
is what determines whether the stars convert into users, contributors, and the FAANG adoption
you are actually after — and whether the repository is still alive in six months. Both matter;
they are different problems and they have different deliverables.

So treat it as two tracks:

| Track | Deliverable | Decides |
|---|---|---|
| Reach | one retellable finding, with a number | whether a thousand people see it at once |
| Retention | install works, demo works, first value in a minute, issues answered | whether any of them are still there next month |

The reach track has exactly one input this project does not yet have, and it is Phase 1.

## The story

Today the project's headline is "I built an evaluation framework." There are a dozen of those
with real funding behind them, and that sentence will not travel.

After Phase 1 the headline is:

> **I pointed a new test harness at five open-source chatbots. All five invented a policy and
> cited a source for it. Here is the harness, here are the configs, reproduce it in two
> commands.**

That travels, for four reasons: a number, a surprise, a reproducible claim, and a villain that
is not a person. It is also *true*, verifiable, and it makes the thing you want adopted the
obvious next click rather than the pitch.

(The exact finding will be whatever Phase 1 actually finds. Do not write the sentence first and
then go looking — that is how a measurement becomes marketing, and this project's only real
asset is that it does not do that.)

## Do not launch before these are all true

A launch spends attention once. Every one of these is a reason someone who arrived interested
leaves, and none of them can be fixed during the hour you are on the front page.

- [ ] `pip install askback` works, from PyPI, tagged, with a release (plan `0.1`, `0.2`)
- [ ] `askback eval` passes on a clean machine with no checkout (plan `0.1`)
- [ ] The README's first command does not start with `git clone` (plan `0.8`)
- [ ] A cast or GIF, above the fold, under 40 seconds, showing a real finding (plan `2.5`)
- [ ] `askback scan <url>` gives a finding with no config and no suite file (plan `2.3`)
- [ ] `docs/VALIDATION.md` describes five real agents, with their configs committed (Phase 1)
- [ ] `docs/FINDINGS.md` exists and every claim in it is reproducible (plan `1.10`)
- [ ] The comparison page is written, and recommends an alternative where one is better (`5.3`)
- [ ] A GitHub Action, so "how do I use this in CI" is three lines (plan `3.5`)
- [ ] Issue templates exist, and you have cleared your calendar for 48 hours to answer them

The last line is not a joke. The highest-leverage hour of a launch is the one spent replying to
the first fifteen issues. It is also where "some guy dumped an AI-written repo" turns into
"there is a maintainer here" — and with one commit in the history and prose this polished, that
is the specific suspicion you will face. Answer it with responsiveness and a real commit
history, not with a denial.

## Where, and in what order

One day, in this order. Do not fire them simultaneously — the later ones feed on the earlier
ones' comments.

1. **Hacker News**, Show HN, Tuesday–Thursday, 08:00–10:00 ET. Title is the finding, not the
   tool: `Show HN: I tested five open-source chatbots for invented policies — all five failed`.
   First comment, from you, immediately: what it does not do, what it has never been run
   against, and what you want reviewed. HN rewards the limitation section; this repository has
   the best one I have read in a while, so lead with it.
2. **`/r/LocalLLaMA`, `/r/MachineLearning`, `/r/devops`.** LocalLLaMA cares that the judge can
   be a local model and that nothing leaves the network — that is a headline there and a
   footnote elsewhere. Rewrite per subreddit; a cross-post reads as a cross-post.
3. **X / LinkedIn**, as a thread: one finding per post, the reproduction command in the last.
4. **The lists**, same week: `awesome-llmops`, `awesome-llm-eval`, `awesome-ai-agents`,
   `awesome-testing`. Pull requests to lists, with the comparison page as the justification.
5. **Direct**, the following week: the maintainers of the five agents you tested, individually,
   privately, *before* any of the above if a finding is a security issue — cross-tenant leakage
   is a vulnerability disclosure, not a blog post. `SECURITY.md` exists in this repository;
   extend the same courtesy outward. If any finding is sensitive, the launch waits for the
   disclosure window. That is not negotiable and it is also the single best reputation purchase
   available to this project.

## What converts a star into a user

- **Time to first finding.** Minutes, not an afternoon. Phase 2 is the entire ballgame here.
- **A finding they did not expect.** The cross-tenant and memory cases are the ones that make
  someone send the link to their own team. That is the mechanism that compounds.
- **An answered issue.** First response under 24 hours for the first month, even if the answer
  is "that is a real gap, here is the backlog line."
- **Three good first issues**, real ones, with the file and the shape of the fix named. Plan
  `0.7` names the lowest-coverage modules — that is the list.
- **A second committer.** One maintainer is a bus factor and serious adopters evaluate it.

## What will be said, and the true answer

Prepare these. Each is a fair criticism with a real answer, and each is better answered in your
own first comment than in a reply to someone else's.

| Said | True answer |
|---|---|
| "Another eval framework." | This one tests a deployed agent over its own transport, across turns, as more than one user, and it answers the agent's clarifying questions. Name the four and let the comparison page carry it. |
| "Why not DeepEval / Promptfoo / Ragas?" | For what they do, use them — say which rows they win. This is for the deployed black box you did not write. |
| "Never run against a real agent." | After Phase 1: five, named, with configs committed and the harness's own misses recorded. Before Phase 1: do not launch. |
| "This is AI-written." | One commit, unusually even prose. The answer is a commit history, responsiveness, and the fact that every limitation is documented rather than hidden. Do not get defensive; the tables are the evidence. |
| "Tuned on HR text." | Written in the README and itemised in `docs/HR-COUPLING-AUDIT.md` before anyone asked. Phase 1 is what replaces it. Volunteer this one. |
| "The HR example fails." | On purpose. 81 of 101, 28 critical findings, and making it green by relaxing an expectation is a documented invariant violation. This is a good answer and people enjoy it. |

## Measure the right thing

Stars are a vanity metric that happens to be the one you asked about, so track it and keep it
in proportion. These are the ones that predict whether the project is alive in six months:

PyPI downloads, week 2 versus week 1 · issues opened by people who are not you · configs
contributed for agents you did not test · forks with commits · the GitHub Action's usage count ·
whether anyone has quoted a finding the harness produced about *their own* agent.

The last one is the only metric that says the tool works.
