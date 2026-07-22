"""
cron_runner.py has no persistent daemon — a fresh OS process runs every
minute — so an in-process lock can't stop two *different* invocations from
both submitting a task on the same messaging channel at once (the API
silently merges a second concurrent submission into the first as a
"supplement" instead of running it independently). These tests exercise the
cross-process channel lock (a SQLite table, not in-memory state) that fixes
that, plus the concurrent-write DB contention it deliberately tolerates.
"""
import datetime
import sqlite3
import threading
import time

import cron_runner as cr


# ── Direct lock-mechanism tests ─────────────────────────────────────────────

def test_lock_basic_acquire_release(isolated_db, monkeypatch):
    monkeypatch.setattr(cr, "DB_PATH", isolated_db["db_path"])
    assert cr._try_acquire_channel_lock("wecom:U1", "owner-A") is True
    assert cr._try_acquire_channel_lock("wecom:U1", "owner-B") is False, \
        "a second owner must not acquire a lock already held"
    cr._release_channel_lock("wecom:U1", "owner-A")
    assert cr._try_acquire_channel_lock("wecom:U1", "owner-C") is True, \
        "acquire should succeed again once released"


def test_lock_channels_are_independent(isolated_db, monkeypatch):
    monkeypatch.setattr(cr, "DB_PATH", isolated_db["db_path"])
    assert cr._try_acquire_channel_lock("wecom:U1", "owner-A") is True
    assert cr._try_acquire_channel_lock("wecom:U2", "owner-B") is True, \
        "a different channel must never be blocked by another channel's lock"


def test_lock_wrong_owner_cannot_release(isolated_db, monkeypatch):
    monkeypatch.setattr(cr, "DB_PATH", isolated_db["db_path"])
    assert cr._try_acquire_channel_lock("wecom:U3", "real-owner") is True
    cr._release_channel_lock("wecom:U3", "wrong-owner")  # must be a no-op
    assert cr._try_acquire_channel_lock("wecom:U3", "someone-else") is False, \
        "releasing with the wrong owner token must not free a lock it doesn't hold"
    cr._release_channel_lock("wecom:U3", "real-owner")
    assert cr._try_acquire_channel_lock("wecom:U3", "someone-else") is True


def test_lock_stale_reclaim(isolated_db, monkeypatch):
    monkeypatch.setattr(cr, "DB_PATH", isolated_db["db_path"])
    conn = sqlite3.connect(isolated_db["db_path"])
    cr._ensure_channel_lock_table(conn)
    stale_time = (datetime.datetime.now()
                  - datetime.timedelta(seconds=cr._CHANNEL_LOCK_STALE_AFTER_SECONDS + 60)).isoformat()
    conn.execute("INSERT INTO cron_channel_locks (channel, task_id, locked_at) VALUES (?,?,?)",
                 ("wecom:U4", "crashed-owner", stale_time))
    conn.commit()
    conn.close()
    assert cr._try_acquire_channel_lock("wecom:U4", "new-owner") is True, \
        "a lock older than the staleness window (owning process crashed/killed) must be reclaimable"


def test_lock_no_double_acquire_under_concurrency(isolated_db, monkeypatch):
    monkeypatch.setattr(cr, "DB_PATH", isolated_db["db_path"])
    violations = {"n": 0}
    held = {"flag": False}
    state_lock = threading.Lock()

    def racer(owner):
        if cr._try_acquire_channel_lock("wecom:RACE", owner):
            with state_lock:
                if held["flag"]:
                    violations["n"] += 1
                held["flag"] = True
            time.sleep(0.02)
            with state_lock:
                held["flag"] = False
            cr._release_channel_lock("wecom:RACE", owner)

    threads = [threading.Thread(target=racer, args=(f"racer-{i}",)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert violations["n"] == 0, "the lock must never be held by two owners at once"


# ── _check_scheduled() behavior under same-channel vs cross-channel timing ──

def _seed_schedule(db_path, id_, name, schedule_type, schedule_time, schedule_day, notify_channel):
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY, name TEXT, goal TEXT, schedule_type TEXT,
            schedule_time TEXT, schedule_day TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
            last_run TEXT DEFAULT '', notify_channel TEXT DEFAULT 'terminal',
            created_at TEXT DEFAULT ''
        );
    """)
    conn.execute(
        "INSERT INTO scheduled_tasks (id,name,goal,schedule_type,schedule_time,schedule_day,notify_channel) "
        "VALUES (?,?,?,?,?,?,?)",
        (id_, name, f"do {name}", schedule_type, schedule_time, schedule_day, notify_channel),
    )
    conn.commit()
    conn.close()


def test_same_channel_serializes_but_different_channel_does_not_wait(isolated_db, monkeypatch):
    monkeypatch.setattr(cr, "DB_PATH", isolated_db["db_path"])
    now = datetime.datetime(2026, 7, 21, 9, 0, 0)
    today = now.strftime("%Y-%m-%d")
    _seed_schedule(isolated_db["db_path"], "r1", "slow same-channel", "once", "09:00", today, "wecom:SAMEUSER")
    _seed_schedule(isolated_db["db_path"], "r2", "same-channel, must defer", "once", "09:00", today, "wecom:SAMEUSER")
    _seed_schedule(isolated_db["db_path"], "r3", "different channel", "once", "09:00", today, "wecom:OTHERUSER")

    events = []
    lock = threading.Lock()

    def fake_submit(goal, channel):
        with lock:
            events.append(("submit", goal, channel, time.time()))
        return f"task-{goal}"

    def fake_poll(task_id, channel):
        if "reminder 1" in task_id or "r1" in task_id or "slow" in task_id:
            time.sleep(0.4)
        else:
            time.sleep(0.02)

    monkeypatch.setattr(cr, "_submit_task", fake_submit)
    monkeypatch.setattr(cr, "_poll_and_notify", fake_poll)

    t0 = time.time()
    cr._check_scheduled(now, skip_terminal_launch=False)

    r3_time = next(e[3] for e in events if e[0] == "submit" and "different channel" in e[1]) - t0
    r2_submitted = any(e[0] == "submit" and "must defer" in e[1] for e in events)

    assert r3_time < 0.3, "a different channel must not be delayed by an unrelated slow same-channel pair"
    assert not r2_submitted, "a same-channel row whose predecessor is still in flight must be deferred, not submitted"

    conn = sqlite3.connect(isolated_db["db_path"])
    conn.row_factory = sqlite3.Row
    r2 = conn.execute("SELECT * FROM scheduled_tasks WHERE id='r2'").fetchone()
    conn.close()
    assert r2 is not None, "the deferred row must still exist (not lost)"
    assert (r2["last_run"] or "") == "", "the deferred row's last_run must be untouched so it retries next run"


def test_db_write_contention_all_notified(isolated_db, monkeypatch):
    """30 same-minute, different-channel schedules stress the SQLite write
    path (main loop's connection + one connection per background thread) —
    every one must still get notified despite the contention."""
    monkeypatch.setattr(cr, "DB_PATH", isolated_db["db_path"])
    now = datetime.datetime(2026, 7, 21, 9, 0, 0)
    today = now.strftime("%Y-%m-%d")
    n = 30
    for i in range(n):
        _seed_schedule(isolated_db["db_path"], f"r{i}", f"reminder {i}", "once", "09:00", today, f"wecom:USER{i}")

    notified = []
    notify_lock = threading.Lock()

    monkeypatch.setattr(cr, "_submit_task", lambda goal, channel: f"task-{goal}")

    def fake_poll(task_id, channel):
        with notify_lock:
            notified.append(task_id)

    monkeypatch.setattr(cr, "_poll_and_notify", fake_poll)

    cr._check_scheduled(now, skip_terminal_launch=False)

    conn = sqlite3.connect(isolated_db["db_path"])
    remaining = conn.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0]
    conn.close()

    assert len(notified) == n, "every row must get notified despite concurrent DB write contention"
    assert remaining == 0, "all successfully-handled 'once' rows should be deleted"
