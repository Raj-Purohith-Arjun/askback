# Backlog

Work that is known, not started, and deliberately deferred. Each line says why it waits.
The common reason is the same: these need traffic from the real system to be built
against, and building them on the simulator's replies would fit them to the simulator.

## Clarification handling

- **PII and secret checks run only on the final reply, not on the agent's intermediate
  clarifying questions.** A question can leak a name, an id or a figure as easily as an
  answer can, and `pii_egress` never sees it because only the last reply is graded.
  Deferred until live traffic shows what intermediate replies actually contain; the rounds
  are already on the result, so the check is a loop over data we keep.

- **Detector near-miss scores are not recorded.** A reply is written to the report only
  when it is a question or questions were answered on the way, so a false negative (a
  question scored just under the threshold) leaves no trace, and there is nothing to
  calibrate the threshold against. Deferred until live traffic exists to calibrate on;
  recording them now would only fill reports with the simulator's replies.

- **HTML, Excel and Markdown reports do not render clarification rounds or the unmeasured
  count.** The JSON carries both (`rounds`, `detection`, `unmeasured`, and the summary's
  `unmeasured` and `persona_*`), so nothing is lost, only not shown. Deferred because what
  a reader needs to see is not clear until there are real exchanges to read.

- **The greeting miss: done.** The real greeting is now a fixture, and it is decided from
  the user's own message (a greeting asks for nothing, so there is nothing to clarify), not
  from phrases in the reply. `open_invitation` was removed. Still open: the structural rule
  treats any message made only of greeting words as asking nothing, so an agent that answers
  a bare "hi" with a real clarifying question (QUESTION 1 after "Hi") is also ruled out as
  one. That is right by the rule and wrong if such an agent exists; it needs real traffic to
  say whether it does.

## Citation existence

- **It proves existence, not support.** A real chapter cited for an unrelated claim passes.
  Closing that needs the cited passage and an entailment judgement, which is the judge's
  job, not this check's. Deferred until the judge is calibrated on real answers.
- **Document names are found heuristically.** A name is recognised after "According to the",
  before ", page(s)" and after "of the", and only if it looks like a document (manual,
  handbook, policy ...). A source the agent names in some other way is not seen. Deferred
  until real replies show which other forms occur.
- **A page far from its chapter is not checked against it.** Pages are attributed to a chapter
  on the same line or in a block that cites exactly one; elsewhere they are held to the
  document's length only, to avoid failing a correct answer on a wrong guess.
- **Roman-numeral and "Ch." chapters, and one manifest per run.** Only "Chapter N" is read, and
  only one document is described. Deferred until a second document or format appears.
- **The title check goes silent on a sentence-case manifest.** The citation title check now
  requires title-case, to avoid misreading a colon-introduced sentence as a claimed chapter
  title. A manifest whose chapter titles are sentence-case therefore gets no title
  verification at all, and silently. A check that stops checking without saying so is worse
  than one that fails loudly: this needs either a warning when a manifest's titles are not
  title-case, or a better discriminator. Not a v1 blocker.

## Socket.IO adapter

- **Replies delivered by acknowledgement, not by event.** Some Socket.IO servers answer an
  `emit` through its ack callback instead of a reply event. The adapter reads events only.
  Deferred until we know how the target answers.
- **Per-event response mapping.** Metadata is merged from all classified events into one
  payload and read by one `response_mapping`, so two metadata events that use the same key
  for different things overwrite each other. Deferred until the target's event payloads
  are known.
- **The target's real auth contract.** `CachedTokenAuth` supplies the caching, expiry and
  refresh; the `/signin` and `/verifyotp` request fields, and where a test identity's OTP
  comes from, are the target's. `examples/socketio-agent/auth.py` is the shape, not the
  contract.
