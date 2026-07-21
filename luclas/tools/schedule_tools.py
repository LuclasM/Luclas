import json
import re
from memory.schedule import ScheduledTaskStore

_store = ScheduledTaskStore()

_VALID_SCHEDULE_TYPES    = ("daily", "weekly", "once")
_VALID_WEEKDAYS          = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_VALID_CHANNEL_PLATFORMS = ("wecom", "whatsapp", "discord")
_WEEKDAY_ALIASES = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}

SCHEDULE_ADD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "schedule_add",
        "description": (
            "Create a scheduled task. Use this when the user says something like "
            "'remind me every day at 9am to ...', 'run X every Monday at 8pm', etc. "
            "Parse the user's natural language into the correct fields before calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short label for this task (e.g. 'morning digest')",
                },
                "goal": {
                    "type": "string",
                    "description": "The instruction Luclas will receive when the task fires (be specific)",
                },
                "schedule_type": {
                    "type": "string",
                    "enum": ["daily", "weekly", "once"],
                    "description": "'daily' every day; 'weekly' once a week; 'once' runs at a specific date/time and then deletes itself",
                },
                "schedule_time": {
                    "type": "string",
                    "description": "HH:MM in 24-hour format, e.g. '09:00' or '21:30'",
                },
                "schedule_day": {
                    "type": "string",
                    "description": "For weekly: mon/tue/wed/thu/fri/sat/sun. For once: YYYY-MM-DD date string. Leave empty for daily.",
                    "default": "",
                },
                "notify_channel": {
                    "type": "string",
                    "description": "Where to send the result when the task fires. Options: 'terminal' (CLI), 'wecom:<user_id>', 'whatsapp:<phone>', 'discord:<user_id>'. Default: 'terminal'.",
                    "default": "terminal",
                },
            },
            "required": ["name", "goal", "schedule_type", "schedule_time"],
        },
    },
}

SCHEDULE_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "schedule_list",
        "description": "List all scheduled tasks. Use this to show the user what recurring tasks exist.",
        "parameters": {"type": "object", "properties": {}},
    },
}

SCHEDULE_DELETE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "schedule_delete",
        "description": "Delete a scheduled task by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Task ID (8-char hex)"},
            },
            "required": ["id"],
        },
    },
}

SCHEDULE_TOGGLE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "schedule_toggle",
        "description": "Enable or disable a scheduled task.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Task ID (8-char hex)"},
                "enabled": {"type": "boolean"},
            },
            "required": ["id", "enabled"],
        },
    },
}


def _normalize_schedule_time(schedule_time: str) -> str:
    """Accepts 'H:MM' or 'HH:MM' and normalizes to zero-padded 'HH:MM' —
    cron_runner.py matches schedule_time against now.strftime("%H:%M") via a
    plain string comparison, so an unpadded value like '9:00' (a plausible
    LLM parse of "9am") would never compare correctly and the schedule could
    silently never fire, with no error visible anywhere."""
    m = re.match(r'^\s*(\d{1,2}):(\d{2})\s*$', schedule_time or "")
    if not m:
        raise ValueError(f"schedule_time must be HH:MM in 24-hour format (e.g. '09:00'), got {schedule_time!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"schedule_time out of range: {schedule_time!r}")
    return f"{hour:02d}:{minute:02d}"


def _normalize_schedule_type(schedule_type: str) -> str:
    st = (schedule_type or "").strip().lower()
    if st not in _VALID_SCHEDULE_TYPES:
        raise ValueError(f"schedule_type must be one of {_VALID_SCHEDULE_TYPES}, got {schedule_type!r}")
    return st


def _normalize_schedule_day(schedule_type: str, schedule_day: str) -> str:
    day = (schedule_day or "").strip().lower()
    if schedule_type == "weekly":
        day = _WEEKDAY_ALIASES.get(day, day)
        if day not in _VALID_WEEKDAYS:
            raise ValueError(
                f"schedule_day for a weekly task must be one of {_VALID_WEEKDAYS} "
                f"(or a full day name), got {schedule_day!r}"
            )
        return day
    if schedule_type == "once":
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
            raise ValueError(f"schedule_day for a 'once' task must be YYYY-MM-DD, got {schedule_day!r}")
        return day
    return ""  # daily: schedule_day is unused, ignore whatever was passed


def _normalize_notify_channel(notify_channel: str) -> str:
    ch = (notify_channel or "terminal").strip()
    if ch == "" or ch.lower() == "terminal":
        return "terminal"
    if ":" not in ch:
        raise ValueError(
            f"notify_channel must be 'terminal' or '<platform>:<id>' "
            f"(platform one of {_VALID_CHANNEL_PLATFORMS}), got {notify_channel!r}"
        )
    platform, _, ident = ch.partition(":")
    platform = platform.strip().lower()
    if platform not in _VALID_CHANNEL_PLATFORMS:
        raise ValueError(f"notify_channel's platform must be one of {_VALID_CHANNEL_PLATFORMS}, got {notify_channel!r}")
    # cron_runner.py/api.py match this prefix case-sensitively (e.g. "wecom:")
    # and use the id verbatim — normalize the platform, keep the id as-is.
    return f"{platform}:{ident}"


def schedule_add(name: str, goal: str, schedule_type: str,
                 schedule_time: str, schedule_day: str = "",
                 notify_channel: str = "terminal") -> str:
    # Validate/normalize before writing — cron_runner.py (which later reads
    # this row) matches schedule_time/schedule_type/schedule_day/
    # notify_channel with strict string comparisons, so a malformed value
    # here wouldn't error anywhere — it would just silently never fire.
    schedule_type  = _normalize_schedule_type(schedule_type)
    schedule_time  = _normalize_schedule_time(schedule_time)
    schedule_day   = _normalize_schedule_day(schedule_type, schedule_day)
    notify_channel = _normalize_notify_channel(notify_channel)

    id_ = _store.add(name, goal, schedule_type, schedule_time, schedule_day, notify_channel)
    if schedule_type == "once":
        when = f"once on {schedule_day} at {schedule_time} (auto-deletes after running)"
    elif schedule_type == "weekly":
        when = f"every {schedule_day} at {schedule_time}"
    else:
        when = f"every day at {schedule_time}"
    return f"Scheduled task created (id={id_}): '{name}' — {when}"


def schedule_list() -> str:
    tasks = _store.list_all()
    if not tasks:
        return "No scheduled tasks."
    lines = []
    for t in tasks:
        status = "enabled" if t["enabled"] else "disabled"
        when = f"{t['schedule_type']} {t['schedule_time']}"
        if t.get("schedule_day"):
            when = f"{t['schedule_day']} {t['schedule_time']}"
        last = t.get("last_run") or "never"
        lines.append(f"[{t['id']}] {t['name']} | {when} | {status} | last: {last}\n  goal: {t['goal']}")
    return "\n".join(lines)


def schedule_delete(id: str) -> str:
    ok = _store.delete(id)
    return f"Deleted {id}" if ok else f"Task {id} not found"


def schedule_toggle(id: str, enabled: bool) -> str:
    ok = _store.toggle(id, enabled)
    state = "enabled" if enabled else "disabled"
    return f"Task {id} {state}" if ok else f"Task {id} not found"
