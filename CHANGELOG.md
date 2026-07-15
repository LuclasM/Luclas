# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed
- **Task execution model**: replaced upfront recursive decomposition (plan
  the full subtask tree before executing any of it, then run each subtask
  in its own disconnected conversation) with a single continuous main
  thread plus on-demand branching. A task is now one `run_agent` conversation
  from start to finish; the LLM calls a new `delegate_subtask` tool only when
  a chunk of work is genuinely independent enough to warrant its own
  sub-conversation, and only the branch's final result folds back in — not
  its raw tool-call transcript. Decisions about what (if anything) to branch
  are now made from what has actually happened so far, instead of a static
  plan drawn up before any of it ran, which is what let later steps overlap
  with or leave gaps around earlier ones. Branches can run in parallel (the
  LLM can call `delegate_subtask` more than once per turn) and can
  themselves branch further, with a soft 3-level nesting cap past which the
  LLM has to justify going deeper. `task_records.tree`'s on-disk shape is
  unchanged, so existing history/`/log` data reads exactly as before.

## [0.2.1] - 2026-07-13

### Added
- **CI** (`.github/workflows/ci.yml`) — every push/PR now runs a syntax
  check, a real import check against installed dependencies (would have
  caught the missing fastapi/pydantic/pycryptodome deps from 0.2.0), and
  a minimal lint pass restricted to actual errors, not style nitpicks.
- **CONTRIBUTING.md** — dev setup, what CI checks, PR expectations.
- **First-run setup** — running `luclas` with no `.env` present now
  launches the setup wizard automatically instead of starting cold.

### Fixed
- `is_available()`'s LLM health check used a 5s timeout, too tight for
  LAN/remote deployments — chat completions succeed over the same path
  with a 300s timeout, so the startup check would falsely report
  "offline" on a slightly slower connection. Bumped to 15s.
- Subtask execution context (task tree, prior-step results) was being
  injected into the system message, with only the bare goal in the user
  turn — models respond more literally to the user turn, so this content
  was easy to ignore. Diagnosed from a real failed task where subtasks
  repeatedly re-fetched data already gathered by a prior step until
  manually interrupted. Moved this content into the user message instead,
  and reworded the reuse instruction to cover actual facts, not just
  technical artifacts (paths/cookies/IDs).
- Feedback now folds into the AAR experience memory a task actually
  wrote (content appended, credibility bumped to reflect human review)
  instead of always filing a disconnected standalone record.
- Two f-strings in `task_runner.py` used backslash escapes inside `{}`
  expressions — legal since Python 3.12 (PEP 701) but a SyntaxError on
  3.11.
- The version shown at startup and the `__version__` bumped by the
  pre-commit hook had drifted apart, showing different numbers for no
  reason. Startup now reads `__version__` directly.

## [0.2.0] - 2026-07-11

### Added
- **Multi-model routing** — configure several models in `data/models.json`;
  Luclas classifies each task's complexity/type and routes it accordingly,
  escalating to a stronger model on failure. `/models edit` gives an
  interactive TUI for managing the config instead of hand-editing JSON.
- **Local LLM auto-detection** — the setup wizard scans common local ports
  for a running Ollama, LM Studio, or vLLM server and offers it directly,
  instead of requiring the base URL/port to already be known.
- **Feedback loop** — after a non-routine task (first time doing something,
  mid-task errors, long-running, large/multi-step, or an open-ended result),
  Luclas asks how it went, saves the exchange to memory, and — given a
  corrected approach and explicit confirmation — redoes the task differently.
  Routine tasks are skipped automatically.
- **WhatsApp and Discord messaging adapters**, alongside the existing WeCom
  one, plus an interactive setup wizard (`luclas setup`) covering LLM,
  messaging platform, and usage-preference configuration.
- **Memory source/credibility fields** — every memory can now record where it
  came from (first-hand experience, user instruction, learning material,
  web, etc.) and a 1-10 credibility score, so retrieval can distinguish a
  verified fact from a guess.

### Changed
- Project renamed **EVA4 → Luclas** (env var prefix `EVA_` → `LUC_`, module
  layout `eva/`/`luc/` → `luclas/`, launcher → `luclas.sh`).
- The three messaging adapters (WeCom/WhatsApp/Discord) now share one
  dispatch layer (`adapters/dispatch.py`) for command/task routing and reply
  text — previously each adapter hardcoded its own language regardless of
  `LUC_LANG`; now all three respect it consistently.
- Dropped the redundant `tasks` DB table — it duplicated `task_records`
  (same id, goal, and a status/result/log entirely derivable from the task
  tree). `task_records` gained a `status` column instead.
- `credibility` switched from a `high`/`medium`/`low` text field to a 1-10
  numeric scale, matching `importance`'s existing convention.

### Fixed
- The task result is now shown to the user *before* the feedback prompt,
  not after — the prompt used to print first, making it look like the
  agent was asking a question before it had even finished.
- Mid-task `ask_user` calls and the post-task feedback prompt now actually
  reach the user on messaging channels. Previously, `ask_user` in a
  non-interactive context raised an exception that a generic tool-error
  handler silently swallowed — the LLM saw a confusing error and the
  question never reached anyone.
- Failed subtasks are now traceable after the fact via `/history` (expands
  the task tree when a failure occurred) and `/log <id>` (links to the
  failed subtask's full tool-call transcript) — previously a failure was
  only visible live in the terminal scroll at the moment it happened.
- `TaskMemory.save()` no longer crashes if `tree` is explicitly `None`.
- `core_update()` (core.md) and `.env` writes are now atomic (temp file +
  rename) with automatic backups/snapshots beforehand, so a crash mid-write
  can no longer truncate or lose either file. `core_update()` also detects
  if core.md changed on disk since it was last loaded (e.g. a manual edit)
  and flags it instead of silently overwriting.
- Messaging sends now retry with backoff on transient failures (connection
  errors, timeouts, 429, 5xx) instead of silently dropping the message on
  the first network blip.

### Security
- **WhatsApp webhook signature verification** — incoming webhook payloads
  are now verified against Meta's `X-Hub-Signature-256` (HMAC-SHA256).
  Previously unchecked, meaning anyone who discovered the callback URL
  could have injected fake messages.
- The Discord bot's connection lifecycle now has a capped-backoff retry
  loop with logging, instead of dying silently on an uncaught exception in
  a bare daemon thread (bad token, transient network failure at startup).

## [0.1.0]

Initial public release: recursive task decomposition, long-term + episodic
memory, WeCom messaging adapter, HTTP API, scheduled tasks, i18n (en/zh).
