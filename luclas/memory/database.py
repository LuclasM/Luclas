import sqlite3
import os
from contextlib import contextmanager
from config import DB_PATH


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id          TEXT PRIMARY KEY,
                content     TEXT NOT NULL,
                type        TEXT DEFAULT '',
                tags        TEXT DEFAULT '[]',
                importance  INTEGER DEFAULT 5,
                source      TEXT DEFAULT '',
                credibility INTEGER DEFAULT 0,
                access_count INTEGER DEFAULT 0,
                embedding   BLOB,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                goal          TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_time TEXT NOT NULL,
                schedule_day  TEXT DEFAULT '',
                enabled       INTEGER DEFAULT 1,
                last_run      TEXT DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            );

            -- cron_runner.py is a fresh OS process per minute (no persistent
            -- daemon), so an in-memory lock can't stop two *different*
            -- invocations from both submitting a task on the same messaging
            -- channel at once (the downstream API silently merges a second
            -- concurrent submission into the first as a "supplement" instead
            -- of running it independently). This table is the shared,
            -- cross-process lock: one row per channel currently in flight.
            CREATE TABLE IF NOT EXISTS cron_channel_locks (
                channel   TEXT PRIMARY KEY,
                task_id   TEXT DEFAULT '',
                locked_at TEXT DEFAULT (datetime('now','localtime'))
            );

            -- One row per LLM call (see llm_client.py:_record_usage) — there
            -- was previously no visibility into token usage at all, even
            -- though every OpenAI-compatible response already carries a
            -- `usage` field that was just being discarded. Rows are tiny and
            -- not pruned/archived — even heavy daily use keeps this table
            -- small for a long time; revisit if that stops being true.
            CREATE TABLE IF NOT EXISTS llm_usage (
                id                 TEXT PRIMARY KEY,
                model              TEXT DEFAULT '',
                prompt_tokens      INTEGER DEFAULT 0,
                completion_tokens  INTEGER DEFAULT 0,
                total_tokens       INTEGER DEFAULT 0,
                created_at         TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at ON llm_usage(created_at);

            -- Persistent per-user/per-channel conversation. One row per
            -- conversation_id (channel-derived, e.g. "wecom_<UserID>", or the
            -- fixed "cli_local" for the interactive CLI). `messages` is the
            -- live/active-window transcript as a JSON array; older turns move
            -- out into `episodes` once they've been closed out (topic
            -- changed) and the window gets too big, not deleted.
            CREATE TABLE IF NOT EXISTS conversations (
                id                     TEXT PRIMARY KEY,
                messages               TEXT DEFAULT '[]',
                current_episode_id     TEXT DEFAULT '',
                active_task_id         TEXT DEFAULT '',
                active_task_foreground INTEGER DEFAULT 0,
                created_at             TEXT DEFAULT (datetime('now','localtime')),
                updated_at             TEXT DEFAULT (datetime('now','localtime'))
            );

            -- Episodic memory ("经历"): either one finished task execution
            -- (kind='task', task_id set) or one topic-segment of a
            -- conversation (kind='conversation'). Replaces task_records.
            -- importance rises on every reference; freshness only decays
            -- with time and is never refreshed by a reference (unlike
            -- lessons in `memories` — see the freshness/linked_episode_ids/
            -- granularity columns added there in _migrate()).
            CREATE TABLE IF NOT EXISTS episodes (
                id                 TEXT PRIMARY KEY,
                conversation_id    TEXT DEFAULT '',
                kind               TEXT DEFAULT 'conversation',
                task_id            TEXT DEFAULT '',
                content            TEXT NOT NULL,
                granularity        TEXT DEFAULT 'raw',
                importance         INTEGER DEFAULT 5,
                freshness          REAL DEFAULT 1.0,
                reference_count    INTEGER DEFAULT 0,
                linked_lesson_ids  TEXT DEFAULT '[]',
                created_at         TEXT DEFAULT (datetime('now','localtime')),
                last_referenced_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_conversation ON episodes(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_episodes_compress_rank ON episodes(granularity, importance, freshness);
        """)
    _migrate()


def _migrate():
    """幂等迁移：为旧数据库补列/补表。"""
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN source TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN credibility INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            # credibility switched from a high/medium/low TEXT column to a 1-10
            # INTEGER scale (matching importance). Convert any DB still on the old
            # TEXT column: capture existing values, rebuild the column, write back
            # the converted numbers. No-op on fresh DBs (already INTEGER above).
            col_types = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(memories)")}
            if col_types.get("credibility", "").upper() != "INTEGER":
                rows = conn.execute("SELECT id, credibility FROM memories").fetchall()
                _cred_map = {"high": 9, "medium": 6, "low": 3}
                converted = {
                    r["id"]: _cred_map.get((r["credibility"] or "").strip().lower(), 0)
                    for r in rows
                }
                conn.execute("ALTER TABLE memories DROP COLUMN credibility")
                conn.execute("ALTER TABLE memories ADD COLUMN credibility INTEGER DEFAULT 0")
                for mid, val in converted.items():
                    if val:
                        conn.execute("UPDATE memories SET credibility=? WHERE id=?", (val, mid))
        except Exception:
            pass
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id            TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    goal          TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_time TEXT NOT NULL,
                    schedule_day  TEXT DEFAULT '',
                    enabled       INTEGER DEFAULT 1,
                    last_run      TEXT DEFAULT '',
                    notify_channel TEXT DEFAULT 'terminal',
                    created_at    TEXT DEFAULT (datetime('now','localtime'))
                );
            """)
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN notify_channel TEXT DEFAULT 'terminal'")
        except Exception:
            pass
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cron_channel_locks (
                    channel   TEXT PRIMARY KEY,
                    task_id   TEXT DEFAULT '',
                    locked_at TEXT DEFAULT (datetime('now','localtime'))
                );
            """)
        except Exception:
            pass
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id                 TEXT PRIMARY KEY,
                    model              TEXT DEFAULT '',
                    prompt_tokens      INTEGER DEFAULT 0,
                    completion_tokens  INTEGER DEFAULT 0,
                    total_tokens       INTEGER DEFAULT 0,
                    created_at         TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at ON llm_usage(created_at);
            """)
        except Exception:
            pass

        try:
            conn.execute("DROP TABLE IF EXISTS tasks")
        except Exception:
            pass

        # Persistent conversation layer (episodes/lessons memory redesign):
        # conversations + episodes are new tables; memories gains freshness/
        # linked_episode_ids/granularity so lessons ("经验") share the same
        # importance/freshness/granularity compression scheme as episodes
        # ("经历"). See memory/decay.py for the shared compression logic.
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id                     TEXT PRIMARY KEY,
                    messages               TEXT DEFAULT '[]',
                    current_episode_id     TEXT DEFAULT '',
                    active_task_id         TEXT DEFAULT '',
                    active_task_foreground INTEGER DEFAULT 0,
                    created_at             TEXT DEFAULT (datetime('now','localtime')),
                    updated_at             TEXT DEFAULT (datetime('now','localtime'))
                );
            """)
        except Exception:
            pass
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id                 TEXT PRIMARY KEY,
                    conversation_id    TEXT DEFAULT '',
                    kind               TEXT DEFAULT 'conversation',
                    task_id            TEXT DEFAULT '',
                    content            TEXT NOT NULL,
                    granularity        TEXT DEFAULT 'raw',
                    importance         INTEGER DEFAULT 5,
                    freshness          REAL DEFAULT 1.0,
                    reference_count    INTEGER DEFAULT 0,
                    linked_lesson_ids  TEXT DEFAULT '[]',
                    created_at         TEXT DEFAULT (datetime('now','localtime')),
                    last_referenced_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_episodes_conversation ON episodes(conversation_id);
                CREATE INDEX IF NOT EXISTS idx_episodes_compress_rank ON episodes(granularity, importance, freshness);
            """)
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN freshness REAL DEFAULT 1.0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN linked_episode_ids TEXT DEFAULT '[]'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN granularity TEXT DEFAULT 'raw'")
        except Exception:
            pass

        # One-time migration: task_records/task_summaries are fully retired
        # (superseded by episodes — see the CREATE TABLE episodes comment
        # above). Any existing rows get folded into episodes (kind='task',
        # granularity='summary' since a saved `summary` is already condensed,
        # not a raw transcript) before the old tables are dropped, so
        # pre-migration task history stays searchable via memory_search
        # instead of silently vanishing. created_at is backdated to the
        # original completed_at (not "now") so already-old tasks decay/
        # compress on their real schedule rather than looking freshly made.
        # Idempotent: no-ops once task_records no longer exists.
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "task_records" in tables:
                rows = conn.execute(
                    "SELECT id, session_id, goal, summary, importance, completed_at FROM task_records"
                ).fetchall()
                for r in rows:
                    content = (r["summary"] or "").strip()
                    content = f"{r['goal']}\n\n{content}" if content and content != r["goal"] else (content or r["goal"])
                    if not content:
                        continue
                    conn.execute("""
                        INSERT OR IGNORE INTO episodes
                          (id, conversation_id, kind, task_id, content, granularity,
                           importance, freshness, reference_count, linked_lesson_ids,
                           created_at, last_referenced_at)
                        VALUES (?,?,?,?,?,'summary',?,1.0,0,'[]',?,?)
                    """, (
                        f"migrated-{r['id']}", r["session_id"] or "", "task", r["id"],
                        content, max(1, min(10, r["importance"] or 7)),
                        r["completed_at"], r["completed_at"],
                    ))
                conn.execute("DROP TABLE IF EXISTS task_records")
                conn.execute("DROP TABLE IF EXISTS task_summaries")
                if rows:
                    print(f"[migrate] moved {len(rows)} task_records row(s) into episodes; dropped task_records/task_summaries")
        except Exception:
            pass


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
