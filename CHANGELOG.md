# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Everything below has landed on `master` since the `v0.1.0` tag but hasn't been
cut as a numbered release yet.

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
