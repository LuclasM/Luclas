import json
import uuid
from memory.database import get_conn


class MemoryStore:

    def write(self, content: str, type: str = "", tags: list = None,
              importance: int = 5) -> str:
        mid = uuid.uuid4().hex[:12]
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        try:
            from memory.embedder import encode
            emb = encode(content)
        except Exception:
            emb = None
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO memories (id,content,type,tags,importance,embedding) VALUES (?,?,?,?,?,?)",
                (mid, content, type, tags_json, importance, emb)
            )
        return mid

    def search(self, query: str = "", type: str = "", tags: list = None,
               min_importance: int = 0, limit: int = 20) -> list:
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

        if rows:
            ids = [r[0] for r in rows]
            placeholders = ",".join("?" * len(ids))
            with get_conn() as conn:
                conn.execute(
                    f"UPDATE memories SET access_count = access_count + 1 WHERE id IN ({placeholders})",
                    ids
                )

        return [self._row(r) for r in rows]

    def get(self, mid: str) -> dict | None:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            return self._row(row) if row else None

    def update(self, mid: str, content: str = None, type: str = None,
               tags: list = None, importance: int = None) -> bool:
        fields, params = [], []
        if content   is not None:
            fields.append("content=?")
            params.append(content)
            try:
                from memory.embedder import encode
                fields.append("embedding=?")
                params.append(encode(content))
            except Exception:
                pass
        if type      is not None: fields.append("type=?");       params.append(type)
        if tags      is not None: fields.append("tags=?");       params.append(json.dumps(tags, ensure_ascii=False))
        if importance is not None: fields.append("importance=?"); params.append(importance)
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
        return d


class TaskStore:

    def save(self, task: dict) -> None:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO tasks (id,goal,status,log,result)
                VALUES (:id,:goal,:status,:log,:result)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    log=excluded.log,
                    result=excluded.result,
                    updated_at=datetime('now','localtime')
            """, task)

    def load(self, tid: str) -> dict | None:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            return dict(row) if row else None

    def list_recent(self, limit: int = 20) -> list:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_active(self) -> list:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='active' ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
