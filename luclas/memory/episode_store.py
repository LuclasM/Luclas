"""
memory/episode_store.py — 情景记忆（"经历"）

一条经历要么是一次任务执行（kind='task', task_id 指向该次任务自己的 task_id，
即用户所说"完成某个任务的 session"，用于精确引用），要么是一段对话话题
（kind='conversation'，由 conversation_runner 在检测到话题切换时落库）。

新鲜度只随时间衰减（created_at 起算），被引用不会让它回升——这是经历和
memory/store.py 里的经验（lessons）之间的关键不对称，经验每次被引用会刷新
新鲜度。重要性规则相同：每次被引用（结构链接或检索命中并被使用）+1。

压缩/遗忘沿用 memory/task_memory.py::maybe_compress() 已验证过的并发安全模式：
先用条件 UPDATE（按 rowcount 判断是否抢到）原子"认领"一批候选，认领成功才发
LLM 摘要调用，失败/被别的调用者抢先则原样释放。
"""

import datetime
import json
import threading
import uuid

from memory.database import get_conn
import memory.decay as decay

# link_lesson() is a read-modify-write on linked_lesson_ids (read the JSON
# array, append in Python, write the whole array back) — the same class of
# lost-update race conversation_store.py's lock addresses, just lower
# frequency (once per AAR-linked task rather than per message). A per-
# episode-id lock is cheap enough to just always take.
_locks_guard = threading.Lock()
_episode_locks: dict = {}


def _lock_for(episode_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _episode_locks.get(episode_id)
        if lock is None:
            lock = threading.Lock()
            _episode_locks[episode_id] = lock
        return lock


class EpisodeStore:
    def create_task_episode(self, conversation_id: str, task_id: str, content: str, importance: int = 7) -> str:
        return self._insert(conversation_id, "task", content, task_id=task_id, importance=importance)

    def close_conversation_episode(self, episode_id: str, conversation_id: str, content: str, importance: int = 5) -> str:
        """Persist a topic-segment as an episode row using the id that was
        already handed out as conversations.current_episode_id when the
        topic opened."""
        return self._insert(conversation_id, "conversation", content, episode_id=episode_id, importance=importance)

    def _insert(self, conversation_id, kind, content, task_id="", episode_id="", importance=5) -> str:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        eid = episode_id or uuid.uuid4().hex[:12]
        with get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO episodes
                  (id, conversation_id, kind, task_id, content, granularity,
                   importance, freshness, reference_count, linked_lesson_ids,
                   created_at, last_referenced_at)
                VALUES (?,?,?,?,?,'raw',?,1.0,0,'[]',?,?)
            """, (eid, conversation_id, kind, task_id, content, importance, now, now))
        return eid

    def reference(self, episode_id: str) -> None:
        """A structural link (a lesson cites this episode) or a usage hit
        (retrieved and actually used) — both count as a reference. Bumps
        importance; freshness is deliberately untouched (episodes only decay
        with time, see module docstring). The increment happens entirely in
        SQL (not read-then-write in Python) so concurrent references to the
        same popular episode (e.g. two conversations' memory_search calls
        both hitting it) can't lose an increment to a lost-update race."""
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            conn.execute("""
                UPDATE episodes
                SET importance=MIN(?, importance+1), reference_count=reference_count+1, last_referenced_at=?
                WHERE id=?
            """, (decay.MAX_IMPORTANCE, now, episode_id))

    def link_lesson(self, episode_id: str, lesson_id: str) -> None:
        with _lock_for(episode_id):
            with get_conn() as conn:
                row = conn.execute("SELECT linked_lesson_ids FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if not row:
                    return
                ids = json.loads(row["linked_lesson_ids"] or "[]")
                if lesson_id not in ids:
                    ids.append(lesson_id)
                conn.execute("UPDATE episodes SET linked_lesson_ids=? WHERE id=?",
                             (json.dumps(ids, ensure_ascii=False), episode_id))

    def get(self, episode_id: str) -> dict:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
            return self._row(row) if row else None

    def get_by_task_id(self, task_id: str) -> dict:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM episodes WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
            return self._row(row) if row else None

    def search(self, query: str = "", conversation_id: str = "", limit: int = 5) -> list:
        keywords = [w.strip() for w in query.split() if len(w.strip()) > 1][:5]
        clauses = []
        params = []
        if keywords:
            conditions = " OR ".join("content LIKE ?" for _ in keywords)
            clauses.append(f"({conditions})")
            params.extend(f"%{kw}%" for kw in keywords)
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with get_conn() as conn:
            rows = conn.execute(f"""
                SELECT * FROM episodes {where}
                ORDER BY importance DESC, created_at DESC LIMIT ?
            """, (*params, limit)).fetchall()
        return [self._row(r) for r in rows]

    def compress_due(self, llm) -> int:
        """Claim the lowest importance+freshness batch and downgrade each one
        granularity level (raw->summary->gist), or delete rows already at
        the coarsest level. Episodes' freshness decays from created_at
        (never refreshed by a reference — see module docstring). Returns how
        many rows were actually processed (downgraded or deleted)."""
        return decay.compress_due(llm, table="episodes", timestamp_column="created_at")

    def _row(self, r) -> dict:
        d = dict(r)
        d["linked_lesson_ids"] = json.loads(d.get("linked_lesson_ids") or "[]")
        return d
