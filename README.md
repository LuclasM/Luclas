# Luclas `v0.2.1`

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
- Use `/history` to review what it's been doing and whether the patterns look right.
- Use `core.md` snapshots (`/core history`) to see how its rules have changed.

## How to get the most out of it

Luclas grows faster with real work than with test questions.

- **Give it actual tasks**, not demos. A real failed attempt teaches more than a successful toy example.
- **Correct it in context.** When it makes a mistake mid-task, use Ctrl-C to pause and inject the correction rather than waiting until the end.
- **Don't over-specify.** Luclas is designed to figure out *how* to do things. Tell it *what* you want and let it decide the approach — then correct the approach if it's wrong.
- **Let it fail sometimes.** Failure with explicit feedback is the fastest path to improvement. Don't only give it easy tasks.

## Features

- **Recursive task decomposition** — the LLM decides whether a goal needs subtasks, with no fixed depth limit.
- **Long-term memory** — searchable SQLite store with tags, importance scores, semantic search (sentence-transformers + cosine similarity, keyword fallback), and per-entry **source/credibility** tracking (first-hand experience, user instruction, learning material, web, etc. — 1-10 confidence score) so retrieval can tell a verified fact from a guess.
- **Episodic memory** — recent tasks injected into context; older ones archived; very old batches compressed into LLM-written summaries.
- **Multi-model routing** — configure several local/hosted models in `data/models.json` (`/models edit` for an interactive TUI manager) and Luclas classifies each task's complexity/type to route it to the right one, escalating to a stronger model on failure. Works with a single model too — this is entirely optional.
- **Local LLM auto-detection** — setup scans for a running Ollama, LM Studio, or vLLM server on common local ports and offers it as a ready-to-use option, instead of requiring you to already know the base URL/port.
- **Feedback loop** — after a task that's non-routine (first time doing something, mid-task errors, long-running, large/multi-step, or an open-ended result), Luclas asks how it went, saves the exchange as a memory, and — if you give it a corrected approach and confirm — redoes the task differently. Skipped automatically for simple, routine tasks.
- **Tool use** — shell, Python (subprocess-isolated), file ops, grep/find, HTTP, web search/fetch, memory read/write, scheduled tasks.
- **Messaging adapters** — WeCom (企业微信), WhatsApp, and Discord, all sharing one dispatch layer (command/task routing, reply language via `LUC_LANG`) so behavior is consistent across channels; more platforms coming.
- **HTTP API** — submit tasks asynchronously, poll for results, integrate with external systems.
- **Scheduled tasks** — daily/weekly/one-shot tasks set via natural language; results routed back to the channel that created them.
- **i18n** — CLI display language via `LUC_LANG` (`en` default, `zh` supported).

## Quick start

```bash
pip install -r requirements.txt
./luclas.sh
```

No `.env` yet? First run launches the setup wizard automatically (LLM config, messaging platform, usage preferences) instead of starting cold. You can also run it manually any time with `luclas setup`.

After setup, Luclas generates its own `data/core.md` by asking the LLM to write an initial policy. From that point on, it owns the file.

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

## Project layout

```
luclas.sh                launcher script
luclas/
  luclas.py            CLI entry point, slash commands, bootstrap
  setup.py             interactive setup wizard (luclas setup)
  api.py               HTTP API (FastAPI)
  cron_runner.py       scheduled task runner (crontab-driven)
  config.py            env-driven configuration
  i18n.py              CLI display strings
  llm_client.py        OpenAI-compatible chat client
  llm_router.py        multi-model routing (classify task → pick a model)
  model_manager.py     interactive TUI for data/models.json
  local_llm_detect.py  auto-detect a running Ollama/LM Studio/vLLM server
  loops/
    agent_loop.py      core LLM ↔ tool execution loop
    task_runner.py     recursive decompose/execute/merge, feedback loop
  memory/
    database.py        SQLite schema and migrations
    store.py           long-term memory (source/credibility, semantic search)
    task_memory.py     episodic task history
  tools/               shell/python/file/search/http/web/memory/schedule tools
  adapters/
    dispatch.py        shared command/task routing used by all three below
    wecom.py           WeCom (企业微信) adapter
    whatsapp.py        WhatsApp Business Cloud API adapter
    discord_adapter.py Discord bot adapter
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
