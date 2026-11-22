# Validation: which real agents, and why these

Phase 1 of the plan points `askback` at real open-source agents. Until it has, every number in
this repository is about a stand-in or our own simulator. This file is the shortlist, written
before any run, so that the choice can be criticised.

Everything here was checked on 2026-11-22 against the GitHub API (default-branch commit date,
tag commit, archived flag, release date) and the project's own README or source at the pinned
tag. Nothing was picked from memory. Where a fact was *not* verified it says so.

## Selection rules

Every candidate had to be: runnable locally by anyone from a public repo (no hosted-only product,
no signup-gated demo); pinned to a release tag, with its commit SHA recorded; and still
maintained (last commit within weeks, not archived). The five had to differ in wire format or
failure mode, not be five variations of one.

"Pinned" is the tag and its commit SHA. For container images, pin by the digest of the tag when
the run is stood up (step 1.3 onwards) and record it in that agent's record; a tag alone can be
moved.

## The shortlist

| | Slot | Agent | Pin | Transport |
|---|---|---|---|---|
| 1 | a. plain HTTP JSON | Ollama | `v0.40.17` | `POST /api/chat`, JSON |
| 2 | b. Socket.IO / streaming | Open WebUI | `v0.13.4` | HTTP in, Socket.IO out (`/ws/socket.io`) |
| 3 | c. retrieval + citations | PrivateGPT | `v1.0.1` | HTTP messages API |
| 4 | d. users / sessions | Onyx | `v5.1.3` | HTTP, authenticated chat sessions |
| 5 | e. opinionated framework API | Dify | `1.20.0` | HTTP service API, `conversation_id` + `user` |

### 1. Ollama (slot a: plain HTTP JSON; a model server, the degenerate case)

- **Repo:** https://github.com/ollama/ollama (MIT)
- **Pinned:** `v0.40.17`, commit `23b94a2daf2cd1fbf6b4dcfbfd3efbaca424b7bf` (2026-11-21)
- **Last commit:** 2026-11-22 (default branch), checked 2026-11-22
- **Stood up:** `docker run ollama/ollama:0.40.17`, then pull one small model. No account, no key.
- **Cost to stand up (estimate, not yet measured):** 1 container; a few GB of disk and RAM for one small model (the model dominates); no API key; no account.
- **Transport:** `POST /api/chat`. Streams newline-delimited JSON by default; the API docs state
  that `"stream": false` returns one JSON object. We send `stream: false`, so this is the plain case.
- **What it is, and is not:** a model server, **not a conversational agent**. It has no persona,
  no policy, no retrieval, no tools, no memory of its own (we resend the message list every
  turn) and no notion of a user. It is in the list as the **degenerate case**: minimum surface,
  maximum skips. It is the floor test for what the harness does when it receives text and
  nothing else, and nothing about its answers should be read as a finding about an agent. It
  also supplies the model the other four run on (see the last section), which is a second,
  incidental reason it is here.
- **Question only this run answers:** *Given a real endpoint that returns nothing but text, does
  every check that needs a route, context or status report `skipped` or `error` rather than
  `pass`?* The tests prove that for our own fixtures; this is the first endpoint we did not
  write that omits those fields (invariants 1 and 8). The expected result is a report that is
  mostly `skipped`, and says so; a green run here is a bug in the harness.

### 2. Open WebUI (slot b: Socket.IO / streaming)

- **Repo:** https://github.com/open-webui/open-webui (licence reported as `NOASSERTION`: a custom
  licence, read it before redistributing anything)
- **Pinned:** `v0.13.4`, commit `0666e59d26a4d056e0bacee41742486badd71cf8` (2026-11-17)
- **Last commit:** 2026-11-22, checked 2026-11-22
- **Stood up:** the `docker run ... ghcr.io/open-webui/open-webui` command in its README, with the
  tag pinned instead of `main`, backed by the Ollama container from entry 1.
- **Cost to stand up (estimate, not yet measured):** 1 container beside the Ollama one; about 1-2 GB of RAM before any model; local model via Ollama, no API key; first-run creates an admin account locally.
- **Transport:** verified in source at the tag: `backend/open_webui/socket/main.py` serves
  `socketio.ASGIApp(..., socketio_path='/ws/socket.io')`, and `chat_completion` in `main.py`
  takes a `session_id`. **Not verified:** which requests deliver their reply over the socket and
  which over the HTTP response. Finding that out is part of the run.
- **Question only this run answers:** *What does `askback` do when the reply it wants arrives on
  a transport it does not speak?* Streaming is unsupported today. A clear refusal naming the
  transport is an acceptable result; a hang, a truncated reply graded as complete, or a green
  run is not. The outcome sizes step 2.0.

### 3. PrivateGPT (slot c: retrieval and citations)

- **Repo:** https://github.com/zylon-ai/private-gpt (Apache-2.0)
- **Pinned:** `v1.0.1`, commit `88d7e259a455625796e3d5cd829c7de78f076231` (2026-06-18)
- **Last commit:** 2026-11-18 (default branch head `2373cc7b91`; four days before this check).
  The pinned tag is older, 2026-06-18, so the pin is five months behind head. Active, but
  re-check that `v1.0.1` is still the latest release when step 1.3 starts.
- **Stood up:** its README says it needs an OpenAI-compatible inference server and names Ollama
  as the easiest, so it sits in front of the container from entry 1. The README points to a
  quickstart for Docker; **we have not yet read that quickstart**, so the Docker recipe is
  unconfirmed. If it needs a hosted service, this entry is replaced, not waived.
- **Cost to stand up (estimate, not yet measured):** 1 service in front of the Ollama container (install path unconfirmed, see above); modest RAM beyond the model; local model, no API key unless its quickstart says otherwise.
- **Transport:** the README describes a messages API "following the Claude API model" with
  streaming and "retrieval with citations". **Wire shape unverified**: exact route, whether the
  non-streaming form carries the citations, and how a document is identified are all to be read
  from the API reference in step 1.3.
- **Note on version:** v1.0 is a rewrite of the project the older "PrivateGPT" posts describe. Do
  not use any pre-1.0 tutorial.
- **Question only this run answers:** *Can the citation resolver and the RAG evaluators ground a
  claim against chunks and citations whose shape we did not design?* The resolver's idea of a
  "document" was developed against HR text (`docs/HR-COUPLING-AUDIT.md`); this is the first
  other citation format it meets.

### 4. Onyx (slot d: users and sessions)

- **Repo:** https://github.com/onyx-dot-app/onyx (licence reported as `NOASSERTION` by the GitHub
  API: not a single standard licence. Read it before redistributing anything.)
- **Pinned:** `v5.1.3`, commit `3835aa4294198de48d318536081ea09fbde7aea6` (2026-11-16)
- **Last commit:** 2026-11-22, checked 2026-11-22
- **Stood up:** its README lists a Docker deployment in a standard mode (vector index, workers,
  model servers) and a "lite" mode without the vector index. Use standard: lite has no retrieval,
  which would make this a second slot-a run. Heavy (many containers); record the resource
  floor in the record.
- **Cost to stand up (estimate, not yet measured):** the heaviest of the five: standard mode is a multi-container stack (API, web, background workers, vector index, model servers), plausibly 8-16 GB of RAM and several GB of images; local model via Ollama or a key we do not want to depend on; a local admin account per test identity.
- **Transport:** verified in source at the tag: `chat_backend.py` has `POST /create-chat-session`,
  `GET /get-chat-session/{session_id}`, `GET /get-user-chat-sessions`, and imports
  `StreamingResponse` and a `gather_stream_full` helper. **Not verified:** the send-message
  route, and whether a non-streaming form exists. Authentication is per-user.
- **Question only this run answers:** *Do `askback`'s identity and session assumptions survive an
  agent where a conversation is a server-side object with an owner?* Cross-principal isolation is
  suite #1 for HR and was only ever run against a simulator that has a field for it. Here the
  harness must hold two real logins, and "did user B see user A's chat" becomes a real question.
  Onyx is also a retrieval agent; that overlap with entry 3 is accepted, the primary reason it
  is here is sessions and ownership.

### 5. Dify (slot e: opinionated framework API)

- **Repo:** https://github.com/langgenius/dify (licence reported as `NOASSERTION` by the
  GitHub API: not a single standard licence; read it)
- **Pinned:** `1.20.0`, commit `7bef4e1b2dc21b2c694d3243425dd3fa6288f916` (2026-11-18)
- **Last commit:** 2026-11-22, checked 2026-11-22
- **Stood up:** `docker compose up -d` from the repo's `docker/` directory at the pinned tag (its
  README, which also asks for Docker Compose v2.35.0 or later and OpenSSL). An app must be built
  in its UI first, and that app definition is exported and committed so the run is reproducible.
- **Cost to stand up (estimate, not yet measured):** a docker compose stack of roughly ten containers (API, worker, web, database, cache, vector store, sandbox); plausibly 4-8 GB of RAM; model via the Ollama container, no API key; one app built by hand in its UI.
- **Transport:** verified in source at the tag: the service API
  (`controllers/service_api/app/completion.py`) takes a `response_mode` of `"blocking"` or
  `"streaming"`, a `conversation_id` the caller passes back on later turns, and a `user`
  identity. Run in `blocking`. **Not verified:** the exact field the response uses for retrieved
  context.
- **Question only this run answers:** *Is `askback`'s request and response mapping expressive
  enough for an API we do not control, with its own idea of conversation state, and does it fail
  loudly where it is not?* The earlier bug where the HTTP mapping silently dropped chunk
  metadata (invariant 8) was found by us reading our own code. This is the first mapping job
  done against someone else's schema.

## Considered and rejected

Rejected on the maintained requirement, which is why a list of "obvious" picks is not the list:

- **Chainlit** (Socket.IO; the natural slot-b agent). The GitHub API reports the repository
  **archived** (last release 2.12.0, 2026-08-25; last commit 2026-08-26). A web search describes
  it as community-maintained since May 2025 and did not show it archived; the API is the more
  direct source and wins, but the two disagree and this should be rechecked before anyone reads
  this as final. Archived is read-only: an agent whose bugs cannot be fixed upstream is a poor
  target for findings.
- **Flowise** (framework API). Reported archived; last commit 2026-08-13.
- **Rasa Open Source** (REST webhook). Not archived, but the last commit is 2025-12-18 and the
  last release `3.6.21` is 2025-01-14, and Rasa's own policy says the community goal is the most
  recent minor release. Development has moved to Rasa Pro, which is not the same product.

## What this shortlist does not cover, and why that is acceptable

- **Tool-calling agents that take side effects.** None of the five is chosen to exercise
  side-effecting tools, so the "empty side-effecting-tool list" class of bug (invariant 8) is
  covered only by what Dify and Onyx happen to expose. Acceptable: a side-effect check needs an
  agent with a real, safely resettable side effect, and that is a research task of its own.
- **Non-English and non-HR domains.** All five are tested with English suites. The domain
  coupling audit says the tuning is HR-shaped; this shortlist will show whether it breaks on
  generic text but not on another language. Acceptable for a first five; the verdict is "does
  it work at all on someone else's agent."
- **Voice, multimodal, file-upload agents.** Out of scope for a text-conversation framework.
- **Scale and rate limits.** Local containers have none of the throttling, token expiry or
  concurrency limits a hosted agent has (CLAUDE.md §8, question 6). Acceptable: those need a
  real deployment, and every candidate here was chosen so that nobody's production system is
  touched.
- **A purely WebSocket-native agent, and an SSE-only one.** Slot b is satisfied by Socket.IO
  alone. Whether `askback` needs separate WebSocket and SSE transports is what step 2.0 is meant
  to decide from this evidence, not from a guess.
- **Representativeness.** Five agents is five agents. Most of them sit on top of the same
  local model through Ollama, so their *answers* will correlate; the shortlist is chosen for
  differing wire formats, not independent answer quality.
- **Verification depth.** The "not verified" items above are deliberately left for the per-agent
  steps. Step 1.1 confirms that each candidate exists, is maintained, can be pinned and
  speaks roughly what is claimed. It does not claim any of them has been run.

Costs above are estimates written before any run; each per-agent record replaces its line with
what was measured. If a stack cannot be stood up on the machine available, the record says so and
the entry is replaced, not skipped.
