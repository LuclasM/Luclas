"""
adapters/dispatch.py — shared messaging-channel dispatch logic

Each adapter (wecom/whatsapp/discord) only handles its own wire protocol —
webhook auth, decryption, connection lifecycle, and how to actually send a
message back out. Once an adapter has parsed an incoming message down to
(sender_id, text) and built a send() closure, it hands off to
handle_incoming() here for everything that's the same across every channel:
command vs. task routing, the notify_channel context prefix, and the
submit/reply flow (including language, via i18n — previously each adapter
hardcoded its own language regardless of LUC_LANG).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

import requests

import i18n as T

LUC_API_BASE = os.environ.get("LUC_API_BASE", "http://localhost:8080")
LUC_API_KEY  = os.environ.get("LUC_API_KEY", "")

_RETRY_ATTEMPTS    = 3
_RETRY_BASE_DELAY  = 1.0   # seconds; doubles each attempt (1s, 2s)


def post_with_retry(url: str, max_retries: int = _RETRY_ATTEMPTS,
                     base_delay: float = _RETRY_BASE_DELAY, **kwargs) -> requests.Response:
    """requests.post with exponential-backoff retry on transient failures
    (connection errors, timeouts, 429, 5xx). Fails fast on 4xx (other than
    429) since retrying a bad request/auth error never helps.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"transient status {resp.status_code}", response=resp)
            return resp
        except (requests.RequestException,) as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_exc


def _headers() -> dict:
    return {"X-API-Key": LUC_API_KEY, "Content-Type": "application/json"}


def _run_command_and_reply(send: Callable[[str], None], line: str) -> None:
    try:
        r = post_with_retry(
            f"{LUC_API_BASE}/command",
            json={"line": line},
            headers=_headers(),
            timeout=15,
        ).json()
        send(r.get("output", T.channel_done()))
    except Exception as e:
        send(T.channel_command_failed(e))


def _submit_task(send: Callable[[str], None], session_id: str, contexted_goal: str) -> None:
    # Submit and return — the background task thread pushes the final result
    # (and any ask_user question) directly via send(), so no polling here.
    try:
        post_with_retry(
            f"{LUC_API_BASE}/chat",
            json={"message": contexted_goal, "session_id": session_id},
            headers=_headers(),
            timeout=10,
        )
    except Exception as e:
        send(T.channel_submit_failed(e))


def handle_incoming(channel_label: str, notify_channel: str, session_id: str,
                     sender_id: str, content: str, send: Callable[[str], None]) -> None:
    """Shared entry point for all messaging adapters.

    channel_label:  human-readable name injected into the task's goal context,
                     e.g. "WeCom" / "Discord"
    notify_channel: value to suggest for scheduled-task notifications,
                     e.g. "wecom:userid"
    session_id:     Luclas session id, e.g. "wecom_userid"
    sender_id:      the platform's own user identifier, for display only
    content:        the raw message text (already extracted from the platform's
                     wire format by the caller)
    send:           callback that delivers a text reply back to the sender
                     on this channel
    """
    content = content.strip()
    if not content:
        return

    if content.startswith("/"):
        threading.Thread(
            target=_run_command_and_reply,
            args=(send, content),
            daemon=True,
        ).start()
        return

    send(T.channel_processing())
    contexted = T.channel_context_prefix(channel_label, sender_id, notify_channel) + content
    threading.Thread(
        target=_submit_task,
        args=(send, session_id, contexted),
        daemon=True,
    ).start()
