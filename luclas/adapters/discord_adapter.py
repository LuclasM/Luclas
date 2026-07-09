"""
adapters/discord_adapter.py — Discord bot adapter

Flow:
  Discord → bot receives message via WebSocket
          → process in background thread
          → poll Luclas API for result
          → reply in the same channel

The bot runs in a daemon thread started at API startup.
Requires: pip install discord.py
"""
from __future__ import annotations

import asyncio
import os
import threading

import requests

BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or "0")
LUC_API_BASE = os.environ.get("LUC_API_BASE", "http://localhost:8080")
LUC_API_KEY  = os.environ.get("LUC_API_KEY", "")

_DISCORD_API = "https://discord.com/api/v10"

# Shared state set when the bot is ready
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_channel = None


# ---------------------------------------------------------------------------
# Send helpers (usable from any thread)
# ---------------------------------------------------------------------------

def send_text(content: str, channel_id: int | None = None) -> None:
    """Send a message to the configured channel (or a specific channel_id)."""
    cid = channel_id or CHANNEL_ID
    if not cid or not BOT_TOKEN:
        return
    if _bot_loop and _bot_channel:
        # prefer the live bot connection
        asyncio.run_coroutine_threadsafe(_bot_channel.send(content), _bot_loop)
    else:
        # fallback: REST call (works even if bot loop isn't running)
        requests.post(
            f"{_DISCORD_API}/channels/{cid}/messages",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json={"content": content},
            timeout=10,
        )


def send_dm(user_id: str, content: str) -> None:
    """Send a direct message to a Discord user."""
    if not BOT_TOKEN:
        return
    try:
        r = requests.post(
            f"{_DISCORD_API}/users/@me/channels",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json={"recipient_id": user_id},
            timeout=10,
        ).json()
        dm_channel_id = r["id"]
        requests.post(
            f"{_DISCORD_API}/channels/{dm_channel_id}/messages",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json={"content": content},
            timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background task: submit → poll → reply
# ---------------------------------------------------------------------------

def _run_command_and_reply(user_id: str, line: str, reply_fn) -> None:
    headers = {"X-API-Key": LUC_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"{LUC_API_BASE}/command",
            json={"line": line},
            headers=headers,
            timeout=15,
        ).json()
        output = r.get("output", "✅ Done")
    except Exception as e:
        output = f"❌ Command failed: {e}"
    if _bot_loop:
        asyncio.run_coroutine_threadsafe(reply_fn(output), _bot_loop)


def _process_and_reply(user_id: str, message: str, reply_fn) -> None:
    # Submit task and return — the task thread pushes the final result via send_text().
    headers = {"X-API-Key": LUC_API_KEY, "Content-Type": "application/json"}
    try:
        requests.post(
            f"{LUC_API_BASE}/chat",
            json={"message": message, "session_id": f"discord_{user_id}"},
            headers=headers,
            timeout=10,
        )
    except Exception as e:
        if _bot_loop:
            asyncio.run_coroutine_threadsafe(reply_fn(f"❌ Submit failed: {e}"), _bot_loop)


# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------

def start_bot() -> None:
    """Start the Discord bot in a background daemon thread. Called at API startup."""
    if not BOT_TOKEN or not CHANNEL_ID:
        return

    try:
        import discord
    except ImportError:
        print("[discord] discord.py not installed — skipping bot. Run: pip install discord.py")
        return

    global _bot_loop, _bot_channel

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        global _bot_channel
        _bot_channel = client.get_channel(CHANNEL_ID)
        print(f"[discord] bot ready — channel: {_bot_channel}")

    @client.event
    async def on_message(message: discord.Message):
        if message.author == client.user:
            return
        if message.channel.id != CHANNEL_ID:
            return

        content = message.content.strip()
        user_id = str(message.author.id)
        username = message.author.display_name

        async def reply(text: str):
            # Discord has a 2000-char limit per message
            for i in range(0, len(text), 1900):
                await message.channel.send(text[i:i + 1900])

        if content.startswith("/"):
            threading.Thread(
                target=_run_command_and_reply,
                args=(user_id, content, reply),
                daemon=True,
            ).start()
        else:
            await message.channel.send("⏳ Processing…")
            contexted = (
                f"[Discord user {username} (id={user_id}), "
                f"set notify_channel=discord:{user_id} for scheduled tasks] {content}"
            )
            threading.Thread(
                target=_process_and_reply,
                args=(user_id, contexted, reply),
                daemon=True,
            ).start()

    def _run() -> None:
        global _bot_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _bot_loop = loop
        loop.run_until_complete(client.start(BOT_TOKEN))

    threading.Thread(target=_run, daemon=True, name="discord-bot").start()
