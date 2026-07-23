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
from memory.task_memory import TaskMemory
from tools.registry import build_tools
from loops.task_runner import TaskRunner
import i18n as T

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Luclas API", version="1.0", docs_url="/docs")

from adapters.wecom import router as wecom_router
from adapters.whatsapp import router as whatsapp_router
app.include_router(wecom_router)
app.include_router(whatsapp_router)

_API_KEY = os.environ.get("LUC_API_KEY", "")

# ---------------------------------------------------------------------------
# Singleton resources (one set shared across all requests)
# ---------------------------------------------------------------------------

_llm:         LLMClient   | None = None
_store:       MemoryStore | None = None
_task_memory: TaskMemory  | None = None

# In-memory result store  {task_id: {...}}
_results: dict[str, dict] = {}
_lock = threading.Lock()

# Per-session supplement queues and running task tracking
# session_id → queue of pending supplement messages
_session_queues: dict[str, queue.Queue] = {}
# session_id → task_id of the currently-running task
_session_tasks: dict[str, str] = {}


@app.on_event("startup")
def _startup() -> None:
    global _llm, _store, _task_memory
    for d in [RAW_DIR, SESSION_DIR,
              os.path.join(SESSION_DIR, "logs"),
              os.path.join(SESSION_DIR, "messages"),
              CORE_HIST]:
        os.makedirs(d, exist_ok=True)
    init_db()
    # Reclaim task_records left stuck at tier='running' by a previous crash —
    # api.py is a long-lived service, so unlike the CLI (which does this once
    # per invocation) it would otherwise never happen, and get_running()/
    # build_context() would keep injecting a phantom "still running" task
    # into every future task's context indefinitely.
    from luclas import _cleanup_interrupted_state
    _cleanup_interrupted_state()
    _router = None
    _loaded = load_models(MODELS_CONFIG_PATH)
    if _loaded:
        _router = ModelRouter(_loaded)
        print(f"[router] loaded {len(_loaded)} model(s) from models.json")
    _llm         = LLMClient(router=_router)
    _store       = MemoryStore()
    _task_memory = TaskMemory()
    from adapters.discord_adapter import start_bot as _start_discord
    _start_discord()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _auth(x_api_key: str = Header(default="")) -> None:
    if _API_KEY and x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


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

    return None


def _run_task(task_id: str, goal: str, session_id: str,
              supplement_queue: "queue.Queue | None" = None) -> None:
    from tools.user_input import _NeedUserInput, set_channel_context, clear_channel_context
    schemas, fns = build_tools(_store)

    push = _make_push_callback(session_id)
    progress_callback = push  # same channel for progress and completion

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
    # arrive, for every question, including the post-task feedback prompt.
    # Passing None here makes ask_user() take its immediate _NeedUserInput
    # path instead — the question still reaches the user (as the task's
    # final result, via cron's own poll+notify), just without the pointless
    # wait first.
    effective_wait_queue = None if is_cron else supplement_queue

    # Per-task LLM client so concurrent sessions don't share _model_queue / _current_idx.
    # The ModelRouter itself is stateless and safe to share.
    task_llm = LLMClient(router=_llm._router if _llm else None)

    runner = TaskRunner(
        llm=task_llm, schemas=schemas, fns=fns,
        task_memory=_task_memory,
        mem_store=_store, session_id=session_id,
        progress_callback=progress_callback,
        supplement_queue=effective_wait_queue,
    )
    # Lets ask_user() (mid-task tool call or the post-task feedback loop) push
    # questions to this channel and block for the reply on the same queue used
    # for mid-task supplements — this thread is dedicated to this one task.
    set_channel_context(push=push, wait_queue=effective_wait_queue, session_id=session_id)
    # Show the full result before any feedback prompt runner.run() may trigger
    # internally (ask_user() pushes to the same channel) — otherwise the
    # feedback question arrives before the user has seen what was produced.
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
    line: str   # e.g. "/tasks" or "/memory search foo"


@app.post("/command", dependencies=[Depends(_auth)])
def run_command(req: CommandRequest):
    """Run a slash command synchronously and return its text output."""
    import io, contextlib, sys
    from luclas import (
        _handle_slash, _show_status, _show_tasks, _show_history,
    )
    schemas, fns = build_tools(_store)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _handle_slash(
                req.line,
                llm=_llm,
                store=_store,
                task_memory=_task_memory,
                schemas=schemas,
                fns=fns,
            )
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
    return {
        "llm":          "online" if llm_ok else "offline",
        "model":        _llm.model    if _llm else LLM_MODEL,
        "endpoint":     _llm.base_url if _llm else LLM_BASE_URL,
        "memory_count": mem_count,
        "pending":      pending,
        "channels":     _channel_health(),
    }


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(_auth)])
def chat(req: ChatRequest):
    """
    Submit a task. Returns immediately with a task_id.
    Poll GET /result/{task_id} for the outcome.
    If the same session already has a running task, the message is injected
    into that task as a supplement instead of starting a new one.
    """
    session_id = req.session_id or uuid.uuid4().hex[:8]

    with _lock:
        running_task_id = _session_tasks.get(session_id)
        if running_task_id and _results.get(running_task_id, {}).get("status") == "running":
            # Inject into the running task instead of starting a new one
            q = _session_queues.get(session_id)
            if q is not None:
                q.put(req.message)
            return {"task_id": running_task_id, "status": "running"}

        task_id = uuid.uuid4().hex[:12]
        q = queue.Queue()
        _results[task_id] = {
            "status":      "running",
            "result":      "",
            "started_at":  _now(),
            "finished_at": "",
        }
        _session_queues[session_id] = q
        _session_tasks[session_id]  = task_id

    t = threading.Thread(
        target=_run_task,
        args=(task_id, req.message, session_id, q),
        daemon=True,
    )
    t.start()

    return {"task_id": task_id, "status": "running"}


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
