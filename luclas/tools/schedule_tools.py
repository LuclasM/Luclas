import json
from memory.schedule import ScheduledTaskStore

_store = ScheduledTaskStore()

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


def schedule_add(name: str, goal: str, schedule_type: str,
                 schedule_time: str, schedule_day: str = "",
                 notify_channel: str = "terminal") -> str:
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
