"""
api.py — EVA4 HTTP API

Start:
    python api.py
    uvicorn api:app --host 0.0.0.0 --port 8080

Environment:
    EVA_API_KEY   optional — if set, all endpoints require X-API-Key header
    EVA_API_PORT  listen port (default 8080)
"""
from __future__ import annotations

import datetime
import os
import sys
import threading
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from config import CORE_HIST, DATA_DIR, LLM_BASE_URL, LLM_MODEL, RAW_DIR, SESSION_DIR
from llm_client import LLMClient
from memory.database import init_db
from memory.store import MemoryStore, TaskStore
from memory.task_memory import TaskMemory
from tools.registry import build_tools
from loops.task_runner import TaskRunner

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="EVA4 API", version="1.0", docs_url="/docs")

from adapters.wecom import router as wecom_router
app.include_router(wecom_router)

_API_KEY = os.environ.get("EVA_API_KEY", "")

# ---------------------------------------------------------------------------
# Singleton resources (one set shared across all requests)
# ---------------------------------------------------------------------------

_llm:         LLMClient   | None = None
_store:       MemoryStore | None = None
_task_store:  TaskStore   | None = None
_task_memory: TaskMemory  | None = None

# In-memory result store  {task_id: {...}}
_results: dict[str, dict] = {}
_lock = threading.Lock()


@app.on_event("startup")
def _startup() -> None:
    global _llm, _store, _task_store, _task_memory
    for d in [RAW_DIR, SESSION_DIR,
              os.path.join(SESSION_DIR, "logs"),
              os.path.join(SESSION_DIR, "messages"),
              CORE_HIST]:
        os.makedirs(d, exist_ok=True)
    init_db()
    _llm         = LLMClient()
    _store       = MemoryStore()
    _task_store  = TaskStore()
    _task_memory = TaskMemory()


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

def _make_wecom_callback(user_id: str):
    from adapters.wecom import _send_text
    def _cb(msg: str):
        try:
            _send_text(user_id, msg)
        except Exception:
            pass
    return _cb


def _run_task(task_id: str, goal: str, session_id: str) -> None:
    from tools.user_input import _NeedUserInput
    schemas, fns = build_tools(_store)

    progress_callback = None
    if session_id.startswith("wecom_"):
        user_id = session_id[len("wecom_"):]
        progress_callback = _make_wecom_callback(user_id)

    runner = TaskRunner(
        llm=_llm, schemas=schemas, fns=fns,
        task_store=_task_store, task_memory=_task_memory,
        mem_store=_store, session_id=session_id,
        progress_callback=progress_callback,
    )
    try:
        result = runner.run(goal)
        _set_result(task_id, "done", result)
    except _NeedUserInput as e:
        _set_result(task_id, "done", f"❓ {e.question}")
    except Exception as e:
        _set_result(task_id, "failed", str(e))


def _set_result(task_id: str, status: str, result: str) -> None:
    with _lock:
        if task_id in _results:
            _results[task_id]["status"]      = status
            _results[task_id]["result"]      = result
            _results[task_id]["finished_at"] = _now()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


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
    import io, contextlib, re
    from eva import _handle_slash
    schemas, fns = build_tools(_store)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _handle_slash(
                req.line,
                llm=_llm,
                store=_store,
                task_store=_task_store,
                task_memory=_task_memory,
                schemas=schemas,
                fns=fns,
            )
    except SystemExit:
        pass
    except Exception as e:
        return {"output": f"❌ {e}"}
    text = re.sub(r'\x1b\[[0-9;]*m', '', buf.getvalue()).strip()
    return {"output": text or "✅ 完成"}


@app.get("/status", dependencies=[Depends(_auth)])
def status():
    """System status."""
    llm_ok    = _llm.is_available() if _llm else False
    mem_count = _store.count()      if _store else 0
    pending   = sum(1 for v in _results.values() if v["status"] == "running")
    return {
        "llm":          "online" if llm_ok else "offline",
        "model":        LLM_MODEL,
        "endpoint":     LLM_BASE_URL,
        "memory_count": mem_count,
        "pending":      pending,
    }


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(_auth)])
def chat(req: ChatRequest):
    """
    Submit a task. Returns immediately with a task_id.
    Poll GET /result/{task_id} for the outcome.
    """
    task_id    = uuid.uuid4().hex[:12]
    session_id = req.session_id or uuid.uuid4().hex[:8]

    with _lock:
        _results[task_id] = {
            "status":      "running",
            "result":      "",
            "started_at":  _now(),
            "finished_at": "",
        }

    t = threading.Thread(
        target=_run_task,
        args=(task_id, req.message, session_id),
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
    port = int(os.environ.get("EVA_API_PORT", 8080))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
