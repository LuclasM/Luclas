#!/usr/bin/env python3
"""
Luclas cron runner — system-level scheduler for nightly reflection and user-defined tasks.

Add to crontab (crontab -e):
    * * * * * /usr/bin/python3 /home/luclas/Luclas/luclas/cron_runner.py >> /home/luclas/Luclas/data/sessions/logs/cron.log 2>&1

This script is called every minute. It does nothing if an interactive Luclas session
is currently running (checked via PID file).
"""
import datetime
import os
import sqlite3
import subprocess
import sys
import threading
import time

CODE_DIR = os.path.dirname(os.path.abspath(__file__))   # luclas/
sys.path.insert(0, CODE_DIR)

from config import DATA_DIR, DB_PATH

_PID_FILE    = os.path.join(DATA_DIR, "luclas.pid")
_ACTIVE_FILE = os.path.join(DATA_DIR, "last_active")
_LOG_DIR     = os.path.join(DATA_DIR, "sessions", "logs")
_LUCLAS_PY      = os.path.join(CODE_DIR, "luclas.py")
_API_BASE    = os.environ.get("LUC_API_BASE", "http://localhost:8080")
_API_KEY     = ""   # loaded lazily from .env

_DAY_NAMES   = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _load_api_key() -> str:
    global _API_KEY
    if _API_KEY:
        return _API_KEY
    env_path = os.path.join(os.path.dirname(CODE_DIR), ".env")
    try:
        for line in open(env_path):
            line = line.strip()
            if line.startswith("LUC_API_KEY="):
                _API_KEY = line.split("=", 1)[1].strip()
                break
    except Exception:
        pass
    return _API_KEY


def _notify_wecom(user_id: str, content: str) -> None:
    """Send content to a WeCom user via the local API."""
    import urllib.request, urllib.parse, json as _json
    key = _load_api_key()
    data = _json.dumps({"line": f"__wecom_send__{user_id}__", "_direct": content}).encode()
    # Use the /command endpoint indirectly — actually POST /chat and let wecom handle it.
    # Simpler: call WeCom send API directly.
    env_path = os.path.join(os.path.dirname(CODE_DIR), ".env")
    env = {}
    try:
        for line in open(env_path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except Exception:
        return
    corp_id  = env.get("WECOM_CORP_ID", "")
    secret   = env.get("WECOM_SECRET", "")
    agent_id = env.get("WECOM_AGENT_ID", "")
    if not all([corp_id, secret, agent_id]):
        _log("wecom notify: missing credentials")
        return
    try:
        # Get token
        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corp_id}&corpsecret={secret}"
        with urllib.request.urlopen(url, timeout=10) as r:
            token_data = _json.loads(r.read())
        token = token_data.get("access_token", "")
        if not token:
            _log(f"wecom notify: token error {token_data}")
            return
        # Send message
        payload = _json.dumps({
            "touser": user_id, "msgtype": "text",
            "agentid": int(agent_id), "text": {"content": content},
        }).encode()
        req = urllib.request.Request(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            data=payload, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = _json.loads(r.read())
        if result.get("errcode", 0) != 0:
            _log(f"wecom notify: send error {result}")
        else:
            _log(f"wecom notify: sent to {user_id}")
    except Exception as e:
        _log(f"wecom notify: exception {e}")


def _notify_whatsapp(phone: str, content: str) -> None:
    """Send content to a WhatsApp number via Meta Graph API."""
    import urllib.request, json as _json
    env_path = os.path.join(os.path.dirname(CODE_DIR), ".env")
    env: dict[str, str] = {}
    try:
        for line in open(env_path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except Exception:
        return
    phone_number_id = env.get("WHATSAPP_PHONE_NUMBER_ID", "")
    access_token    = env.get("WHATSAPP_ACCESS_TOKEN", "")
    if not phone_number_id or not access_token:
        _log("whatsapp notify: missing credentials")
        return
    try:
        payload = _json.dumps({
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": content},
        }).encode()
        req = urllib.request.Request(
            f"https://graph.facebook.com/v19.0/{phone_number_id}/messages",
            data=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        _log(f"whatsapp notify: sent to {phone}")
    except Exception as e:
        _log(f"whatsapp notify: exception {e}")


def _notify_discord(user_id: str, content: str) -> None:
    """Send a DM to a Discord user via REST API."""
    import urllib.request, json as _json
    env_path = os.path.join(os.path.dirname(CODE_DIR), ".env")
    env: dict[str, str] = {}
    try:
        for line in open(env_path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except Exception:
        return
    bot_token = env.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        _log("discord notify: missing bot token")
        return
    try:
        # Create DM channel
        payload = _json.dumps({"recipient_id": user_id}).encode()
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me/channels",
            data=payload,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            dm = _json.loads(r.read())
        dm_channel_id = dm["id"]
        # Send message
        payload = _json.dumps({"content": content[:2000]}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{dm_channel_id}/messages",
            data=payload,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        _log(f"discord notify: DM sent to {user_id}")
    except Exception as e:
        _log(f"discord notify: exception {e}")


def _submit_task(goal: str, notify_channel: str) -> str | None:
    """POST /chat to submit the task. Returns the task_id, or None if the
    submission itself failed (server down, network error, etc.) — the caller
    uses this to decide whether it's safe to treat the schedule as "handled"
    (delete a 'once' row / advance last_run) or whether it must be retried.

    session_id uses a "cron_"-prefixed, underscore-joined form (e.g.
    "cron_wecom_U123") deliberately distinct from the live chat session_id a
    user's own messages arrive under (e.g. "wecom_U123") — reusing the same
    session_id could get a scheduled task silently merged as a "supplement"
    into whatever unrelated conversation the user happens to have running at
    that moment. api.py's _make_push_callback() strips the "cron_" prefix
    before matching, so channel push (progress/ask_user/errors) still works.
    """
    import urllib.request, json as _json
    key = _load_api_key()
    session_id = "cron_" + notify_channel.replace(":", "_", 1)
    try:
        data = _json.dumps({"message": goal, "session_id": session_id}).encode()
        req = urllib.request.Request(
            f"{_API_BASE}/chat", data=data,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return _json.loads(r.read())["task_id"]
    except Exception as e:
        _log(f"api submit failed: {e}")
        return None


def _poll_and_notify(task_id: str, notify_channel: str) -> None:
    """Poll /result until done/failed (or timeout), then deliver to the right
    channel. Runs on its own thread (started by _check_scheduled) so multiple
    tasks due at the same minute poll concurrently instead of each blocking
    the next behind up to 10 minutes of waiting."""
    import urllib.request, json as _json
    key = _load_api_key()

    def _get(path):
        req = urllib.request.Request(f"{_API_BASE}{path}", headers={"X-API-Key": key})
        with urllib.request.urlopen(req, timeout=10) as r:
            return _json.loads(r.read())

    result = None
    for _ in range(300):
        time.sleep(2)
        try:
            r = _get(f"/result/{task_id}")
            if r["status"] in ("done", "failed"):
                result = r.get("result", "")
                break
        except Exception:
            continue

    if result is None:
        result = "⏱ 任务超时"

    if notify_channel.startswith("wecom:"):
        user_id = notify_channel[len("wecom:"):]
        _notify_wecom(user_id, result or "✅ 完成")
    elif notify_channel.startswith("whatsapp:"):
        phone = notify_channel[len("whatsapp:"):]
        _notify_whatsapp(phone, result or "✅ Done")
    elif notify_channel.startswith("discord:"):
        user_id = notify_channel[len("discord:"):]
        _notify_discord(user_id, result or "✅ Done")
    else:
        _log(f"task result (terminal):\n{result}")


def _luclas_running() -> bool:
    if not os.path.isfile(_PID_FILE):
        return False
    try:
        pid = int(open(_PID_FILE).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def _idle_hours() -> float:
    if not os.path.isfile(_ACTIVE_FILE):
        return float("inf")
    try:
        ts   = open(_ACTIVE_FILE).read().strip()
        last = datetime.datetime.fromisoformat(ts)
        return (datetime.datetime.now() - last).total_seconds() / 3600
    except Exception:
        return float("inf")


def _launch(extra_args: list[str], log_suffix: str) -> bool:
    """Returns True iff the subprocess was actually started, so callers can
    tell a real launch failure (e.g. interpreter missing, log dir unwritable)
    apart from success — a schedule shouldn't be considered "handled" if the
    process never started."""
    os.makedirs(_LOG_DIR, exist_ok=True)
    stamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(_LOG_DIR, f"{log_suffix}_{stamp}.log")
    try:
        with open(log_path, "w") as lf:
            subprocess.Popen(
                [sys.executable, _LUCLAS_PY] + extra_args,
                stdout=lf, stderr=lf,
                cwd=CODE_DIR, start_new_session=True,
            )
        _log(f"launched {extra_args} → {log_path}")
        return True
    except Exception as e:
        _log(f"launch failed: {extra_args} → {e}")
        return False


def _check_reflection(now: datetime.datetime) -> None:
    if now.hour != 4 or now.minute != 0:
        return
    idle = _idle_hours()
    if idle >= 1:
        _log(f"nightly reflection triggered (idle {idle:.1f}h)")
        _launch(["--reflect"], "reflect")
    else:
        _log(f"nightly reflection skipped (idle only {idle:.1f}h)")


def _check_scheduled(now: datetime.datetime, skip_terminal_launch: bool = False) -> None:
    """skip_terminal_launch: True when an interactive CLI session is running
    (see main()) — only schedules with no notify_channel (which would launch
    a competing headless `luclas.py --run` process) are deferred; channel-
    routed schedules go through the independent API service and are always
    checked, regardless of what the interactive terminal is doing."""
    if not os.path.isfile(DB_PATH):
        return
    today      = _DAY_NAMES[now.weekday()]
    today_date = now.strftime("%Y-%m-%d")
    now_hhmm   = now.strftime("%H:%M")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE enabled=1"
        ).fetchall()
    except Exception:
        conn.close()
        return

    poll_threads = []

    for row in rows:
        stype       = row["schedule_type"]
        sched_hhmm  = row["schedule_time"]
        last_run    = row["last_run"] or ""

        # Due-or-past-due-and-not-yet-run, rather than exact-minute equality —
        # so a minute this script misses (interactive session open, cron_runner
        # itself didn't fire, transient failure below) still catches up the
        # next time it runs, instead of silently losing that occurrence.
        if not sched_hhmm or now_hhmm < sched_hhmm:
            continue
        if stype == "weekly" and row["schedule_day"] != today:
            continue
        if stype == "once" and row["schedule_day"] != today_date:
            continue
        already_ran = bool(last_run) if stype == "once" else last_run.startswith(today_date)
        if already_ran:
            continue

        channel     = row["notify_channel"] if "notify_channel" in row.keys() else "terminal"
        is_terminal = not channel or channel == "terminal"
        if is_terminal and skip_terminal_launch:
            _log(f"scheduled task [{row['id']}] '{row['name']}' deferred — interactive session active")
            continue  # last_run untouched — picked up again next run

        conn.execute(
            "UPDATE scheduled_tasks SET last_run=? WHERE id=?",
            (now.strftime("%Y-%m-%d %H:%M:%S"), row["id"]),
        )
        conn.commit()
        _log(f"scheduled task [{row['id']}] '{row['name']}' triggered")

        submitted = False
        if not is_terminal:
            task_id = _submit_task(row["goal"], channel)
            if task_id:
                submitted = True
                t = threading.Thread(target=_poll_and_notify, args=(task_id, channel), daemon=True)
                t.start()
                poll_threads.append(t)
        else:
            submitted = _launch(["--run", row["goal"]], f"sched_{row['id']}")

        if not submitted:
            # Submission never actually happened — don't count this occurrence
            # as handled, or a transient outage would silently and permanently
            # lose it (still bounded to today/this occurrence by the checks above).
            conn.execute("UPDATE scheduled_tasks SET last_run='' WHERE id=?", (row["id"],))
            conn.commit()
            _log(f"scheduled task [{row['id']}] '{row['name']}' submission failed — will retry")
            continue

        if stype == "once":
            conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (row["id"],))
            conn.commit()
            _log(f"one-time task [{row['id']}] deleted after successful trigger")

    conn.close()

    # Let same-minute channel-routed tasks poll/notify concurrently instead of
    # one blocking the next behind up to 10 minutes of waiting.
    for t in poll_threads:
        t.join()


def _log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main():
    # Interactive session active: only skip work that would launch a
    # competing headless `luclas.py` process against the same PID/terminal
    # resources. Channel-routed scheduled tasks go through the independent
    # API service and don't touch any of that, so they're never skipped.
    interactive = _luclas_running()
    now = datetime.datetime.now()
    if not interactive:
        _check_reflection(now)
    _check_scheduled(now, skip_terminal_launch=interactive)


if __name__ == "__main__":
    main()
