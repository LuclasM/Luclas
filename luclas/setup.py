"""
setup.py — luclas setup wizard.

Guides the user through LLM configuration, messaging platform,
and usage preferences. Writes results to .env and data/user_direction.md.
"""

import os
import sys

from local_llm_detect import (fetch_openai_models, fetch_ollama_models, scan_local_llm_servers,
                              DETECTED_PROVIDER_LABELS)


# ── I/O helpers ──────────────────────────────────────────────────────────────

def _input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Setup cancelled.")
        sys.exit(0)


def _ask(label: str, default: str = "") -> str:
    hint = f"  (default: {default})" if default else ""
    val = _input(f"  {label}{hint}\n  → ")
    return val if val else default


def _choose(title: str, options: list[tuple[str, str]], allow_skip: bool = False) -> str:
    print(f"\n  {title}")
    for i, (_, label) in enumerate(options, 1):
        print(f"    {i}. {label}")
    if allow_skip:
        print(f"    0. Skip")
    while True:
        raw = _input("\n  Enter number: ")
        if allow_skip and raw == "0":
            return ""
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        print("  Invalid choice, try again.")


def _section(title: str) -> None:
    print("\n" + "━" * 54)
    print(f"  {title}")
    print("━" * 54)


# ── Step 1: LLM ──────────────────────────────────────────────────────────────

_LLM_PRESETS: dict[str, tuple[str, str]] = {
    # key: (base_url, api_key_hint)
    "openai":       ("https://api.openai.com/v1",
                     "sk-...  (platform.openai.com/api-keys)"),
    "deepseek":     ("https://api.deepseek.com/v1",
                     "sk-...  (platform.deepseek.com)"),
    "gemini":       ("https://generativelanguage.googleapis.com/v1beta/openai",
                     "AIza...  (aistudio.google.com)"),
    "moonshot":     ("https://api.moonshot.cn/v1",
                     "sk-...  (platform.moonshot.cn)"),
    "siliconflow":  ("https://api.siliconflow.cn/v1",
                     "sk-...  (cloud.siliconflow.cn)"),
    "zhipu":        ("https://open.bigmodel.cn/api/paas/v4",
                     "...  (open.bigmodel.cn)"),
    "ollama":       ("http://localhost:11434/v1",  "ollama"),
    "lmstudio":     ("http://localhost:1234/v1",   "lm-studio"),
    "vllm":         ("http://localhost:8000/v1",   "none"),
    "custom":       ("", ""),
}

_LLM_KNOWN_MODELS: dict[str, list[str]] = {
    "openai": [
        "gpt-4o", "gpt-4o-mini",
        "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
        "o3", "o3-mini", "o4-mini",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    "gemini": [
        "gemini-2.5-pro", "gemini-2.5-flash",
        "gemini-2.0-flash", "gemini-2.0-flash-lite",
        "gemini-1.5-pro", "gemini-1.5-flash",
    ],
    "moonshot": [
        "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k",
    ],
    "siliconflow": [
        "Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen2.5-72B-Instruct",
        "Qwen/QwQ-32B",
        "deepseek-ai/DeepSeek-V3",
        "deepseek-ai/DeepSeek-R1",
        "meta-llama/Meta-Llama-3.1-8B-Instruct",
        "meta-llama/Meta-Llama-3.1-70B-Instruct",
    ],
    "zhipu": [
        "glm-4-flash", "glm-4-flash-250414",
        "glm-4", "glm-4-plus", "glm-4-long",
        "glm-z1-flash",
    ],
}

_LLM_OPTIONS = [
    ("openai",      "OpenAI          (GPT-4o, GPT-4.1, o3 …)"),
    ("deepseek",    "DeepSeek        (deepseek-chat, deepseek-reasoner)"),
    ("gemini",      "Google Gemini   (gemini-2.5-flash …)"),
    ("moonshot",    "Moonshot / Kimi (moonshot-v1-8k …)"),
    ("siliconflow", "SiliconFlow     (hosted open-source models)"),
    ("zhipu",       "Zhipu / GLM     (glm-4-flash …)"),
    ("ollama",      "Ollama          (local · http://localhost:11434)"),
    ("lmstudio",    "LM Studio       (local · http://localhost:1234)"),
    ("vllm",        "vLLM            (local · http://localhost:8000)"),
    ("custom",      "Other / Custom endpoint"),
]


def _pick_model(provider: str, base_url: str, api_key: str = "") -> str:
    """Show a model menu; fall back to manual entry."""
    models: list[str] = []

    if provider == "ollama":
        print("\n  Detecting installed Ollama models…", end="", flush=True)
        models = fetch_ollama_models()
        print(" done." if models else " (could not reach Ollama)")
    elif provider in ("lmstudio", "vllm"):
        print("\n  Detecting loaded models…", end="", flush=True)
        models, _ = fetch_openai_models(base_url, api_key)
        print(" done." if models else " (server not reachable or no models loaded)")
    elif provider in _LLM_KNOWN_MODELS:
        models = _LLM_KNOWN_MODELS[provider]

    if not models:
        return _ask("Model name", "")

    options = [(m, m) for m in models] + [("__other__", "Other (enter manually)")]
    choice = _choose("Select a model:", options)
    if choice == "__other__":
        return _ask("Model name", "")
    return choice


def _step_llm() -> dict[str, str]:
    _section("Step 1 · LLM Configuration")

    print("\n  Scanning for locally running LLM servers…", end="", flush=True)
    detected = scan_local_llm_servers()
    print(f" found {len(detected)}." if detected else " none found.")

    detected_options: list[tuple[str, str]] = []
    detected_by_key: dict[str, dict] = {}
    for i, d in enumerate(detected):
        key = f"__detected_{i}__"
        label = (
            f"✓ Detected: {DETECTED_PROVIDER_LABELS.get(d['provider'], 'Local server')} "
            f"at {d['base_url']}  ({len(d['models'])} model(s))"
        )
        detected_options.append((key, label))
        detected_by_key[key] = d

    provider = _choose("Which LLM provider are you using?", detected_options + _LLM_OPTIONS)

    if provider in detected_by_key:
        d = detected_by_key[provider]
        options = [(m, m) for m in d["models"]] + [("__other__", "Other (enter manually)")]
        model = _choose("Select a model:", options)
        if model == "__other__":
            model = _ask("Model name", "")
        return {
            "LUC_LLM_BASE_URL": d["base_url"],
            "LUC_LLM_MODEL":    model,
            "LUC_LLM_API_KEY":  "none",
        }

    base_url_default, key_hint = _LLM_PRESETS[provider]

    if provider == "ollama":
        print("\n  Make sure `ollama serve` is running.")
    elif provider == "lmstudio":
        print("\n  Enable 'Local Server' in LM Studio settings.")
    elif provider not in ("vllm", "custom"):
        print(f"\n  API key: {key_hint}")

    print()
    base_url = _ask("API base URL", base_url_default)
    # key_hint is a real usable default only for local providers (e.g. "none"
    # for vllm, "ollama" for ollama — those servers typically don't check the
    # key at all). For cloud providers it's a *pattern description*
    # ("sk-...  (platform.openai.com/api-keys)"), already shown above via
    # print(f"API key: {key_hint}") — using it as the _ask() default meant a
    # blank Enter silently wrote that literal placeholder string into .env as
    # if it were a real key, and every LLM call would then fail with what
    # looks like an ordinary 401 with no indication the key was never set.
    default_key = (key_hint.split()[0] if key_hint else "none") if provider in ("ollama", "lmstudio", "vllm") else ""
    api_key  = _ask("API key", default_key)
    model    = _pick_model(provider, base_url, api_key)

    return {
        "LUC_LLM_BASE_URL": base_url,
        "LUC_LLM_MODEL":    model,
        "LUC_LLM_API_KEY":  api_key,
    }


# ── Step 2: Messaging platform ────────────────────────────────────────────────

_MSG_OPTIONS = [
    ("wecom",    "WeCom / 企业微信"),
    ("whatsapp", "WhatsApp  (Meta Business Cloud API)"),
    ("discord",  "Discord   (Bot)"),
    ("none",     "Skip — use terminal only"),
]


def _setup_wecom() -> dict[str, str]:
    print("\n  You'll need the following from the WeCom admin panel:")
    print("    · Corp ID             企业管理后台 → 我的企业 → 企业ID")
    print("    · App Agent ID        应用管理 → 自建 → AgentId")
    print("    · App Secret          应用管理 → 自建 → Secret")
    print("    · Token & AES Key     应用 → 接收消息 → 设置API接收")
    print()

    corp_id  = _ask("WECOM_CORP_ID")
    agent_id = _ask("WECOM_AGENT_ID")
    secret   = _ask("WECOM_SECRET")
    token    = _ask("WECOM_TOKEN")
    aes_key  = _ask("WECOM_ENCODING_AES_KEY")
    return {
        "WECOM_CORP_ID":          corp_id,
        "WECOM_AGENT_ID":         agent_id,
        "WECOM_SECRET":           secret,
        "WECOM_TOKEN":            token,
        "WECOM_ENCODING_AES_KEY": aes_key,
    }


def _setup_whatsapp() -> dict[str, str]:
    print("\n  Uses the Meta WhatsApp Business Cloud API (free tier available).")
    print("  Steps to get credentials:")
    print("    1. Go to developers.facebook.com → My Apps → Create App")
    print("    2. Add 'WhatsApp' product to your app")
    print("    3. Phone Number ID    → WhatsApp → API Setup")
    print("    4. Access Token       → WhatsApp → API Setup (temporary) or")
    print("                           System User token from Business Settings")
    print("    5. Webhook Verify Token — any string you choose; enter it here")
    print("       and use the same value when registering the webhook in Meta.")
    print("    6. App Secret         → App Dashboard → Settings → Basic")
    print("                           (used to verify incoming webhook signatures)")
    print()

    phone_id     = _ask("WHATSAPP_PHONE_NUMBER_ID")
    access_token = _ask("WHATSAPP_ACCESS_TOKEN")
    verify_token = _ask("WHATSAPP_VERIFY_TOKEN  (choose any secret string)")
    app_secret   = _ask("WHATSAPP_APP_SECRET")
    return {
        "WHATSAPP_PHONE_NUMBER_ID": phone_id,
        "WHATSAPP_ACCESS_TOKEN":    access_token,
        "WHATSAPP_VERIFY_TOKEN":    verify_token,
        "WHATSAPP_APP_SECRET":      app_secret,
    }


def _setup_discord() -> dict[str, str]:
    print("\n  Uses a Discord Bot. Steps to get credentials:")
    print("    1. Go to discord.com/developers/applications → New Application")
    print("    2. Bot → Add Bot → copy the Token")
    print("    3. OAuth2 → URL Generator → scope 'bot' → permissions:")
    print("       Send Messages, Read Message History → invite bot to your server")
    print("    4. Enable 'Message Content Intent' under Bot → Privileged Intents")
    print()

    bot_token  = _ask("DISCORD_BOT_TOKEN")
    channel_id = _ask("DISCORD_CHANNEL_ID  (right-click channel → Copy Channel ID)")
    return {
        "DISCORD_BOT_TOKEN":  bot_token,
        "DISCORD_CHANNEL_ID": channel_id,
    }


def _step_messaging() -> dict[str, str]:
    _section("Step 2 · Messaging Platform")
    print("  Connect a messaging platform so Luclas can push")
    print("  results and progress updates outside the terminal.")

    platform = _choose("Which platform?", _MSG_OPTIONS)
    if platform == "none":
        return {}

    result: dict[str, str] = {}

    if platform == "wecom":
        result.update(_setup_wecom())
    elif platform == "whatsapp":
        result.update(_setup_whatsapp())
    elif platform == "discord":
        result.update(_setup_discord())

    # Shared: public API URL and optional auth key
    print()
    print("  LUC_API_BASE — the public URL this server is reachable at.")
    if platform == "wecom":
        print("  WeCom will POST to: {LUC_API_BASE}/wecom/callback")
    elif platform == "whatsapp":
        print("  Meta will POST to:  {LUC_API_BASE}/whatsapp/callback")
    elif platform == "discord":
        print("  (Discord uses a bot connection, no inbound webhook needed.)")
    print()
    api_base = _ask("LUC_API_BASE", "https://your-domain.com")
    result["LUC_API_BASE"] = api_base

    print()
    print("  LUC_API_KEY — optional auth key for the HTTP API. Leave empty to skip.")
    api_key = _ask("LUC_API_KEY", "")
    if api_key:
        result["LUC_API_KEY"] = api_key

    return result


# ── Step 3: Usage preferences ─────────────────────────────────────────────────

_LANG_OPTIONS = [
    ("en", "English"),
    ("zh", "中文 (Chinese)"),
]

_ROLE_OPTIONS = [
    ("engineer",   "Software / Systems Engineer"),
    ("researcher", "Researcher / Analyst"),
    ("manager",    "Manager / Executive"),
    ("creator",    "Writer / Content Creator"),
    ("student",    "Student / Learner"),
    ("other",      "Other"),
]

_FOCUS_OPTIONS = [
    ("coding",      "Coding & technical work"),
    ("research",    "Research & information gathering"),
    ("writing",     "Writing & content creation"),
    ("operations",  "Operations & task scheduling"),
    ("mixed",       "A mix of everything"),
]

_STYLE_OPTIONS = [
    ("concise",  "Concise — just the answer, no padding"),
    ("detailed", "Detailed — explain the reasoning"),
    ("balanced", "Balanced — brief normally, detailed when asked"),
]


def _step_preferences() -> tuple[dict[str, str], str]:
    """Returns (env_vars, direction_note_for_core)."""
    _section("Step 3 · Your Usage Profile")
    print("  Your answers help Luclas calibrate its working style.")
    print("  You can always change this later via /core or direct .env edits.")

    lang  = _choose("Preferred CLI language?",  _LANG_OPTIONS)
    role  = _choose("Your role?",                _ROLE_OPTIONS)
    focus = _choose("Primary use case?",         _FOCUS_OPTIONS)
    style = _choose("Preferred response style?", _STYLE_OPTIONS)

    print()
    extra = _ask(
        "Anything else Luclas should know about you or your work? (optional, press Enter to skip)",
        "",
    )

    role_label  = dict(_ROLE_OPTIONS)[role]
    focus_label = dict(_FOCUS_OPTIONS)[focus]
    style_label = dict(_STYLE_OPTIONS)[style]

    direction = (
        f"User profile:\n"
        f"- Role: {role_label}\n"
        f"- Primary focus: {focus_label}\n"
        f"- Response style preference: {style_label}\n"
    )
    if extra:
        direction += f"- Additional context: {extra}\n"

    return {"LUC_LANG": lang}, direction


# ── .env writer ───────────────────────────────────────────────────────────────

def _write_env(base_dir: str, new_vars: dict[str, str]) -> None:
    env_path = os.path.join(base_dir, ".env")
    lines: list[str] = []
    updated: set[str] = set()

    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                stripped = line.rstrip("\n")
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    if k in new_vars:
                        lines.append(f"{k}={new_vars[k]}\n")
                        updated.add(k)
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")

        # Back up the previous .env before touching it — this file holds every
        # configured secret (LLM key, messaging platform credentials, etc.),
        # so an interrupted write here would otherwise lose all of it at once.
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{env_path}.bak.{ts}"
        with open(env_path, encoding="utf-8") as src, open(backup_path, "w", encoding="utf-8") as dst:
            dst.write(src.read())

    new_keys = {k: v for k, v in new_vars.items() if k not in updated}
    if new_keys:
        if lines and lines[-1].strip():
            lines.append("\n")
        for k, v in new_keys.items():
            lines.append(f"{k}={v}\n")

    # Atomic write: temp file + rename, so a crash mid-write can't leave a
    # truncated .env behind (the pre-existing backup above covers the rest).
    tmp_path = f"{env_path}.tmp-{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, env_path)


def _write_direction(base_dir: str, note: str) -> None:
    data_dir = os.path.join(base_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "user_direction.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# User direction\n\n")
        f.write(note)
        f.write("\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(base_dir: str) -> None:
    import sys as _sys
    from i18n import ascii_banner
    print(ascii_banner())
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │              Setup Wizard                        │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print("  Steps: 1) LLM models  2) Messaging platform  3) Usage preferences")
    print("  Press Ctrl-C at any time to cancel.")

    env_vars: dict[str, str] = {}

    # ── Step 1: LLM models (interactive manager) ──────────────────────────────
    _section("Step 1 · LLM Model Configuration")
    print("  Add one or more LLM models. Models are saved to data/models.json.")
    print("  Navigation: UP/DOWN arrows · a=add · e/Enter=edit · d=delete · q=done")
    print()
    _input("  Press Enter to open the model manager…")

    if _sys.stdin.isatty():
        from model_manager import run as _run_model_mgr, _load as _load_models
        _run_model_mgr()
        # llm_client.py falls back to LUC_LLM_BASE_URL/LUC_LLM_MODEL/
        # LUC_LLM_API_KEY before any task has run set_goal() (e.g. the
        # "online/offline" check right at CLI startup) — mirroring the
        # first configured model into those env vars means that check has a
        # real endpoint to test against right after setup finishes, instead
        # of reporting "offline" against an empty base_url even though
        # models.json is fully and correctly configured. Also keeps the
        # documented single-model fallback meaningful if models.json is ever
        # emptied out later. Routing itself is still fully governed by
        # models.json whenever it has entries — this doesn't change that.
        configured = _load_models()
        if configured:
            first = configured[0]
            env_vars["LUC_LLM_BASE_URL"] = first.get("base_url", "")
            env_vars["LUC_LLM_MODEL"]    = first.get("name", "")
            env_vars["LUC_LLM_API_KEY"]  = first.get("api_key", "none")
    else:
        print("  (non-interactive — falling back to single-model setup)")
        env_vars.update(_step_llm())

    # ── Steps 2–3: messaging + preferences ───────────────────────────────────
    env_vars.update(_step_messaging())
    pref_vars, direction = _step_preferences()
    env_vars.update(pref_vars)

    _section("Saving configuration")
    _write_env(base_dir, env_vars)
    _write_direction(base_dir, direction)

    print()
    print("  ✓  data/models.json  (LLM models)")
    print("  ✓  .env              (messaging & preferences)")
    print("  ✓  data/user_direction.md")
    print()
    print("  All done. Run `luclas` to start.")
    print()
