import datetime
import uuid
from memory.database import get_conn


class ScheduledTaskStore:

    def add(self, name: str, goal: str, schedule_type: str,
            schedule_time: str, schedule_day: str = "",
            notify_channel: str = "terminal") -> str:
        id_ = uuid.uuid4().hex[:8]
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, goal, schedule_type, schedule_time, schedule_day, notify_channel, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (id_, name, goal, schedule_type, schedule_time, schedule_day, notify_channel, now),
            )
        return id_

    def list_all(self) -> list[dict]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def toggle(self, id_: str, enabled: bool) -> bool:
        with get_conn() as conn:
            n = conn.execute(
                "UPDATE scheduled_tasks SET enabled=? WHERE id=?",
                (1 if enabled else 0, id_),
            ).rowcount
        return n > 0

    def delete(self, id_: str) -> bool:
        with get_conn() as conn:
            n = conn.execute(
                "DELETE FROM scheduled_tasks WHERE id=?", (id_,)
            ).rowcount
        return n > 0
