# Luclas `v0.3.2`

[![CI](https://github.com/LuclasM/Luclas/actions/workflows/ci.yml/badge.svg)](https://github.com/LuclasM/Luclas/actions/workflows/ci.yml)

See [CHANGELOG.md](CHANGELOG.md) for what's changed since the last release.

Luclas is a self-evolving AI agent. It starts empty and grows through use.

Most AI assistants are static — same behavior on day one as day one thousand. Luclas is different: every task it runs, every mistake it makes, every correction you give it gets written into a persistent memory and a self-managed policy file (`core.md`). The agent reads its own history before acting, and can rewrite its own operating rules mid-task when it finds a better way.

The result is an assistant that gets meaningfully better at *your specific work* the more you use it — not better at everything in general, but better at the things you actually ask it to do.

## How growth works

Luclas has three layers of self-improvement:

1. **Experience memory** — after every task, what happened, what worked, and what failed is stored in SQLite and retrieved as context for future similar tasks. The agent learns from its own track record.

2. **Self-updating policy** — `data/core.md` is the agent's operating manual. The agent can rewrite it when it identifies a better strategy. Every version is snapshotted, so you can diff the evolution over time.

3. **Zero pre-loaded knowledge** — the database starts empty. Everything Luclas knows about your domain, your workflows, your preferences, it learned from working with you. This means two Luclas instances raised on different work will behave very differently.

## The risk: drift

Because Luclas writes its own rules, it can go wrong in ways a static assistant cannot. If it develops a bad habit — overcautious, sloppy about a certain task type, optimizing for the wrong outcome — that pattern gets reinforced across future tasks until you correct it.

**You are responsible for steering it.** Luclas grows toward whatever behavior you reward with continued use and corrects away from whatever you explicitly push back on.

Practical safeguards:
- Read `data/core.md` periodically. It's a plain-text file; you can edit it directly.
- When Luclas does something wrong, say so explicitly — "that approach was wrong because X" is more useful than silence or a vague "try again".
- Use `/memory` to review what it's stored and whether the patterns look right.
- Use `core.md` snapshots (`/core history`) to see how its rules have changed.

## How to get the most out of it

Luclas grows faster with real work than with test questions.

- **Give it actual tasks**, not demos. A real failed attempt teaches more than a successful toy example.
- **Correct it in context.** When it makes a mistake mid-task, use Ctrl-C to pause and inject the correction rather than waiting until the end.
- **Don't over-specify.** Luclas is designed to figure out *how* to do things. Tell it *what* you want and let it decide the approach — then correct the approach if it's wrong.
- **Let it fail sometimes.** Failure with explicit feedback is the fastest path to improvement. Don't only give it easy tasks.

## Features

- **Recursive task decomposition** — the LLM decides whether a goal needs subtasks, with no fixed depth limit.
- **Persistent conversation** — one durable conversation per user/channel (and one for the CLI), not a fresh context per message. Luclas chats directly in it and only hands real work off to task execution (in the foreground, replacing the conversation with live progress, or in the background, staying responsive) when the message actually calls for it.
- **Cross-channel identity memory** — conversation history normally follows the channel connection (a WeCom account, a browser tab), but anyone can say "I'm Gia, switch to my memory" on any channel and Luclas rebinds that connection to the right person's history instead — typo-tolerant (fuzzy name matching), and it asks who you are the first time a brand-new connection shows up instead of silently treating it as a new stranger. Long-term knowledge (the lessons/episodes stores below) stays shared regardless of whose conversation is active.
- **Long-term memory** — split into episodes (one per finished task or conversation topic) and lessons (facts/experience/opinions, with **source/credibility** tracking — first-hand experience, user instruction, learning material, web, etc., 1-10 confidence score), searchable via semantic search (sentence-transformers + cosine similarity, keyword fallback). Both share one importance/freshness-driven compression scheme that gradually condenses and eventually forgets low-value entries, instead of a fixed archive-then-summarize schedule — see [Retention, compression, and forgetting](#retention-compression-and-forgetting) for how that actually works.
- **Multi-model routing** — configure several local/hosted models in `data/models.json` (`/models edit` for an interactive TUI manager, or the Settings page below) and Luclas classifies each task's complexity/type to route it to the right one, escalating to a stronger model on failure. Works with a single model too — this is entirely optional.
- **Local LLM auto-detection** — setup scans for a running Ollama, LM Studio, or vLLM server on common local ports and offers it as a ready-to-use option, instead of requiring you to already know the base URL/port.
- **Tool use** — shell, Python (subprocess-isolated), file ops, grep/find, HTTP, web search/fetch, memory read/write, scheduled tasks.
- **Messaging adapters** — WeCom (企业微信), WhatsApp, and Discord, all sharing one dispatch layer (command/task routing, reply language via `LUC_LANG`) so behavior is consistent across channels; more platforms coming.
- **Web UI** — a zero-build local dashboard (`/ui`) covering chat (streamed over Server-Sent Events, same push mechanism as the messaging adapters), system management (status, scheduled tasks, `core.md` history, service restart), and settings (`.env` config, model CRUD, local-LLM detection). Meant for LAN/SSH-tunnel access, not public exposure — see [Security](#security-notes) below.
- **HTTP API** — submit tasks asynchronously, poll for results, integrate with external systems.
- **Scheduled tasks** — daily/weekly/one-shot tasks set via natural language; results routed back to the channel that created them.
- **i18n** — CLI display language via `LUC_LANG` (`en` default, `zh` supported).

## Retention, compression, and forgetting

Long-term memory has two kinds of rows — **episodes** (`memory/episode_store.py`, one per finished task or closed conversation topic) and **lessons** (`memory/store.py`, standalone facts/experience/opinions) — but both are aged, compressed, and eventually deleted by the same shared algorithm (`memory/decay.py`), so there's one mental model for how anything fades out.

**Every row has an importance (1-10) and a freshness (0.0-1.0).** Freshness isn't ticked down by a background job — it's computed lazily whenever a row is read, as exponential decay with a 30-day half-life: `freshness = base * 0.5 ^ (days_since_touched / 30)`. What counts as "touched" differs by kind:

- **Lessons refresh on use.** Every time a lesson is returned by `memory_search`, it's treated as a real reference: `access_count` goes up, `importance` rises by 1 (capped at 10), and `freshness` snaps straight back to `1.0`. A lesson that keeps getting cited effectively never decays; one nobody ever searches for drifts toward the bottom on its own.
- **Episodes decay from creation, unconditionally.** Referencing an episode (e.g. pulling it in as task context) bumps its `importance` and `reference_count`, but its freshness clock still runs from when it was created, not from when it was last used — a heavily-cited but old episode still fades, just more slowly than an uncited one at the same age (higher importance offsets freshness in the ranking below).

**Compression runs once a day** (`cron_runner.py`, 04:30, offset from the 04:00 nightly reflection so they don't compete), and only touches up to 10 rows per store per run. It ranks every row by `importance + freshness * 10` (both scaled to roughly the same range) and picks the *lowest*-ranked batch — i.e. whatever's least important and least fresh gets processed first. Each row moves down exactly one granularity step:

```
raw → summary → gist → (deleted)
```

`raw` and `summary` rows get sent to the LLM with an instruction to compress to the next, coarser granularity (losing detail, keeping the conclusion); a row already at `gist` — the coarsest level — is just deleted. Because only one step happens per row per day, nothing disappears in a single run: a genuinely unused memory ages from full detail, through two rounds of summarization, to gone over at least three separate days, and can be pulled back from the brink at any point along the way by being referenced again. Concurrent compression passes (e.g. if you also trigger one manually) can't double-process the same row — candidates are claimed with a conditional `UPDATE` checked by row count before any LLM call happens.

## Quick start

```bash
pip install -r requirements.txt
./luclas.sh
```

No `.env` yet? First run launches the setup wizard automatically (LLM config, messaging platform, usage preferences) instead of starting cold. You can also run it manually any time with `luclas setup`.

After setup, Luclas generates its own `data/core.md` by asking the LLM to write an initial policy. From that point on, it owns the file.

### Web UI

Start the HTTP API (`python api.py`, or however you run it as a service) and open `http://localhost:8080/ui/` — no separate install or build step. The first load asks for the `LUC_API_KEY` you configured (or blank if you didn't set one); it's stored in the browser and reused after that.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LUC_LANG` | `en` | CLI display language (`en` / `zh`) |
| `LUC_LLM_BASE_URL` | `http://localhost:8003/v1` | OpenAI-compatible endpoint |
| `LUC_LLM_MODEL` | `qwen3.6-27b-awq-int4` | Model name |
| `LUC_LLM_API_KEY` | `none` | API key if required |
| `LUC_API_KEY` | _(none)_ | Auth key for the HTTP API |
| `LUC_API_PORT` | `8080` | HTTP API listen port |
| `LUC_EMBED_MODEL` | language-dependent | sentence-transformers model for memory search |

For more than one model, skip `LUC_LLM_*` and configure `data/models.json` instead (`luclas` → `/models edit` for an interactive editor) — Luclas will route each task to the right model automatically.

### Private policy customization

Create `data/core.local.md` to override `data/core.md` without touching the tracked default. This file is gitignored — use it for domain-specific instructions, business workflows, or constraints you don't want in the public repo.

## Security notes

The HTTP API (and by extension the Web UI, which is just another set of routes on the same server) is protected only by the single static `LUC_API_KEY`, checked on every request — there's no per-user auth, rate limiting, or audit log. Several endpoints are intentionally powerful: `/chat` hands the LLM a real task with full tool access, and `/command` runs any CLI slash command server-side. Treat the key like any other credential, and **don't expose this port to the public internet** — run it behind a LAN/SSH tunnel, or if you're already forwarding a domain to it for a messaging webhook (WeCom/WhatsApp both require a publicly reachable callback URL), restrict the forwarding rule to just those callback paths (e.g. an ingress `path` allowlist) rather than the whole port. FastAPI's auto-generated `/docs`/`/openapi.json` are unauthenticated by default too, and inherit whatever exposure the port itself has.

## Project layout

```
luclas.sh                launcher script
luclas/
  luclas.py            CLI entry point, slash commands, bootstrap
  setup.py             interactive setup wizard (luclas setup)
  api.py               HTTP API (FastAPI)
  web_api.py           /api/system/* and /api/settings/* routes for the Web UI
  env_store.py         shared .env read/write (setup wizard + Settings page)
  cron_runner.py       scheduled task runner (crontab-driven) + daily memory compression
  conversation_runner.py  persistent-conversation turn loop (chat vs. dispatch_task)
  config.py            env-driven configuration
  i18n.py              CLI display strings
  llm_client.py        OpenAI-compatible chat client
  llm_router.py        multi-model routing (classify task → pick a model)
  model_manager.py     interactive TUI for data/models.json
  local_llm_detect.py  auto-detect a running Ollama/LM Studio/vLLM server
  static/              Web UI — zero-build HTML/CSS/JS, served at /ui
  loops/
    agent_loop.py      core LLM ↔ tool execution loop
    task_runner.py     recursive decompose/execute/merge
  memory/
    database.py        SQLite schema and migrations
    conversation_store.py  persistent per-user/channel conversation
    identity_store.py  cross-channel identity binding (memory follows the person)
    episode_store.py   episodic memory (one per task or conversation topic)
    store.py           lessons (source/credibility, semantic search)
    decay.py           shared importance/freshness compression algorithm
  tools/               shell/python/file/search/http/web/memory/schedule tools
  adapters/
    dispatch.py        shared command/task routing used by all four below
    wecom.py           WeCom (企业微信) adapter
    whatsapp.py        WhatsApp Business Cloud API adapter
    discord_adapter.py Discord bot adapter
    web.py             Web UI chat push (Server-Sent Events)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — setup, what CI checks, and PR expectations.

## Roadmap

- [ ] **Popular LLM support** — first-class integration with OpenAI, Anthropic Claude, Google Gemini, and other hosted providers
- [x] **Popular messaging platforms** — WeCom, WhatsApp, Discord supported; Telegram, Slack coming
- [ ] **Telegram adapter**
- [ ] **Slack adapter**

## License

MIT — see [LICENSE](LICENSE).
