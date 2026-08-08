import json
import threading
import uuid
from memory.database import get_conn
import memory.decay as decay

# "经验"（lessons）: search only discovers candidates. Freshness and
# importance are refreshed when get_for_use() actually loads a selected lesson
# into working context. Lessons are durable retrieval records, not resident
# conversation context, so they are deliberately not summarized or deleted by
# background compression.

# link_episode() is a read-modify-write on linked_episode_ids (read the JSON
# array, append in Python, write the whole array back) — same lost-update
# race class as memory/conversation_store.py's messages blob, just lower
# frequency (once per AAR-linked task). A per-lesson-id lock is cheap enough
# to just always take.
_locks_guard = threading.Lock()
_lesson_locks: dict = {}


def _lock_for(mid: str) -> threading.Lock:
    with _locks_guard:
        lock = _lesson_locks.get(mid)
        if lock is None:
            lock = threading.Lock()
            _lesson_locks[mid] = lock
        return lock


class MemoryStore:

    def write(self, content: str, type: str = "", tags: list = None,
              importance: int = 5, source: str = "", credibility: int = 0,
              linked_episode_ids: list = None) -> str:
        mid = uuid.uuid4().hex[:12]
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        linked_json = json.dumps(linked_episode_ids or [], ensure_ascii=False)
        try:
            from memory.embedder import encode
            emb = encode(content)
        except Exception:
            emb = None
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO memories (id,content,type,tags,importance,source,credibility,embedding,linked_episode_ids) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, content, type, tags_json, importance, source, credibility, emb, linked_json)
            )
        return mid

    def search(self, query: str = "", type: str = "", tags: list = None,
               min_importance: int = 0, source: str = "", min_credibility: int = 0,
               limit: int = 20) -> list:
        conds, params = [], []
        if type:
            conds.append("type=?")
            params.append(type)
        if tags:
            for tag in tags:
                conds.append("tags LIKE ?")
                params.append(f'%"{tag}"%')
        if min_importance > 0:
            conds.append("importance >= ?")
            params.append(min_importance)
        if source:
            conds.append("source=?")
            params.append(source)
        if min_credibility > 0:
            conds.append("credibility >= ?")
            params.append(min_credibility)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""

        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM memories {where} "
                f"ORDER BY importance DESC, access_count DESC, created_at DESC",
                params
            ).fetchall()

        if query and rows:
            try:
                from memory.embedder import encode, cosine
                q_emb = encode(query)
                q_words = set(query.lower().split())

                def _score(r):
                    if r["embedding"]:
                        return cosine(q_emb, r["embedding"])
                    # 没有 embedding 的旧记忆：用关键词匹配兜底
                    text = (r["content"] + " " + (r["tags"] or "")).lower()
                    hits = sum(1 for w in q_words if w in text)
                    return 0.3 * hits / max(len(q_words), 1)

                rows = sorted(rows, key=_score, reverse=True)
            except Exception:
                # embedding 失败时退回关键词过滤
                q_words = query.lower().split()
                rows = [r for r in rows
                        if any(w in (r["content"] or "").lower() for w in q_words)]

        rows = list(rows[:limit])

        return [self._row(r) for r in rows]

    def link_episode(self, mid: str, episode_id: str) -> None:
        with _lock_for(mid):
            with get_conn() as conn:
                row = conn.execute("SELECT linked_episode_ids FROM memories WHERE id=?", (mid,)).fetchone()
                if not row:
                    return
                ids = json.loads(row["linked_episode_ids"] or "[]")
                if episode_id not in ids:
                    ids.append(episode_id)
                conn.execute("UPDATE memories SET linked_episode_ids=? WHERE id=?",
                             (json.dumps(ids, ensure_ascii=False), mid))

    def get(self, mid: str) -> dict | None:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            return self._row(row) if row else None

    def get_for_use(self, mid: str) -> dict | None:
        """Load one selected lesson and record an actual use.

        Candidate discovery via search() deliberately does not call this. The
        increment is a single SQL expression so concurrent users cannot lose
        importance/access-count updates.
        """
        with get_conn() as conn:
            changed = conn.execute(
                "UPDATE memories SET access_count = access_count + 1, "
                "importance = MIN(?, importance + 1), freshness = 1.0, "
                "updated_at = datetime('now','localtime') WHERE id=?",
                (decay.MAX_IMPORTANCE, mid),
            ).rowcount
            if not changed:
                return None
            row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            return self._row(row) if row else None

    def update(self, mid: str, content: str = None, type: str = None,
               tags: list = None, importance: int = None,
               source: str = None, credibility: int = None) -> bool:
        fields, params = [], []
        if content   is not None:
            fields.append("content=?")
            params.append(content)
            try:
                from memory.embedder import encode
                emb = encode(content)
            except Exception:
                emb = None
            if emb is not None:
                fields.append("embedding=?")
                params.append(emb)
        if type      is not None: fields.append("type=?");       params.append(type)
        if tags      is not None: fields.append("tags=?");       params.append(json.dumps(tags, ensure_ascii=False))
        if importance is not None: fields.append("importance=?"); params.append(importance)
        if source    is not None: fields.append("source=?");      params.append(source)
        if credibility is not None: fields.append("credibility=?"); params.append(credibility)
        if not fields:
            return False
        fields.append("updated_at=datetime('now','localtime')")
        params.append(mid)
        with get_conn() as conn:
            n = conn.execute(
                f"UPDATE memories SET {', '.join(fields)} WHERE id=?", params
            ).rowcount
        return n > 0

    def delete(self, mid: str) -> bool:
        with get_conn() as conn:
            n = conn.execute("DELETE FROM memories WHERE id=?", (mid,)).rowcount
        return n > 0

    def migrate_embeddings(self) -> int:
        """为没有 embedding 的旧记忆批量计算向量，返回处理条数。"""
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT rowid, content FROM memories WHERE embedding IS NULL"
            ).fetchall()
        if not rows:
            return 0
        from memory.embedder import encode_batch
        rowids = [r["rowid"] for r in rows]
        embs = encode_batch([r["content"] for r in rows])
        with get_conn() as conn:
            for rowid, emb in zip(rowids, embs):
                conn.execute("UPDATE memories SET embedding=? WHERE rowid=?", (emb, rowid))
        return len(rowids)

    def count(self) -> int:
        with get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def get_all(self, limit: int = 50) -> list:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY importance DESC, created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [self._row(r) for r in rows]

    def _row(self, r) -> dict:
        d = dict(r)
        d.pop("embedding", None)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        try:
            d["linked_episode_ids"] = json.loads(d.get("linked_episode_ids") or "[]")
        except Exception:
            d["linked_episode_ids"] = []
        return d
