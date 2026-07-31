"""
memory/identity_store.py — cross-channel identity binding for conversation
memory continuity.

A session_id (a specific channel connection: wecom_U123, web_abc123,
cli_local, ...) can be bound to a named identity (e.g. "gia") so its
conversation history reads/writes go against that identity's shared memory
thread ("identity_gia") instead of the channel's own default one. This is
deliberately narrow: task dispatch, push delivery, and long-term
MemoryStore knowledge are NOT affected by this binding — only
ConversationStore/EpisodeStore's chat history is (see conversation_runner.py
and api.py's _run_conversation_turn/_dispatch_task_for_conversation for how
session_id vs. the resolved memory id are kept separate).
"""
from __future__ import annotations

import re

from memory.database import get_conn

IDENTITY_PREFIX = "identity_"
_CLEAR_WORDS = {"", "clear", "self", "me"}
_COMMAND_RE = re.compile(r'^/identity(?:\s+(.+))?\s*$', re.IGNORECASE)


def _normalize(name: str) -> str:
    return name.strip().lower()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def resolve(session_id: str) -> str:
    """Return the conversation_id ConversationStore/EpisodeStore should
    actually use for this physical session — either a bound identity's
    shared memory, or the session's own id by default (no binding yet)."""
    name = active_identity_name(session_id)
    return IDENTITY_PREFIX + name if name else session_id


def active_identity_name(session_id: str) -> str | None:
    """None means this session_id has never been bound to any identity —
    used to decide whether to proactively ask who's on the other end."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT identity FROM identity_bindings WHERE session_id=?", (session_id,)
        ).fetchone()
    return row["identity"] if row else None


def list_known_identities() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT identity FROM identity_bindings").fetchall()
    return [r["identity"] for r in rows]


def set_active_identity(session_id: str, name: str) -> str:
    name = _normalize(name)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO identity_bindings (session_id, identity, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            ON CONFLICT(session_id) DO UPDATE SET
                identity=excluded.identity, updated_at=excluded.updated_at
        """, (session_id, name))
    return name


def clear_active_identity(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM identity_bindings WHERE session_id=?", (session_id,))


def resolve_or_bind(session_id: str, claimed_name: str) -> dict:
    """Normalize claimed_name, fuzzy-match it against known identities, and
    either bind session_id to an existing identity, flag ambiguity for the
    caller to ask about, or create a brand-new identity. Shared by the
    fixed /identity command and the switch_identity LLM tool so there's one
    implementation of the matching rules.

    Returns one of:
      {"status": "matched", "identity": name}                       — exact match to an EXISTING identity
      {"status": "matched", "identity": name, "corrected_from": x}  — confident typo-correction to an EXISTING identity
      {"status": "ambiguous", "candidates": [...], "claimed": x}    — not confident enough to bind; caller should ask
      {"status": "created", "identity": name}                      — no reasonable match (or first identity ever); bound as new

    Short names (<=4 chars) never get silent auto-correction, even at edit
    distance 1 — a 1-character difference in a 3-4 letter name is not a
    reliable typo signal (e.g. "gia" vs "pia"/"nia"/"ria"/"kia" are all
    distance 1 from each other but are plainly different names), so for
    those lengths every fuzzy hit is treated as ambiguous and asked about
    rather than silently merging two different people's memory. Confident
    auto-correction only kicks in for longer names, where a 1-character
    edit is a much smaller fraction of the string and much more likely to
    genuinely be a typo (e.g. "gianna" vs "giana").
    """
    name = _normalize(claimed_name)
    known = list_known_identities()

    if name in known:
        set_active_identity(session_id, name)
        return {"status": "matched", "identity": name}

    if not known:
        set_active_identity(session_id, name)
        return {"status": "created", "identity": name}

    is_short = len(name) <= 4
    scored = sorted((_levenshtein(name, k), k) for k in known)
    best_dist, best_name = scored[0]
    tie = len(scored) > 1 and scored[1][0] == best_dist
    ambiguous_threshold = 1 if is_short else 2

    if not is_short and best_dist <= 1 and not tie:
        set_active_identity(session_id, best_name)
        return {"status": "matched", "identity": best_name, "corrected_from": name}

    if best_dist <= ambiguous_threshold:
        candidates = [k for d, k in scored if d <= ambiguous_threshold]
        return {"status": "ambiguous", "candidates": candidates, "claimed": name}

    set_active_identity(session_id, name)
    return {"status": "created", "identity": name}


def _render_reply(result: dict) -> str:
    status = result["status"]
    if status == "matched":
        if result.get("corrected_from"):
            return f"已切换到「{result['identity']}」的记忆(把「{result['corrected_from']}」当成了「{result['identity']}」,如果不对请再说一次)。"
        return f"已切换到「{result['identity']}」的记忆,之后的对话会接续这段记忆。"
    if status == "created":
        return f"没找到已有的「{result['identity']}」,已经作为新的身份记住了,之后再说「我是{result['identity']}」就能接上这段记忆。"
    if status == "ambiguous":
        options = "、".join(f"「{c}」" for c in result["candidates"])
        return f"你说的「{result['claimed']}」有点像 {options},是哪一个?"
    return "身份没变。"


def try_handle_command(session_id: str, message: str) -> str | None:
    """If `message` is a /identity command, apply it and return a reply
    string. Returns None if it's not a match (caller should fall through to
    the normal conversation turn)."""
    m = _COMMAND_RE.match(message.strip())
    if not m:
        return None
    name = (m.group(1) or "").strip()
    if _normalize(name) in _CLEAR_WORDS:
        clear_active_identity(session_id)
        return "已切换回本渠道自己的记忆。"
    result = resolve_or_bind(session_id, name)
    return _render_reply(result)


SWITCH_IDENTITY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "switch_identity",
        "description": (
            "Switch which person's conversation memory this channel connection is "
            "currently using, when the user states or implies their identity — e.g. "
            "'我是Gia' / '切换到Gia的记忆' / \"I'm Luke, resume my conversation\". "
            "Also call this when you don't yet know who you're talking to and need "
            "to record the name they just gave you. Pass an empty string (or "
            "'self'/'clear') to switch back to this channel's own default memory. "
            "The result may come back as status=ambiguous with candidate names if "
            "the given name closely resembles more than one known identity — in "
            "that case, ask the user to confirm which one instead of guessing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The identity name to switch to, or empty/'self'/'clear' to reset"},
            },
            "required": ["name"],
        },
    },
}


def switch_identity_tool(session_id: str):
    """Build the switch_identity tool function bound to a specific physical
    session_id, for conversation_runner.py's _build_tools()."""
    def switch_identity(name: str) -> dict:
        if _normalize(name) in _CLEAR_WORDS:
            clear_active_identity(session_id)
            return {"status": "cleared"}
        return resolve_or_bind(session_id, name)
    return switch_identity
