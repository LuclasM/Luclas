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


# ---------------------------------------------------------------------------
# Active channel/LLM health checks + self-alerting
#
# Without this, a dead channel (e.g. an expired WeCom corp secret, or the
# Discord bot silently failing to reconnect) is otherwise only discovered
# when a user reports they stopped getting replies — there's no other signal
# anywhere. This actively probes api.py's own /status (which api.py can
# check cheaply/accurately since it holds the live connection objects — see
# api.py:_channel_health) and pushes an alert to LUC_ADMIN_NOTIFY through the
# same already-working channels, rather than requiring a new notification
# system.
# ---------------------------------------------------------------------------

_HEALTH_CHECK_INTERVAL_MINUTES = 15
_HEALTH_ALERT_COOLDOWN_HOURS   = 6   # don't re-alert for an already-known ongoing failure more often than this
_HEALTH_STATE_FILE = os.path.join(DATA_DIR, "channel_health_state.json")


def _load_health_state() -> dict:
    import json as _json
    try:
        with open(_HEALTH_STATE_FILE) as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_health_state(state: dict) -> None:
    import json as _json
    try:
        with open(_HEALTH_STATE_FILE, "w") as f:
            _json.dump(state, f, indent=2)
    except Exception as e:
        _log(f"health state save failed: {e}")


def _notify_admin(content: str) -> None:
    """Send a health-check alert to LUC_ADMIN_NOTIFY — same "channel:id"
    format as a schedule's notify_channel (e.g. "wecom:U123"). No-op if unset
    (this whole feature is opt-in)."""
    target = os.environ.get("LUC_ADMIN_NOTIFY", "").strip()
    if not target:
        return
    if target.startswith("wecom:"):
        _notify_wecom(target[len("wecom:"):], content)
    elif target.startswith("whatsapp:"):
        _notify_whatsapp(target[len("whatsapp:"):], content)
    elif target.startswith("discord:"):
        _notify_discord(target[len("discord:"):], content)
    else:
        _log(f"LUC_ADMIN_NOTIFY has an unrecognized format: {target!r} (expected wecom:/whatsapp:/discord:)")


def _check_channel_health(now: datetime.datetime) -> None:
    """Runs every _HEALTH_CHECK_INTERVAL_MINUTES minutes. A persisted state
    file means a still-ongoing failure only re-alerts every
    _HEALTH_ALERT_COOLDOWN_HOURS, not on every single check, while a fresh
    failure or a recovery always alerts immediately."""
    if not os.environ.get("LUC_ADMIN_NOTIFY", "").strip():
        return  # opt-in — nothing configured to notify, nothing to check
    if now.minute % _HEALTH_CHECK_INTERVAL_MINUTES != 0:
        return

    import urllib.request, json as _json
    key = _load_api_key()
    try:
        req = urllib.request.Request(f"{_API_BASE}/status", headers={"X-API-Key": key})
        with urllib.request.urlopen(req, timeout=15) as r:
            current = _json.loads(r.read())
    except Exception as e:
        # api.py itself being unreachable is exactly the kind of thing this
        # is meant to catch — but there's nothing to notify *through* if the
        # service that would carry the notification is the thing that's down,
        # so just log it for now.
        _log(f"health check: could not reach {_API_BASE}/status: {e}")
        return

    checks = {"llm": current.get("llm") == "online"}
    for ch, info in (current.get("channels") or {}).items():
        if info.get("configured") and "healthy" in info:
            checks[ch] = info["healthy"]

    state   = _load_health_state()
    now_iso = now.isoformat()

    for name, healthy in checks.items():
        prev = state.get(name, {"healthy": True, "last_alert": ""})
        if healthy:
            if not prev["healthy"]:
                _notify_admin(f"✅ Luclas: {name} is back up.")
            state[name] = {"healthy": True, "last_alert": ""}
            continue

        just_broke  = prev["healthy"]
        stale_alert = True
        if prev.get("last_alert"):
            try:
                last_alert_dt = datetime.datetime.fromisoformat(prev["last_alert"])
                stale_alert = (now - last_alert_dt).total_seconds() >= _HEALTH_ALERT_COOLDOWN_HOURS * 3600
            except Exception:
                stale_alert = True

        if just_broke or stale_alert:
            detail = (current.get("channels") or {}).get(name, {}).get("detail", "")
            msg = f"⚠️ Luclas: {name} appears to be down."
            if detail:
                msg += f" ({detail})"
            _notify_admin(msg)
            state[name] = {"healthy": False, "last_alert": now_iso}
        else:
            state[name] = {"healthy": False, "last_alert": prev.get("last_alert", "")}

    _save_health_state(state)


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


_CHANNEL_LOCK_STALE_AFTER_SECONDS = 900  # generous margin over _poll_and_notify's ~10-minute budget


def _ensure_channel_lock_table(conn: sqlite3.Connection) -> None:
    """cron_runner.py is invoked standalone (no guarantee memory.database.init_db()
    has run in this deployment yet), so make sure the lock table exists here too —
    cheap and idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cron_channel_locks (
            channel   TEXT PRIMARY KEY,
            task_id   TEXT DEFAULT '',
            locked_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)


def _try_acquire_channel_lock(channel: str, owner: str) -> bool:
    """Cross-process channel lock, backed by the shared SQLite DB rather than
    in-memory state: cron_runner.py is a fresh OS process every minute (no
    persistent daemon), so an in-process dict can't stop two *different*
    invocations from both submitting a task on the same channel — the
    downstream API would silently merge the second submission into the first
    as a "supplement" instead of running it independently. Both branches
    below are single atomic statements (INSERT OR IGNORE / UPDATE ... WHERE)
    with no separate read-then-write step, so this is race-free even under
    concurrent acquire attempts from other processes — SQLite serializes
    writers, so by the time a second connection's statement actually runs,
    it sees the first connection's already-committed result.
    A lock older than _CHANNEL_LOCK_STALE_AFTER_SECONDS is treated as
    abandoned (the owning process crashed or was killed) and reclaimable."""
    now_iso = datetime.datetime.now().isoformat()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            _ensure_channel_lock_table(conn)
            cur = conn.execute(
                "INSERT OR IGNORE INTO cron_channel_locks (channel, task_id, locked_at) VALUES (?,?,?)",
                (channel, owner, now_iso),
            )
            if cur.rowcount > 0:
                conn.commit()
                return True
            cutoff = (datetime.datetime.now()
                      - datetime.timedelta(seconds=_CHANNEL_LOCK_STALE_AFTER_SECONDS)).isoformat()
            cur = conn.execute(
                "UPDATE cron_channel_locks SET task_id=?, locked_at=? WHERE channel=? AND locked_at<?",
                (owner, now_iso, channel, cutoff),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        _log(f"channel lock acquire failed for {channel}: {e}")
        return False


def _release_channel_lock(channel: str, owner: str) -> None:
    # AND task_id=owner guards against releasing a lock this row no longer
    # actually holds — e.g. this row's own task ran long enough to be
    # reclaimed as stale by another process before finishing, in which case
    # releasing unconditionally would drop the *new* owner's lock instead.
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            _ensure_channel_lock_table(conn)
            conn.execute("DELETE FROM cron_channel_locks WHERE channel=? AND task_id=?", (channel, owner))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log(f"channel lock release failed for {channel}: {e}")


def _handle_channel_row(row_id: str, row_name: str, goal: str, channel: str, stype: str) -> None:
    """Runs entirely on its own thread (see _check_scheduled), which has
    already acquired this channel's lock before spawning it: submits,
    polls+notifies, does this row's own last_run/delete bookkeeping, and
    always releases the channel lock on the way out. Uses its own sqlite
    connection since it may run concurrently with other channels' threads and
    with _check_scheduled's own connection — SQLite serializes writers at the
    file level, so a generous busy timeout plus not letting a bookkeeping
    error skip the notify step below matters here: the task itself has
    already been submitted successfully once task_id is set, so failing to
    record that locally must never also mean failing to tell the user the
    result."""
    try:
        task_id = _submit_task(goal, channel)

        try:
            conn = sqlite3.connect(DB_PATH, timeout=30)
            try:
                if not task_id:
                    # Submission never actually happened — don't count this occurrence
                    # as handled, or a transient outage would silently and permanently
                    # lose it (still bounded to today/this occurrence by the caller's checks).
                    conn.execute("UPDATE scheduled_tasks SET last_run='' WHERE id=?", (row_id,))
                    conn.commit()
                    _log(f"scheduled task [{row_id}] '{row_name}' submission failed — will retry")
                elif stype == "once":
                    conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (row_id,))
                    conn.commit()
                    _log(f"one-time task [{row_id}] deleted after successful trigger")
            finally:
                conn.close()
        except Exception as e:
            _log(f"scheduled task [{row_id}] '{row_name}' bookkeeping failed: {e}")

        if task_id:
            _poll_and_notify(task_id, channel)
    finally:
        _release_channel_lock(channel, row_id)


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

    # A generous busy timeout: once rows start spawning _handle_channel_row
    # threads (each opening its own connection), this connection stays in use
    # for later rows' bookkeeping concurrently with those threads' writes —
    # SQLite serializes writers at the file level, and the default 5s timeout
    # is tighter than ideal for that overlap.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE enabled=1"
        ).fetchall()
    except Exception:
        conn.close()
        return

    poll_threads = []
    # Two schedules sharing a notify_channel (e.g. the same WeCom user with
    # two reminders close together) must not be *submitted* concurrently:
    # /chat treats a second submission to a session that's still "running" as
    # a supplement into the first task rather than an independent task (see
    # api.py's /chat handler), which would silently merge the second
    # schedule's goal into the first's conversation and double up
    # notifications once both poll loops see the same task_id. This applies
    # across separate cron_runner.py invocations too, not just within one
    # pass of this loop — this script is a fresh OS process every minute (no
    # persistent daemon), and a single row's poll can legitimately take up to
    # ~10 minutes, so two schedules on the same channel a few minutes apart
    # are routinely handled by two *different* processes with no shared
    # in-memory state between them. _try_acquire_channel_lock() is the
    # cross-process fix (a DB-backed lock, checked and — if busy — deferred
    # to a later run, before this row is even marked as triggered).

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

        if not is_terminal and not _try_acquire_channel_lock(channel, row["id"]):
            _log(f"scheduled task [{row['id']}] '{row['name']}' deferred — channel '{channel}' busy with another in-flight task")
            continue  # last_run untouched — picked up again once the channel frees up

        try:
            conn.execute(
                "UPDATE scheduled_tasks SET last_run=? WHERE id=?",
                (now.strftime("%Y-%m-%d %H:%M:%S"), row["id"]),
            )
            conn.commit()
            _log(f"scheduled task [{row['id']}] '{row['name']}' triggered")
        except Exception as e:
            # Couldn't even mark it triggered — don't proceed as if we had
            # (that would submit it with no local record of having done so).
            # Release any lock we just took so it isn't held for nothing.
            _log(f"scheduled task [{row['id']}] '{row['name']}' failed to record trigger, skipping this run: {e}")
            if not is_terminal:
                _release_channel_lock(channel, row["id"])
            continue

        if not is_terminal:
            # Everything from here — submitting, polling+notifying, this
            # row's own bookkeeping, and releasing the channel lock — runs on
            # its own thread so the main loop moves on to the next row
            # immediately, regardless of channel.
            t = threading.Thread(target=_handle_channel_row,
                                  args=(row["id"], row["name"], row["goal"], channel, stype),
                                  daemon=True)
            t.start()
            poll_threads.append(t)
            continue

        submitted = _launch(["--run", row["goal"]], f"sched_{row['id']}")
        try:
            if not submitted:
                # Submission never actually happened — don't count this occurrence
                # as handled, or a transient outage would silently and permanently
                # lose it (still bounded to today/this occurrence by the checks above).
                conn.execute("UPDATE scheduled_tasks SET last_run='' WHERE id=?", (row["id"],))
                conn.commit()
                _log(f"scheduled task [{row['id']}] '{row['name']}' submission failed — will retry")
            elif stype == "once":
                conn.execute("DELETE FROM scheduled_tasks WHERE id=?", (row["id"],))
                conn.commit()
                _log(f"one-time task [{row['id']}] deleted after successful trigger")
        except Exception as e:
            _log(f"scheduled task [{row['id']}] '{row['name']}' bookkeeping failed: {e}")

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
    # Goes through the independent API service (a plain HTTP GET), same as
    # channel-routed scheduled tasks — never skipped for an interactive
    # session for the same reason those aren't.
    _check_channel_health(now)


if __name__ == "__main__":
    main()
