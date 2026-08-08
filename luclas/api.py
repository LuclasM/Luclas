"""
api.py — Luclas HTTP API

Start:
    python api.py
    uvicorn api:app --host 0.0.0.0 --port 8080

Environment:
    LUC_API_KEY   optional — if set, all endpoints require X-API-Key header
    LUC_API_PORT  listen port (default 8080)
"""
from __future__ import annotations

import datetime
import os
import queue
import sys
import threading
import traceback
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from config import CORE_HIST, DATA_DIR, LLM_BASE_URL, LLM_MODEL, MODELS_CONFIG_PATH, RAW_DIR, SESSION_DIR
from llm_client import LLMClient
from llm_router import ModelRouter, load_models
from memory.database import init_db
from memory.store import MemoryStore
from memory.conversation_store import ConversationStore
from memory.episode_store import EpisodeStore
from tools.registry import build_tools
from loops.task_runner import TaskRunner
import conversation_runner
import i18n as T

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Luclas API", version="1.0", docs_url="/docs")

from adapters.wecom import router as wecom_router
from adapters.whatsapp import router as whatsapp_router
from adapters.web import router as web_sse_router
app.include_router(wecom_router)
app.include_router(whatsapp_router)
app.include_router(web_sse_router)

_API_KEY = os.environ.get("LUC_API_KEY", "")

# ---------------------------------------------------------------------------
# Singleton resources (one set shared across all requests)
# ---------------------------------------------------------------------------

_llm:         LLMClient        | None = None
_store:       MemoryStore      | None = None
_conversations = ConversationStore()
_episodes      = EpisodeStore()

# In-memory result store  {task_id: {...}}
_results: dict[str, dict] = {}
_lock = threading.Lock()

# Per-session supplement queues and running task tracking
# session_id → queue of pending supplement messages
_session_queues: dict[str, queue.Queue] = {}
# session_id → task_id of the currently-running task
_session_tasks: dict[str, str] = {}

# Two messages to the *same* conversation can arrive close enough together
# that /chat spawns two _run_conversation_turn threads before either has
# appended anything (neither looks like an "active task" yet, so /chat's own
# foreground-supplement check doesn't catch this) — without serializing them,
# both turns would build their LLM prompt from the same stale snapshot and
# reply out of order. This does NOT need to (and must not) span the whole
# call chain including a background dispatch_task's own completion — that
# already appends safely via memory/conversation_store.py's own per-
# conversation lock — this is purely about not running two *whole turns*
# for one conversation concurrently.
_turn_locks_guard = threading.Lock()
_turn_locks: dict[str, threading.Lock] = {}


def _turn_lock_for(conversation_id: str) -> threading.Lock:
    with _turn_locks_guard:
        lock = _turn_locks.get(conversation_id)
        if lock is None:
            lock = threading.Lock()
            _turn_locks[conversation_id] = lock
        return lock


@app.on_event("startup")
def _startup() -> None:
    global _llm, _store
    for d in [RAW_DIR, SESSION_DIR,
              os.path.join(SESSION_DIR, "logs"),
              os.path.join(SESSION_DIR, "messages"),
              CORE_HIST]:
        os.makedirs(d, exist_ok=True)
    init_db()
    # Reclaim task_state memories left stuck by a previous crash — api.py is
    # a long-lived service, so unlike the CLI (which does this once per
    # invocation) it would otherwise never happen.
    from luclas import _cleanup_interrupted_state
    _cleanup_interrupted_state()
    _router = None
    _loaded = load_models(MODELS_CONFIG_PATH)
    if _loaded:
        _router = ModelRouter(_loaded)
        print(f"[router] loaded {len(_loaded)} model(s) from models.json")
    _llm         = LLMClient(router=_router)
    _store       = MemoryStore()
    from adapters.discord_adapter import start_bot as _start_discord
    _start_discord()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _auth(x_api_key: str = Header(default="")) -> None:
    if _API_KEY and x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# web_api.py imports `_auth` from this module at its own load time, so this
# include must come after _auth is defined above — including it earlier
# (e.g. next to the wecom/whatsapp routers) would try to import _auth before
# it exists yet, since api.py is still mid-execution at that point.
from web_api import system_router, settings_router
app.include_router(system_router)
app.include_router(settings_router)

# Web UI static frontend, mounted at /ui (not "/") so a typo'd API path
# still 404s instead of being swallowed by StaticFiles' html=True fallback.
from fastapi.staticfiles import StaticFiles


class _NoCacheStaticFiles(StaticFiles):
    """Plain StaticFiles sends no Cache-Control header at all, which leaves
    it up to each browser's own caching heuristics whether a JS/CSS fix
    actually reaches an already-open tab or a returning visit — this has
    already caused real confusion twice (a CSS fix and a JS fix both
    silently not taking effect until a manual hard-refresh). ETag-based
    conditional requests (already sent by default) make "no-cache" cheap:
    an unchanged file still comes back as a 304, only a real change forces
    a full re-fetch."""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/ui", _NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="ui")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message:    str
    session_id: str = ""   # caller can pass its own session id (e.g. Telegram chat_id)


class ChatResponse(BaseModel):
    task_id: str
    status:  str           # always "running" at this point


class ResultResponse(BaseModel):
    task_id:     str
    status:      str       # "running" | "done" | "failed"
    result:      str = ""
    started_at:  str = ""
    finished_at: str = ""


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _make_push_callback(session_id: str):
    """Return a send-function for the adapter that owns this session, or None.

    Scheduled tasks (cron_runner.py) submit under a "cron_"-prefixed session_id
    (e.g. "cron_wecom_U123") deliberately distinct from a user's own live chat
    session_id ("wecom_U123") so a scheduled goal never gets merged as a
    "supplement" into whatever unrelated conversation happens to be running —
    strip that prefix before matching so channel push (progress/ask_user/
    errors) still resolves to the right adapter for cron-originated tasks too.
    """
    if session_id.startswith("cron_"):
        session_id = session_id[len("cron_"):]
    if session_id.startswith("wecom_"):
        user_id = session_id[len("wecom_"):]
        from adapters.wecom import _send_text
        def _cb(msg: str):
            try:
                _send_text(user_id, msg)
            except Exception as e:
                # Previously a bare `except Exception: pass` — a failure here
                # (e.g. the WeCom access token can no longer be refreshed)
                # left zero trace anywhere, not even in the log files; the
                # only way to notice was a user reporting they'd stopped
                # getting replies. At minimum, log it.
                print(f"[api] push to wecom user {user_id} failed: {e}", file=sys.stderr)
        return _cb

    if session_id.startswith("whatsapp_"):
        phone = session_id[len("whatsapp_"):]
        from adapters.whatsapp import send_text as _wa_send
        def _cb(msg: str):
            try:
                _wa_send(phone, msg)
            except Exception as e:
                print(f"[api] push to whatsapp {phone} failed: {e}", file=sys.stderr)
        return _cb

    if session_id.startswith("discord_"):
        from adapters.discord_adapter import send_text as _dc_send
        def _cb(msg: str):
            try:
                for i in range(0, max(len(msg), 1), 1900):
                    _dc_send(msg[i:i + 1900])
            except Exception as e:
                print(f"[api] push to discord session {session_id} failed: {e}", file=sys.stderr)
        return _cb

    if session_id.startswith("web_"):
        from adapters.web import push as _web_push
        def _cb(msg: str):
            try:
                _web_push(session_id, msg)
            except Exception as e:
                print(f"[api] push to web session {session_id} failed: {e}", file=sys.stderr)
        return _cb

    return None


def _run_task(task_id: str, goal: str, session_id: str,
              supplement_queue: "queue.Queue | None" = None, show_progress: bool = True) -> None:
    from tools.user_input import _NeedUserInput, set_channel_context, clear_channel_context
    schemas, fns = build_tools(_store)

    push = _make_push_callback(session_id)
    # show_progress=False is how background dispatch_task (see
    # _dispatch_task_for_conversation below) stays quiet mid-task — without
    # this, every tool-call round trip pushes a "💭.../▶ tool_name" progress
    # line to the channel regardless of foreground/background, which is
    # exactly the live-process view "watch it happen" (foreground) is
    # supposed to be, defeating the entire point of background dispatch
    # ("stays quiet, conversation can continue, only the final answer
    # lands"). on_result/failure pushes below are NOT gated by this — a
    # background task still needs to actually tell the user when it's done
    # or failed, just not narrate every step getting there.
    progress_callback = push if show_progress else None

    # cron_runner.py submits under a "cron_"-prefixed session_id and delivers
    # the final result itself by separately polling /result and notifying
    # (see cron_runner.py:_poll_and_notify) — it doesn't rely on push for
    # that, so the *final*-result deliveries below must be suppressed there,
    # or the user gets the answer twice: once via push, once again via
    # cron's own poll loop. push itself still fires for progress_callback.
    is_cron = session_id.startswith("cron_")

    # A cron-triggered task has no live two-way channel: if the user replies
    # on this channel, that message always arrives under their normal chat
    # session_id (e.g. "wecom_U123"), never this cron-specific one
    # ("cron_wecom_U123") — nothing ever routes a reply into *this* session's
    # queue. So ask_user() must not wait on it: with a real queue, ask_user()
    # would push the question and then block for up to
    # ASK_USER_TIMEOUT_SECONDS on an answer that can structurally never
    # arrive. Passing None here makes ask_user() take its immediate
    # _NeedUserInput path instead — the question still reaches the user (as
    # the task's final result, via cron's own poll+notify), just without the
    # pointless wait first.
    effective_wait_queue = None if is_cron else supplement_queue

    # Per-task LLM client so concurrent sessions don't share _model_queue / _current_idx.
    # The ModelRouter itself is stateless and safe to share.
    task_llm = LLMClient(router=_llm._router if _llm else None)

    runner = TaskRunner(
        llm=task_llm, schemas=schemas, fns=fns,
        mem_store=_store, session_id=session_id,
        progress_callback=progress_callback,
        supplement_queue=effective_wait_queue,
    )
    # Lets ask_user() (a mid-task tool call) push questions to this channel
    # and block for the reply on the same queue used for mid-task
    # supplements — this thread is dedicated to this one task.
    set_channel_context(push=push, wait_queue=effective_wait_queue, session_id=session_id)
    on_result = (lambda r: push(r or T.channel_done())) if (push and not is_cron) else None
    try:
        result = runner.run(goal, on_result=on_result)
        _set_result(task_id, "done", result)
    except _NeedUserInput as e:
        msg = f"❓ {e.question}"
        _set_result(task_id, "done", msg)
        if push and not is_cron:
            push(msg)
    except Exception as e:
        msg = str(e)
        print(f"[task {task_id}] failed: {msg}", file=sys.stderr)
        traceback.print_exc()
        _set_result(task_id, "failed", msg)
        if push and not is_cron:
            push(T.channel_task_failed(msg[:500]))
    finally:
        clear_channel_context()
        with _lock:
            if _session_tasks.get(session_id) == task_id:
                _session_tasks.pop(session_id, None)
                _session_queues.pop(session_id, None)


def _dispatch_task_for_conversation(session_id: str, memory_id: str, goal: str, foreground: bool) -> dict:
    """dispatch_task's implementation for the conversation layer — reuses
    _run_task()/TaskRunner exactly as the old per-message /chat path did, so
    push-based progress, ask_user()/supplement routing, and cron-style
    channel resolution all keep working unchanged. Only the bookkeeping
    around it differs: conversations.active_task_* tracks whether this is a
    foreground ("work mode", replaces the conversation until done) or
    background (conversation keeps going) dispatch, and a kind='task'
    episode is recorded once the task finishes either way.

    session_id (the physical channel connection) drives everything live —
    task tracking, ask_user supplement routing, and push delivery — exactly
    as before identity switching existed. memory_id (possibly a different,
    identity-resolved conversation — see memory/identity_store.py) is used
    only for where the *finished* task's episode/transcript gets recorded,
    so it lands in the right person's history even if they're chatting
    under a switched identity on this channel.

    _run_task() already pushes the final result itself via on_result
    (unless cron-prefixed, which session_id never is) — so this must NOT
    push it again. That's what "delivered" in the return value signals to
    conversation_runner.handle_turn(): for foreground it's True (already
    pushed, don't send reply_text again); for background it's False (the
    background thread below handles delivery once the task actually
    finishes, and the immediate return here is just an ack for the
    conversation model to react to in its own words).
    """
    task_id = uuid.uuid4().hex[:12]
    q = queue.Queue()
    with _lock:
        _results[task_id] = {"status": "running", "result": "", "started_at": _now(), "finished_at": ""}
        _session_queues[session_id] = q
        _session_tasks[session_id]  = task_id
    _conversations.set_active_task(session_id, task_id, foreground)

    def _finish():
        with _lock:
            result = _results.get(task_id, {}).get("result", "")
        episode_id = _episodes.create_task_episode(memory_id, task_id, result, importance=7)
        _conversations.clear_active_task(session_id)
        return result, episode_id

    if foreground:
        _run_task(task_id, goal, session_id, q, show_progress=True)  # blocks — caller is already off the HTTP thread
        result, episode_id = _finish()
        return {"delivered": True, "result": result, "episode_id": episode_id}

    def _bg():
        _run_task(task_id, goal, session_id, q, show_progress=False)
        result, episode_id = _finish()
        # _run_task already pushed `result` via its own on_result — only
        # record it into the conversation transcript here, don't push again.
        _conversations.append_message(memory_id, "assistant", result,
                                      episode_id=episode_id)

    threading.Thread(target=_bg, daemon=True).start()
    return {"delivered": False, "started": True, "task_id": task_id}


def _run_conversation_turn(session_id: str, message: str) -> None:
    from memory.identity_store import try_handle_command, resolve, active_identity_name
    push = _make_push_callback(session_id)

    # Fixed /identity command — deterministic, doesn't touch the LLM at all.
    command_reply = try_handle_command(session_id, message)
    if command_reply is not None:
        if push:
            push(command_reply)
        return

    memory_id = resolve(session_id)
    needs_identity_check = active_identity_name(session_id) is None
    try:
        with _turn_lock_for(memory_id):
            reply, already_delivered = conversation_runner.handle_turn(
                session_id, memory_id, message, llm=_llm, mem_store=_store,
                episode_store=_episodes, conv_store=_conversations,
                dispatch_fn=_dispatch_task_for_conversation,
                needs_identity_check=needs_identity_check,
            )
        if reply and not already_delivered and push:
            push(reply)
    except Exception as e:
        print(f"[api] conversation turn failed for {session_id}: {e}", file=sys.stderr)
        traceback.print_exc()
        if push:
            push(T.channel_task_failed(str(e)[:500]))


def _set_result(task_id: str, status: str, result: str) -> None:
    with _lock:
        if task_id in _results:
            _results[task_id]["status"]      = status
            _results[task_id]["result"]      = result
            _results[task_id]["finished_at"] = _now()
        _purge_old_results()


def _purge_old_results() -> None:
    """Remove finished results older than 2 hours. Must be called under _lock."""
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=2)
    cutoff_str = cutoff.isoformat(timespec="seconds")
    stale = [
        tid for tid, v in _results.items()
        if v["status"] != "running" and v.get("finished_at", "") < cutoff_str
    ]
    for tid in stale:
        _results.pop(tid, None)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _channel_health() -> dict:
    """Best-effort per-channel health, for /status and cron_runner.py's
    active health-check alerting (see _check_channel_health there). Not an
    equally deep check for every platform:
      - WeCom: a real liveness signal — _get_access_token() reuses its
        existing cache, so this is a no-op read in the common case and only
        actually hits the network once the cached token is genuinely close
        to expiry. This is the case the health-check alerting exists for
        (a revoked/expired corp secret going unnoticed for days).
      - Discord: the bot's own connection state is already tracked
        in-process (_bot_loop/_bot_channel) — free to read, no extra call.
      - WhatsApp: the Cloud API has no equivalently cheap liveness probe
        without spending a real API call, so this only reports whether the
        required credentials are configured, not whether they still work.
    """
    result: dict = {}

    from adapters import wecom
    if wecom.CORP_ID and wecom.SECRET and wecom.AGENT_ID:
        try:
            wecom._get_access_token()
            result["wecom"] = {"configured": True, "healthy": True}
        except Exception as e:
            result["wecom"] = {"configured": True, "healthy": False, "detail": str(e)[:200]}
    else:
        result["wecom"] = {"configured": False}

    from adapters import whatsapp
    if whatsapp.PHONE_NUMBER_ID and whatsapp.ACCESS_TOKEN:
        result["whatsapp"] = {"configured": True}
    else:
        result["whatsapp"] = {"configured": False}

    from adapters import discord_adapter
    if discord_adapter.BOT_TOKEN and discord_adapter.CHANNEL_ID:
        healthy = discord_adapter._bot_loop is not None and discord_adapter._bot_channel is not None
        result["discord"] = {"configured": True, "healthy": healthy}
    else:
        result["discord"] = {"configured": False}

    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness check — no auth required."""
    return {"ok": True}


class CommandRequest(BaseModel):
    line: str   # e.g. "/status" or "/memory search foo"
    session_id: str = ""


@app.post("/command", dependencies=[Depends(_auth)])
def run_command(req: CommandRequest):
    """Run a slash command synchronously and return its text output."""
    import io, contextlib, sys
    from luclas import _handle_slash
    from memory.identity_store import resolve
    schemas, fns = build_tools(_store)
    buf = io.StringIO()
    try:
        memory_id = resolve(req.session_id) if req.session_id else ""
        turn_guard = (_turn_lock_for(memory_id)
                      if memory_id and req.line.strip().lower() == "/new topic"
                      else contextlib.nullcontext())
        with turn_guard, contextlib.redirect_stdout(buf):
            _handle_slash(
                req.line, llm=_llm, store=_store, schemas=schemas, fns=fns,
                conversation_id=memory_id, conv_store=_conversations,
                episode_store=_episodes)
    except SystemExit:
        pass
    except Exception as e:
        traceback.print_exc()
        return {"output": f"❌ {e}"}
    # Strip ANSI colour codes
    import re
    text = re.sub(r'\x1b\[[0-9;]*m', '', buf.getvalue()).strip()
    return {"output": text or T.channel_done()}


@app.get("/status", dependencies=[Depends(_auth)])
def status():
    """System status."""
    llm_ok    = _llm.is_available() if _llm else False
    mem_count = _store.count()      if _store else 0
    pending   = sum(1 for v in _results.values() if v["status"] == "running")
    from llm_client import usage_summary
    return {
        "llm":          "online" if llm_ok else "offline",
        "model":        _llm.model    if _llm else LLM_MODEL,
        "endpoint":     _llm.base_url if _llm else LLM_BASE_URL,
        "memory_count": mem_count,
        "pending":      pending,
        "channels":     _channel_health(),
        "token_usage_24h": usage_summary(days=1),
    }


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(_auth)])
def chat(req: ChatRequest):
    """
    session_id is the physical channel connection — messaging adapters
    already derive a stable per-user id in exactly this format
    ("wecom_<UserID>" etc.). By default it also doubles as the persistent
    conversation's own id (see memory/conversation_store.py), but that's
    only the default: _run_conversation_turn resolves the *actual* memory
    id via memory/identity_store.py, which can differ from session_id once
    this channel has been switched to someone else's identity ("我是Gia").
    Task dispatch/push/the supplement-queue mechanism below all still key
    off the raw session_id regardless — only conversation history moves.

    Cron-submitted tasks (session_id prefixed "cron_") bypass the
    conversation layer entirely and keep going through _run_task() exactly
    as before — cron has no live human on the other end to converse with.

    For everything else: if the conversation is in foreground "work mode"
    (a dispatch_task(foreground=true) is in flight), the message is injected
    into that task's existing supplement mechanism, unchanged from before.
    Otherwise (no active task, or a background task quietly running) a
    conversation turn is run — conversation_runner.handle_turn() decides
    whether this needs dispatch_task or is just a reply, and delivers the
    result via push (adapters don't read this response body: see
    adapters/dispatch.py's _submit_task, "no polling here").
    """
    session_id = req.session_id or uuid.uuid4().hex[:8]

    # The browser posts directly to /chat rather than going through an
    # adapter's slash-command route.  Handle the manual topic boundary here
    # too so /new topic behaves identically on every channel.
    if req.message.strip().lower() == "/new topic":
        from memory.identity_store import resolve
        memory_id = resolve(session_id)
        with _turn_lock_for(memory_id):
            closed = _conversations.close_topic(memory_id, _episodes)
        push = _make_push_callback(session_id)
        if push:
            push("New topic started." if closed else "Already at a new empty topic.")
        return {"task_id": uuid.uuid4().hex[:12], "status": "running"}

    if session_id.startswith("cron_"):
        with _lock:
            task_id = uuid.uuid4().hex[:12]
            q = queue.Queue()
            _results[task_id] = {"status": "running", "result": "", "started_at": _now(), "finished_at": ""}
            _session_queues[session_id] = q
            _session_tasks[session_id]  = task_id
        threading.Thread(target=_run_task, args=(task_id, req.message, session_id, q), daemon=True).start()
        return {"task_id": task_id, "status": "running"}

    conv = _conversations.get_or_create(session_id)

    # A background task (active_task_foreground=False) normally lets a new
    # message start its own independent conversation turn — that's the
    # entire point of running it in the background. But the moment it's
    # actually sitting in ask_user() waiting on this same session's
    # supplement queue, the next message IS the answer to that question, not
    # a new topic, and must be routed there instead — see
    # tools/user_input.py::has_pending_question() for why this needs to be
    # tracked separately from the foreground flag.
    from tools.user_input import has_pending_question
    if conv["active_task_id"] and (conv["active_task_foreground"] or has_pending_question(session_id)):
        with _lock:
            still_running = _results.get(conv["active_task_id"], {}).get("status") == "running"
            q = _session_queues.get(session_id)
        if still_running and q is not None:
            q.put(req.message)
            return {"task_id": conv["active_task_id"], "status": "running"}
        # Stale bookkeeping (task actually finished but active_task_* wasn't
        # cleared yet, or the process restarted mid-task) — fall through to
        # a normal conversation turn instead of silently dropping the message.
        _conversations.clear_active_task(session_id)

    task_id = uuid.uuid4().hex[:12]  # kept only for the response shape — conversation turns aren't polled via /result
    threading.Thread(target=_run_conversation_turn, args=(session_id, req.message), daemon=True).start()
    return {"task_id": task_id, "status": "running"}


@app.get("/chat/history", dependencies=[Depends(_auth)])
def chat_history(session_id: str):
    """Persisted turns for a session's chat view — what F5 re-primes the UI
    from, since the SSE backlog (adapters/web.py) only ever held assistant
    pushes and was never meant as a history store on its own. Resolves
    through identity_store the same way a live turn would, so a channel
    that's switched to someone else's memory sees that identity's history
    here too, not its own empty default thread. Also returns the sender's
    current backlog seq so the caller's first SSE connect can pass it back
    as `since` and skip re-replaying the assistant replies this history
    already includes (see adapters/web.py's current_seq)."""
    from memory.identity_store import resolve
    from adapters.web import current_seq
    memory_id = resolve(session_id)
    messages = [m for m in _conversations.get_messages(memory_id) if not m.get("hidden")]
    return {"messages": messages, "last_seq": current_seq(session_id)}


@app.get("/result/{task_id}", response_model=ResultResponse, dependencies=[Depends(_auth)])
def get_result(task_id: str):
    """Poll for the result of a submitted task."""
    with _lock:
        r = _results.get(task_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, **r}


@app.delete("/result/{task_id}", dependencies=[Depends(_auth)])
def delete_result(task_id: str):
    """Clean up a finished task from memory."""
    with _lock:
        r = _results.pop(task_id, None)
    if r is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"deleted": task_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("LUC_API_PORT", 8080))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
