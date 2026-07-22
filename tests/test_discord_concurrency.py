"""
The Discord adapter's reconnect loop used to leave _bot_loop/_bot_channel
looking "live" for the entire retry backoff window (5s-300s) after a
connection error — send_text()/_make_channel_send() would silently schedule
a send onto a closed, dead event loop and lose the message with no error.
These tests exercise both halves of the fix: the reconnect loop clearing
both globals immediately (not after the backoff sleep), and the send paths
falling back to REST when scheduling onto a closed loop raises.
"""
import asyncio
import sys
import threading
import time
import types

import pytest


@pytest.fixture
def discord_adapter(monkeypatch):
    import adapters.discord_adapter as da
    monkeypatch.setattr(da, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(da, "CHANNEL_ID", 12345)
    monkeypatch.setattr(da, "_bot_loop", None)
    monkeypatch.setattr(da, "_bot_channel", None)

    rest_calls = []

    def fake_post_with_retry(url, **kw):
        rest_calls.append((url, kw))
        class R:
            def json(self):
                return {}
        return R()

    monkeypatch.setattr(da.dispatch, "post_with_retry", fake_post_with_retry)
    return da, rest_calls


class _FakeChannel:
    id = 999

    async def send(self, content):
        pass


def test_send_text_falls_back_to_rest_when_no_loop(discord_adapter):
    da, rest_calls = discord_adapter
    da.send_text("hello, no loop")
    assert len(rest_calls) == 1


def test_channel_send_falls_back_to_rest_when_no_loop(discord_adapter):
    da, rest_calls = discord_adapter
    send = da._make_channel_send(_FakeChannel())
    send("hello via _send, no loop")
    assert len(rest_calls) == 1


def test_send_text_falls_back_to_rest_on_closed_loop(discord_adapter):
    da, rest_calls = discord_adapter
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    da._bot_loop = closed_loop
    da._bot_channel = _FakeChannel()
    da.send_text("hello, closed loop")
    assert len(rest_calls) == 1, "a RuntimeError from a closed loop must fall back to REST, not be lost"


def test_channel_send_falls_back_to_rest_on_closed_loop(discord_adapter):
    da, rest_calls = discord_adapter
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    da._bot_loop = closed_loop
    send = da._make_channel_send(_FakeChannel())
    send("hello via _send, closed loop")
    assert len(rest_calls) == 1


def test_send_text_uses_live_loop_without_rest_fallback(discord_adapter):
    da, rest_calls = discord_adapter
    live_loop = asyncio.new_event_loop()

    def _pump():
        asyncio.set_event_loop(live_loop)
        live_loop.run_forever()

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    time.sleep(0.05)
    try:
        da._bot_loop = live_loop
        da._bot_channel = _FakeChannel()
        da.send_text("hello, live loop")
        time.sleep(0.05)
        assert len(rest_calls) == 0, "must not fall back to REST when the bot connection is genuinely alive"
    finally:
        live_loop.call_soon_threadsafe(live_loop.stop)
        time.sleep(0.05)
        live_loop.close()


def test_reconnect_loop_clears_globals_before_backoff_sleep(monkeypatch):
    """The exact race this whole module guards against: globals must be None
    for the ENTIRE backoff window, not just after it."""
    fake_discord = types.ModuleType("discord")

    class FakeIntents:
        @staticmethod
        def default():
            return FakeIntents()

    class LoginFailure(Exception):
        pass

    attempt = {"n": 0}

    class FakeClient:
        def __init__(self, intents=None):
            self.user = object()

        def event(self, fn):
            return fn

        def get_channel(self, cid):
            return None

        async def start(self, token):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise ConnectionError("simulated transient failure")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

    fake_discord.Intents = FakeIntents
    fake_discord.Client = FakeClient
    fake_discord.LoginFailure = LoginFailure
    monkeypatch.setitem(sys.modules, "discord", fake_discord)

    import adapters.discord_adapter as da
    monkeypatch.setattr(da, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(da, "CHANNEL_ID", 12345)
    monkeypatch.setattr(da, "_RECONNECT_BASE_DELAY", 0.2)
    monkeypatch.setattr(da, "_bot_loop", None)
    monkeypatch.setattr(da, "_bot_channel", None)

    da.start_bot()

    time.sleep(0.05)  # after the first failed attempt, mid-backoff
    assert da._bot_loop is None, "_bot_loop must be None during the retry backoff sleep"
    assert da._bot_channel is None, "_bot_channel must be None during the retry backoff sleep"

    time.sleep(0.5)  # let the second (hanging, "successful") attempt start
    assert da._bot_loop is not None, "a fresh loop should be set once the reconnect attempt begins"
