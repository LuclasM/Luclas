"""
env_store.py — shared .env read/write utility.

write_env() was originally a setup.py-only helper; the web Settings page
needs the same backup+atomic-write behavior, so it lives here and setup.py
imports it instead of keeping its own copy.
"""
from __future__ import annotations

import datetime
import os

from config import BASE_DIR

# Keys the Settings page is allowed to write. Anything else is rejected —
# without this, the endpoint would be a generic "write any key into this
# file" primitive.
ALLOWED_ENV_KEYS = {
    "LUC_LANG",
    "LUC_LLM_BASE_URL", "LUC_LLM_MODEL", "LUC_LLM_API_KEY",
    "LUC_EMBED_MODEL",
    "LUC_API_KEY", "LUC_API_PORT",
    "LUC_ADMIN_NOTIFY", "LUC_DAILY_TOKEN_BUDGET",
    "WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET", "WECOM_TOKEN", "WECOM_ENCODING_AES_KEY",
    "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN",
    "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID",
}

# Values for these keys are never echoed back to the browser — only
# whether they're currently configured. Everything else in
# ALLOWED_ENV_KEYS is a plain identifier (corp id, channel id, phone
# number id), safe to show as-is.
SECRET_ENV_KEYS = {
    "LUC_LLM_API_KEY", "LUC_API_KEY",
    "WECOM_SECRET", "WECOM_TOKEN", "WECOM_ENCODING_AES_KEY",
    "WHATSAPP_ACCESS_TOKEN", "DISCORD_BOT_TOKEN",
}


def _parse_env_lines(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        result[k.strip()] = v
    return result


def read_env(base_dir: str = BASE_DIR) -> dict[str, str]:
    """Read the .env file's current on-disk contents (distinct from
    config.py's _load_dotenv, which only reads once at process start)."""
    env_path = os.path.join(base_dir, ".env")
    if not os.path.isfile(env_path):
        return {}
    with open(env_path, encoding="utf-8") as f:
        return _parse_env_lines(f.readlines())


def validate_env_value(value: str) -> None:
    """Raise ValueError if value would corrupt the flat KEY=value-no-quoting
    format this project's .env parser expects (no escaping support)."""
    if "\n" in value or "\r" in value:
        raise ValueError("value cannot contain a newline")
    if value.startswith("#"):
        raise ValueError("value cannot start with '#' (would become a comment)")


def write_env(new_vars: dict[str, str], base_dir: str = BASE_DIR) -> None:
    """Update or append KEY=value pairs in .env, preserving comments and
    ordering. Backs up the previous file before touching it, and writes via
    temp-file + atomic rename so a crash mid-write can't leave a truncated
    .env behind."""
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

    tmp_path = f"{env_path}.tmp-{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, env_path)
