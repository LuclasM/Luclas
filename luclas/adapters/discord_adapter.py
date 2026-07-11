"""
adapters/discord_adapter.py — Discord bot adapter

Flow:
  Discord → bot receives message via WebSocket
          → process in background thread (adapters/dispatch.py)
          → reply in the same channel

The bot runs in a daemon thread started at API startup, with a reconnect
loop (backoff, capped) around the whole client lifecycle — discord.py's own
`reconnect=True` only covers transient gateway drops *within* a session; it
doesn't cover the initial connection failing outright or the client loop
exiting unexpectedly, and an uncaught exception in a bare daemon thread would
otherwise die silently with nothing but a stderr traceback.

Requires: pip install discord.py
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Callable

from adapters import dispatch

BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or "0")

_DISCORD_API = "https://discord.com/api/v10"

# Shared state set when the bot is ready
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_channel = None

_RECONNECT_BASE_DELAY = 5      # seconds
_RECONNECT_MAX_DELAY  = 300    # seconds


# ---------------------------------------------------------------------------
# Send helpers (usable from any thread)
# ---------------------------------------------------------------------------

def send_text(content: str, channel_id: int | None = None) -> None:
    """Send a message to the configured channel (or a specific channel_id)."""
    cid = channel_id or CHANNEL_ID
    if not cid or not BOT_TOKEN:
        return
    if _bot_loop and _bot_channel:
        # prefer the live bot connection (discord.py handles its own rate-limit retries)
        asyncio.run_coroutine_threadsafe(_bot_channel.send(content), _bot_loop)
    else:
        # fallback: REST call (works even if bot loop isn't running)
        try:
            dispatch.post_with_retry(
                f"{_DISCORD_API}/channels/{cid}/messages",
                headers={"Authorization": f"Bot {BOT_TOKEN}"},
                json={"content": content},
                timeout=10,
            )
        except Exception as e:
            print(f"[discord] failed to deliver message to channel {cid} after retries: {e}")


def send_dm(user_id: str, content: str) -> None:
    """Send a direct message to a Discord user."""
    if not BOT_TOKEN:
        return
    try:
        r = dispatch.post_with_retry(
            f"{_DISCORD_API}/users/@me/channels",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json={"recipient_id": user_id},
            timeout=10,
        ).json()
        dm_channel_id = r["id"]
        dispatch.post_with_retry(
            f"{_DISCORD_API}/channels/{dm_channel_id}/messages",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json={"content": content},
            timeout=10,
        )
    except Exception as e:
        print(f"[discord] failed to send DM to {user_id} after retries: {e}")


def _make_channel_send(channel) -> Callable[[str], None]:
    """Sync send closure for dispatch.handle_incoming — safe to call from any
    thread (the event loop's own thread, or dispatch's background threads)."""
    def _send(text: str) -> None:
        if not _bot_loop:
            return
        for i in range(0, max(len(text), 1), 1900):   # Discord's 2000-char limit
            chunk = text[i:i + 1900]
            asyncio.run_coroutine_threadsafe(channel.send(chunk), _bot_loop)
    return _send


# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------

def _build_client(discord):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        global _bot_channel
        _bot_channel = client.get_channel(CHANNEL_ID)
        print(f"[discord] bot ready — channel: {_bot_channel}")

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        if message.channel.id != CHANNEL_ID:
            return

        content  = message.content.strip()
        user_id  = str(message.author.id)
        username = message.author.display_name

        dispatch.handle_incoming(
            channel_label="Discord",
            notify_channel=f"discord:{user_id}",
            session_id=f"discord_{user_id}",
            sender_id=f"{username} (id={user_id})",
            content=content,
            send=_make_channel_send(message.channel),
        )

    return client


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

    def _run() -> None:
        global _bot_loop, _bot_channel
        delay = _RECONNECT_BASE_DELAY
        while True:
            client = _build_client(discord)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _bot_loop = loop
            try:
                loop.run_until_complete(client.start(BOT_TOKEN))
                print("[discord] bot connection closed normally, not restarting")
                break
            except discord.LoginFailure:
                print("[discord] ERROR: invalid DISCORD_BOT_TOKEN — bot will not retry")
                break
            except Exception as e:
                print(f"[discord] connection error: {e} — retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)
                continue
            finally:
                _bot_channel = None
                try:
                    loop.close()
                except Exception:
                    pass

    threading.Thread(target=_run, daemon=True, name="discord-bot").start()
