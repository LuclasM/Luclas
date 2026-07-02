"""
setup.py — luclas setup wizard.

Guides the user through LLM configuration, messaging platform,
and usage preferences. Writes results to .env and data/user_direction.md.
"""

import os
import sys


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

_LLM_PRESETS: dict[str, tuple[str, str, str]] = {
    # key: (base_url, default_model, api_key_hint)
    "openai":       ("https://api.openai.com/v1",
                     "gpt-4o",
                     "sk-...  (from platform.openai.com/api-keys)"),
    "deepseek":     ("https://api.deepseek.com/v1",
                     "deepseek-chat",
                     "sk-...  (from platform.deepseek.com)"),
    "gemini":       ("https://generativelanguage.googleapis.com/v1beta/openai",
                     "gemini-2.0-flash",
                     "AIza...  (from aistudio.google.com)"),
    "moonshot":     ("https://api.moonshot.cn/v1",
                     "moonshot-v1-8k",
                     "sk-...  (from platform.moonshot.cn)"),
    "siliconflow":  ("https://api.siliconflow.cn/v1",
                     "Qwen/Qwen2.5-7B-Instruct",
                     "sk-...  (from cloud.siliconflow.cn)"),
    "zhipu":        ("https://open.bigmodel.cn/api/paas/v4",
                     "glm-4-flash",
                     "  (from open.bigmodel.cn)"),
    "ollama":       ("http://localhost:11434/v1",
                     "qwen2.5:7b",
                     "ollama"),
    "lmstudio":     ("http://localhost:1234/v1",
                     "",
                     "lm-studio"),
    "vllm":         ("http://localhost:8000/v1",
                     "",
                     "token-abc123  (or 'none' if auth disabled)"),
    "custom":       ("", "", ""),
}

_LLM_OPTIONS = [
    ("openai",      "OpenAI          (GPT-4o, GPT-4o-mini …)"),
    ("deepseek",    "DeepSeek        (deepseek-chat, deepseek-reasoner …)"),
    ("gemini",      "Google Gemini   (gemini-2.0-flash …)"),
    ("moonshot",    "Moonshot / Kimi (moonshot-v1-8k …)"),
    ("siliconflow", "SiliconFlow     (hosted open-source models)"),
    ("zhipu",       "Zhipu / GLM     (glm-4-flash …)"),
    ("ollama",      "Ollama          (local · http://localhost:11434)"),
    ("lmstudio",    "LM Studio       (local · http://localhost:1234)"),
    ("vllm",        "vLLM            (local · http://localhost:8000)"),
    ("custom",      "Other / Custom endpoint"),
]


def _step_llm() -> dict[str, str]:
    _section("Step 1 · LLM Configuration")

    provider = _choose("Which LLM provider are you using?", _LLM_OPTIONS)
    base_url_default, model_default, key_hint = _LLM_PRESETS[provider]

    if provider == "ollama":
        print("\n  Tip: make sure `ollama serve` is running.")
        print("  Check available models with: ollama list")
    elif provider == "lmstudio":
        print("\n  Tip: enable 'Local Server' in LM Studio settings.")
    elif provider not in ("vllm", "custom"):
        print(f"\n  API key hint: {key_hint}")

    print()
    base_url = _ask("API base URL", base_url_default)
    model    = _ask("Model name",   model_default)
    api_key  = _ask("API key",      key_hint.split()[0] if key_hint else "none")

    return {
        "LUC_LLM_BASE_URL": base_url,
        "LUC_LLM_MODEL":    model,
        "LUC_LLM_API_KEY":  api_key,
    }


# ── Step 2: Messaging platform ────────────────────────────────────────────────

_MSG_OPTIONS = [
    ("wecom", "WeCom / 企业微信"),
    ("none",  "Skip — use terminal only"),
]


def _step_messaging() -> dict[str, str]:
    _section("Step 2 · Messaging Platform")
    print("  Connect a messaging platform so Luclas can send")
    print("  you results and progress updates outside the terminal.")

    platform = _choose("Which platform?", _MSG_OPTIONS)
    if platform != "wecom":
        return {}

    print("\n  You'll need the following from the WeCom admin panel:")
    print("    · Corp ID            企业管理后台 → 我的企业 → 企业ID")
    print("    · App Agent ID       应用管理 → 自建 → AgentId")
    print("    · App Secret         应用管理 → 自建 → Secret")
    print("    · Callback Token & EncodingAESKey   应用 → 接收消息 → 设置")
    print()

    corp_id  = _ask("WECOM_CORP_ID")
    agent_id = _ask("WECOM_AGENT_ID")
    secret   = _ask("WECOM_SECRET")
    token    = _ask("WECOM_TOKEN")
    aes_key  = _ask("WECOM_ENCODING_AES_KEY")

    print()
    print("  LUC_API_BASE is the public URL Luclas is reachable at.")
    print("  WeCom will POST incoming messages to: {LUC_API_BASE}/wecom/callback")
    api_base = _ask("LUC_API_BASE (public URL)", "https://your-domain.com")

    print()
    print("  LUC_API_KEY protects the HTTP API. Leave empty to disable auth.")
    api_key  = _ask("LUC_API_KEY (optional)", "")

    result: dict[str, str] = {
        "WECOM_CORP_ID":          corp_id,
        "WECOM_AGENT_ID":         agent_id,
        "WECOM_SECRET":           secret,
        "WECOM_TOKEN":            token,
        "WECOM_ENCODING_AES_KEY": aes_key,
        "LUC_API_BASE":           api_base,
    }
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

    new_keys = {k: v for k, v in new_vars.items() if k not in updated}
    if new_keys:
        if lines and lines[-1].strip():
            lines.append("\n")
        for k, v in new_keys.items():
            lines.append(f"{k}={v}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


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
    print()
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │           Luclas Setup Wizard                    │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print("  We'll configure your LLM, messaging platform, and")
    print("  usage preferences. This writes to .env in the repo root.")
    print("  Press Ctrl-C at any time to cancel.")

    env_vars: dict[str, str] = {}

    env_vars.update(_step_llm())
    env_vars.update(_step_messaging())
    pref_vars, direction = _step_preferences()
    env_vars.update(pref_vars)

    _section("Saving configuration")
    _write_env(base_dir, env_vars)
    _write_direction(base_dir, direction)

    print()
    print("  ✓  .env updated")
    print("  ✓  data/user_direction.md saved")
    print()
    print("  All done. Run `luclas` to start.")
    print()
