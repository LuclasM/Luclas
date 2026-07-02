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
                access_count INTEGER DEFAULT 0,
                embedding   BLOB,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                goal        TEXT NOT NULL,
                status      TEXT DEFAULT 'active',
                log         TEXT DEFAULT '',
                result      TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS task_records (
                id           TEXT PRIMARY KEY,
                session_id   TEXT DEFAULT '',
                goal         TEXT NOT NULL,
                summary      TEXT DEFAULT '',
                artifacts    TEXT DEFAULT '[]',
                tree         TEXT DEFAULT '{}',
                importance   INTEGER DEFAULT 7,
                tier         TEXT DEFAULT 'active',
                created_at   TEXT DEFAULT (datetime('now','localtime')),
                completed_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS task_summaries (
                id           TEXT PRIMARY KEY,
                content      TEXT NOT NULL,
                period_start TEXT DEFAULT '',
                period_end   TEXT DEFAULT '',
                record_ids   TEXT DEFAULT '[]',
                created_at   TEXT DEFAULT (datetime('now','localtime'))
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
