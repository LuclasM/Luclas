"""
adapters/web.py — browser-facing SSE push adapter.

Mirrors wecom.py/whatsapp.py's send_text() shape, but the "send" target is
a connected browser tab rather than an outbound platform API call.
Delivery for web_-prefixed sessions is push-only, exactly like every other
channel — the browser never polls /result, it just holds an SSE connection
open (GET /sse/chat) and receives conversation replies, task progress, and
ask_user() questions through it.
"""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import time

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import StreamingResponse

router = APIRouter()

_API_KEY = os.environ.get("LUC_API_KEY", "")

_PING_INTERVAL_SECONDS = 20
_BACKLOG_LIMIT = 200  # per session_id; old entries are dropped once exceeded

# Every distinct session_id ever pushed to (e.g. a new browser tab that never
# set an identity) leaves a _backlog/_next_seq entry that unregister() alone
# never cleans up — a slow, unbounded-in-count (though bounded-in-size-each)
# memory leak for the life of the process. Swept lazily on every push() —
# same style as api.py's _purge_old_results — rather than a background
# thread/timer, since pushes already happen often enough to keep this fresh.
_STALE_SESSION_TTL_SECONDS = 24 * 3600

_lock = threading.Lock()
_streams: dict[str, list["queue.Queue"]] = {}
_backlog: dict[str, list[tuple[int, str]]] = {}
_next_seq: dict[str, int] = {}
_last_touch: dict[str, float] = {}


def _purge_stale_sessions_locked(now: float) -> None:
    """Must be called with _lock already held. Only ever drops sessions with
    zero live connections — an open tab is never evicted out from under
    itself no matter how old its backlog is."""
    cutoff = now - _STALE_SESSION_TTL_SECONDS
    stale = [
        sid for sid, touched in _last_touch.items()
        if touched < cutoff and not _streams.get(sid)
    ]
    for sid in stale:
        _last_touch.pop(sid, None)
        _backlog.pop(sid, None)
        _next_seq.pop(sid, None)


def push(session_id: str, content: str) -> None:
    """Deliver content to every browser tab currently connected to this
    session, and buffer it so a tab that reconnects shortly after (or wasn't
    open yet when this was called) can still catch up via backlog_since."""
    with _lock:
        now = time.time()
        _last_touch[session_id] = now
        seq = _next_seq.get(session_id, 0) + 1
        _next_seq[session_id] = seq
        buf = _backlog.setdefault(session_id, [])
        buf.append((seq, content))
        if len(buf) > _BACKLOG_LIMIT:
            del buf[: len(buf) - _BACKLOG_LIMIT]
        queues = list(_streams.get(session_id, []))
        _purge_stale_sessions_locked(now)
    for q in queues:
        q.put_nowait((seq, content))


def register(session_id: str) -> "queue.Queue":
    q: "queue.Queue" = queue.Queue()
    with _lock:
        _streams.setdefault(session_id, []).append(q)
        _last_touch[session_id] = time.time()
    return q


def unregister(session_id: str, q: "queue.Queue") -> None:
    with _lock:
        qs = _streams.get(session_id)
        if qs and q in qs:
            qs.remove(q)
        if qs is not None and not qs:
            _streams.pop(session_id, None)


def backlog_since(session_id: str, last_seq: int) -> list[tuple[int, str]]:
    with _lock:
        buf = _backlog.get(session_id, [])
        return [(s, m) for s, m in buf if s > last_seq]


def current_seq(session_id: str) -> int:
    """Highest seq already pushed for this session — a fresh page load that
    primes its message list from GET /chat/history (persisted conversation
    history, which already has every past user/assistant turn) passes this
    back as `since` on its first SSE connect so the backlog replay below
    doesn't re-append those same assistant replies a second time."""
    with _lock:
        return _next_seq.get(session_id, 0)


def _format_sse(seq: int, content: str) -> str:
    # A message may contain newlines — each line needs its own "data:"
    # prefix per the SSE spec, or everything after the first line break is
    # silently dropped by the client.
    data_lines = "\n".join(f"data: {line}" for line in content.split("\n"))
    return f"id: {seq}\n{data_lines}\n\n"


async def _event_stream(request: Request, session_id: str, last_event_id: str):
    # register() happens before the backlog is read, so a push() landing in
    # between is both appended to the backlog AND put_nowait'd straight into
    # our queue — without tracking the highest seq already replayed from the
    # backlog and skipping it again from the live queue, that message would
    # be delivered twice.
    q = register(session_id)
    try:
        try:
            last_seq = int(last_event_id) if last_event_id else 0
        except ValueError:
            last_seq = 0
        max_yielded = last_seq
        for seq, content in backlog_since(session_id, last_seq):
            max_yielded = max(max_yielded, seq)
            yield _format_sse(seq, content)

        while True:
            if await request.is_disconnected():
                break
            try:
                seq, content = await asyncio.to_thread(q.get, True, _PING_INTERVAL_SECONDS)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if seq <= max_yielded:
                continue
            max_yielded = seq
            yield _format_sse(seq, content)
    finally:
        unregister(session_id, q)


@router.get("/sse/chat")
async def sse_chat(request: Request, session_id: str = Query(...), key: str = Query(""), since: str = Query("")):
    # EventSource can't set a custom X-API-Key header, so the key travels as
    # a query param here specifically — acceptable given this endpoint is no
    # longer reachable from the public tunnel (path allowlist tightened to
    # /wecom/callback and /whatsapp/callback only).
    if _API_KEY and key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing key")
    # The browser's own automatic reconnect (transient network blip) sets
    # Last-Event-ID itself and takes priority; `since` is how a freshly
    # constructed EventSource (page load, or chat.js's manual retry after a
    # dead connection) supplies the same resume point — EventSource has no
    # way to set that header on an initial request, only query params.
    last_event_id = request.headers.get("last-event-id", "") or since
    return StreamingResponse(
        _event_stream(request, session_id, last_event_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
