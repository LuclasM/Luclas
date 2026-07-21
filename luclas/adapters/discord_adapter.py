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

def _rest_send(cid, content: str) -> None:
    try:
        dispatch.post_with_retry(
            f"{_DISCORD_API}/channels/{cid}/messages",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json={"content": content},
            timeout=10,
        )
    except Exception as e:
        print(f"[discord] failed to deliver message to channel {cid} after retries: {e}")


def send_text(content: str, channel_id: int | None = None) -> None:
    """Send a message to the configured channel (or a specific channel_id)."""
    cid = channel_id or CHANNEL_ID
    if not cid or not BOT_TOKEN:
        return
    # Snapshot once — _bot_loop/_bot_channel can be reassigned by the
    # reconnect loop (_run(), below) from another thread at any moment.
    loop, channel = _bot_loop, _bot_channel
    if loop and channel:
        try:
            # prefer the live bot connection (discord.py handles its own rate-limit retries)
            asyncio.run_coroutine_threadsafe(channel.send(content), loop)
            return
        except RuntimeError:
            # The loop was closed between the check above and this call (a
            # reconnect happened concurrently) — fall through to REST rather
            # than silently losing the message.
            pass
    _rest_send(cid, content)


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
        for i in range(0, max(len(text), 1), 1900):   # Discord's 2000-char limit
            chunk = text[i:i + 1900]
            # Snapshot once per chunk — _bot_loop can be reassigned by the
            # reconnect loop from another thread at any moment. Previously
            # this just returned outright (dropping the *whole* remaining
            # message) whenever _bot_loop was falsy, with no REST fallback
            # at all — unlike send_text(), which always had one.
            loop = _bot_loop
            if loop:
                try:
                    asyncio.run_coroutine_threadsafe(channel.send(chunk), loop)
                    continue
                except RuntimeError:
                    pass  # loop closed between the check and the call — fall through to REST
            _rest_send(channel.id, chunk)
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
            message_id=str(message.id),
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
            should_retry = False
            try:
                loop.run_until_complete(client.start(BOT_TOKEN))
                print("[discord] bot connection closed normally, not restarting")
            except discord.LoginFailure:
                print("[discord] ERROR: invalid DISCORD_BOT_TOKEN — bot will not retry")
            except Exception as e:
                print(f"[discord] connection error: {e} — retrying in {delay}s")
                should_retry = True
            finally:
                # Clear both globals and close the dead loop immediately —
                # *before* any retry backoff sleep below. Previously
                # _bot_loop was never reset here at all, and _bot_channel
                # was only cleared after the sleep already ran — so for the
                # whole backoff window (5s up to 300s), send_text()/_send()
                # would see a loop/channel that still looked live and
                # silently schedule sends onto a loop nobody was pumping
                # anymore, losing the message with no error.
                _bot_channel = None
                _bot_loop = None
                try:
                    loop.close()
                except Exception:
                    pass

            if not should_retry:
                break
            time.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    threading.Thread(target=_run, daemon=True, name="discord-bot").start()
