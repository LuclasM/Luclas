"""
web_api.py — /api/system/* and /api/settings/* routes for the web UI.

Included by api.py after _auth is defined there (see api.py) — this module
imports `_auth` from api.py at load time, so the include must happen after
that definition to avoid a circular-import ordering issue.
"""
from __future__ import annotations

import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import _auth
from config import MODELS_CONFIG_PATH
import env_store
from memory.schedule import ScheduledTaskStore
from tools.core_tools import list_core_snapshots, load_core, read_core_snapshot

system_router = APIRouter(prefix="/api/system", dependencies=[Depends(_auth)])
settings_router = APIRouter(prefix="/api/settings", dependencies=[Depends(_auth)])

_sched = ScheduledTaskStore()

# ---------------------------------------------------------------------------
# System management — scheduled tasks
# ---------------------------------------------------------------------------

_VALID_SCHEDULE_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleCreate(BaseModel):
    name: str
    goal: str
    schedule_type: str          # "daily" | "weekly"
    schedule_time: str          # "HH:MM"
    schedule_day: str = ""      # required if weekly
    notify_channel: str = "terminal"


class ScheduleToggle(BaseModel):
    enabled: bool


@system_router.get("/schedule")
def list_schedule():
    return _sched.list_all()


@system_router.post("/schedule")
def create_schedule(req: ScheduleCreate):
    if req.schedule_type not in ("daily", "weekly"):
        raise HTTPException(status_code=400, detail="schedule_type must be 'daily' or 'weekly'")
    if not re.match(r'^\d{2}:\d{2}$', req.schedule_time):
        raise HTTPException(status_code=400, detail="schedule_time must be HH:MM")
    if req.schedule_type == "weekly" and req.schedule_day not in _VALID_SCHEDULE_DAYS:
        raise HTTPException(status_code=400, detail=f"schedule_day must be one of {_VALID_SCHEDULE_DAYS}")
    if not req.name.strip() or not req.goal.strip():
        raise HTTPException(status_code=400, detail="name and goal are required")
    id_ = _sched.add(req.name, req.goal, req.schedule_type, req.schedule_time,
                      req.schedule_day, req.notify_channel)
    return {"id": id_}


@system_router.patch("/schedule/{task_id}")
def toggle_schedule(task_id: str, req: ScheduleToggle):
    if not _sched.toggle(task_id, req.enabled):
        raise HTTPException(status_code=404, detail="not found")
    return {"id": task_id, "enabled": req.enabled}


@system_router.delete("/schedule/{task_id}")
def delete_schedule(task_id: str):
    if not _sched.delete(task_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": task_id}


# ---------------------------------------------------------------------------
# System management — restart, core.md
# ---------------------------------------------------------------------------

@system_router.post("/restart")
def restart_api():
    from luclas import _restart_api_service
    restarted, msg = _restart_api_service()
    if not restarted:
        raise HTTPException(status_code=500, detail=msg)
    return {"ok": True, "detail": msg}


@system_router.get("/core")
def get_core():
    return {"content": load_core()}


@system_router.get("/core/history")
def get_core_history():
    return list_core_snapshots()


@system_router.get("/core/history/{name}")
def get_core_snapshot(name: str):
    try:
        return {"name": name, "content": read_core_snapshot(name)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="snapshot not found")


@system_router.get("/identities")
def list_identity_bindings():
    """Which physical channel connections are currently bound to which
    named identity — see memory/identity_store.py. Mainly for debugging/
    visibility (e.g. "did switching identity on this channel actually
    stick?"), not something normally edited from here."""
    from memory.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, identity, updated_at FROM identity_bindings ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Settings — .env
# ---------------------------------------------------------------------------

class EnvUpdate(BaseModel):
    values: dict[str, str]


@settings_router.get("/env")
def get_env():
    current = env_store.read_env()
    result = {}
    for key in sorted(env_store.ALLOWED_ENV_KEYS):
        if key in env_store.SECRET_ENV_KEYS:
            result[key] = {"secret": True, "configured": bool(current.get(key))}
        else:
            result[key] = {"secret": False, "value": current.get(key, "")}
    return result


@settings_router.post("/env")
def update_env(req: EnvUpdate):
    unknown = set(req.values) - env_store.ALLOWED_ENV_KEYS
    if unknown:
        raise HTTPException(status_code=400, detail=f"not allowed to set: {sorted(unknown)}")
    for v in req.values.values():
        try:
            env_store.validate_env_value(v)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    env_store.write_env(req.values)
    return {"written": sorted(req.values), "restart_required": True}


# ---------------------------------------------------------------------------
# Settings — models.json
# ---------------------------------------------------------------------------

class ModelUpsert(BaseModel):
    id: str
    name: str
    base_url: str
    api_key: str = "none"
    priority: int = 1
    complexity: str = "mid"
    task_types: list[str] = ["general"]
    classifier: bool = False
    context_length: int = 8192


class ModelPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    priority: int | None = None
    complexity: str | None = None
    task_types: list[str] | None = None
    classifier: bool | None = None
    context_length: int | None = None


def _load_models_raw() -> list[dict]:
    if not os.path.isfile(MODELS_CONFIG_PATH):
        return []
    try:
        with open(MODELS_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_models_raw(models: list[dict]) -> None:
    from model_manager import _save
    _save(models)


def _reload_llm_router() -> None:
    import api
    if api._llm:
        api._llm.reload_router()


def _mask(model: dict) -> dict:
    m = dict(model)
    m["has_api_key"] = bool(m.pop("api_key", ""))
    return m


@settings_router.get("/models")
def list_models():
    return [_mask(m) for m in _load_models_raw()]


@settings_router.post("/models")
def create_model(req: ModelUpsert):
    models = _load_models_raw()
    if any(m["id"] == req.id for m in models):
        raise HTTPException(status_code=409, detail=f"model id {req.id!r} already exists")
    models.append(req.model_dump())
    _save_models_raw(models)
    _reload_llm_router()
    return _mask(req.model_dump())


@settings_router.patch("/models/{model_id}")
def patch_model(model_id: str, req: ModelPatch):
    models = _load_models_raw()
    for m in models:
        if m["id"] == model_id:
            m.update({k: v for k, v in req.model_dump(exclude_unset=True).items()})
            _save_models_raw(models)
            _reload_llm_router()
            return _mask(m)
    raise HTTPException(status_code=404, detail="model not found")


@settings_router.delete("/models/{model_id}")
def delete_model(model_id: str):
    models = _load_models_raw()
    remaining = [m for m in models if m["id"] != model_id]
    if len(remaining) == len(models):
        raise HTTPException(status_code=404, detail="model not found")
    _save_models_raw(remaining)
    _reload_llm_router()
    return {"deleted": model_id}


@settings_router.get("/models/detect")
def detect_local_models():
    from local_llm_detect import scan_local_llm_servers
    return scan_local_llm_servers()


class ModelProbe(BaseModel):
    base_url: str
    api_key: str = ""


@settings_router.post("/models/probe")
def probe_model_endpoint(req: ModelProbe):
    from local_llm_detect import fetch_openai_models
    model_ids, effective_base_url = fetch_openai_models(req.base_url, req.api_key)
    return {"models": model_ids, "base_url": effective_base_url}
