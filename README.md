# EVA4

EVA4 is an experience-driven AI assistant. It runs an agent loop against any
OpenAI-compatible chat-completions endpoint (Ollama, vLLM, llama.cpp server,
OpenAI itself, etc.), keeps a searchable long-term memory in SQLite, and
recursively decomposes goals into subtasks until they're atomic enough to
execute with tools.

The agent's own behavior — when to stop, how to break down a task, what to
remember — is governed by `data/core.md`, a plain-text policy file the agent
can rewrite itself (`core_update`) as it learns better strategies.

## Features

- **Recursive task decomposition** — the LLM decides whether a goal needs to
  be split into subtasks, with no fixed depth limit.
- **Long-term memory** — facts, experiences, workflows, and opinions are
  stored in SQLite with tags, importance scores, and semantic search
  (sentence-transformers embeddings + cosine similarity, with keyword
  fallback if embeddings are unavailable).
- **Task history / episodic memory** — recent task records are automatically
  injected into context; older ones are archived, and very old batches get
  compressed into LLM-written summaries.
- **Tool use** — shell exec, Python exec (subprocess-isolated), file
  read/write/list, grep/find, HTTP requests, web search/fetch, and memory
  read/write tools.
- **Self-updating policy** — the agent can rewrite `core.md` mid-task when it
  finds a better way of working; previous versions are snapshotted.
- **Resumable, interruptible tasks** — Ctrl-C pauses a running task so you
  can inject new instructions; a second Ctrl-C stops it. Interrupted task
  state is checkpointed and recoverable.
- **i18n** — CLI output language is controlled by `EVA_LANG` (`en` default,
  `zh` supported); LLM-facing prompts are always English regardless of
  `EVA_LANG`, since the model's reply language is driven by the conversation
  content, not the scaffold prompt.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # edit EVA_LLM_BASE_URL / EVA_LLM_MODEL etc.
python3 eva.py
```

On first run, if no `data/core.md` exists, EVA4 asks the configured LLM to
generate its own policy file; if that fails it falls back to the bundled
default in `data/core.md`.

## Configuration

All configuration is via environment variables (see `.env.example`), loaded
from a local `.env` file if present:

| Variable | Default | Purpose |
|---|---|---|
| `EVA_LANG` | `en` | CLI display language (`en` / `zh`) |
| `EVA_LLM_BASE_URL` | `http://localhost:8003/v1` | OpenAI-compatible endpoint |
| `EVA_LLM_MODEL` | `qwen3.6-27b-awq-int4` | Model name passed to the endpoint |
| `EVA_LLM_API_KEY` | `none` | API key, if the endpoint requires one |
| `EVA_EMBED_MODEL` | language-dependent | sentence-transformers model for memory embeddings |

### Customizing the agent's policy without forking

`data/core.md` is the generic default shipped with this repo. If a file
named `data/core.local.md` exists, it takes precedence over `data/core.md`
and is gitignored — use it to run your own private policy customizations
(domain-specific instructions, business workflows, etc.) without ever
touching the tracked default or needing to fork the public policy file.

## Project layout

```
eva.py                 CLI entry point, slash commands, bootstrap
config.py              env-driven configuration
i18n.py                 CLI display strings (EVA_LANG-driven)
llm_client.py           OpenAI-compatible chat client
loops/
  agent_loop.py         core LLM ↔ tool execution loop
  task_runner.py        recursive decompose/execute/merge
  _upgrade_eval.py       detects repeated failures, asks the LLM to self-diagnose
memory/
  database.py           SQLite schema/migrations
  store.py               long-term memory (write/search/update/delete)
  embedder.py             sentence-transformers wrapper
  task_memory.py         episodic task history (active/archived/summarized tiers)
tools/                  shell/python/file/search/http/web/memory tool implementations
utils/display.py       terminal color helpers
```

## Roadmap

- [ ] **Popular LLM support** — first-class integration with OpenAI, Anthropic Claude, Google Gemini, and other hosted providers (currently works with any OpenAI-compatible endpoint)
- [ ] **Popular messaging platforms** — Telegram, Slack, Discord, and other chat platforms (WeCom/企业微信 is the first adapter; more coming)

## License

MIT — see [LICENSE](LICENSE).
