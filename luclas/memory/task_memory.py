"""
memory/task_memory.py — 情景记忆（任务工作历史）

三层结构：
  active     最近 10 条，自动进入每次任务的 context
  archived   较旧记录，全量保留，可搜索按需拉入
  summarized 已被压缩，原记录不再单独展示

压缩规则：archived 超过 50 条时，把最旧的 20 条压缩成一段历史摘要。
"""

import datetime
import json
import re
import uuid

from memory.database import get_conn
import i18n as T


class TaskMemory:
    ACTIVE_KEEP      = 10
    ARCHIVE_THRESHOLD = 50
    COMPRESS_BATCH   = 20

    def save(self, record: dict) -> None:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tree = record.get("tree") or {}
        status = record.get("status") or tree.get("status") or "running"
        if status not in ("done", "failed"):
            status = "running"
        with get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO task_records
                  (id, session_id, goal, summary, artifacts, tree, importance, tier,
                   status, created_at, completed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                record.get("id") or uuid.uuid4().hex[:12],
                record.get("session_id", ""),
                record.get("goal", ""),
                record.get("summary", ""),
                json.dumps(record.get("artifacts", []), ensure_ascii=False),
                json.dumps(tree, ensure_ascii=False),
                record.get("importance", 7),
                record.get("tier", "active"),
                status,
                record.get("created_at", now),
                record.get("completed_at", now),
            ))

    def archive_old(self, keep: int = None) -> int:
        keep = keep or self.ACTIVE_KEEP
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, tree FROM task_records WHERE tier='active' ORDER BY completed_at DESC"
            ).fetchall()
            if len(rows) <= keep:
                return 0
            # 优先保留有子任务的复杂根任务，简单任务优先归档
            complex_ids = []
            simple_ids  = []
            for r in rows:
                tree = json.loads(r["tree"] or "{}")
                if tree.get("subtasks"):
                    complex_ids.append(r["id"])
                else:
                    simple_ids.append(r["id"])
            # 复杂任务先入 keep 池，超额的简单任务先归档
            priority = complex_ids + simple_ids
            to_keep  = set(priority[:keep])
            to_archive = [r["id"] for r in rows if r["id"] not in to_keep]
            if not to_archive:
                return 0
            conn.executemany(
                "UPDATE task_records SET tier='archived' WHERE id=?",
                [(rid,) for rid in to_archive]
            )
            return len(to_archive)

    def maybe_compress(self, llm) -> bool:
        with get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as n FROM task_records WHERE tier='archived'"
            ).fetchone()["n"]

        if count < self.ARCHIVE_THRESHOLD:
            return False

        # Select-then-claim in one connection/transaction: api.py runs one
        # TaskRunner per session, all sharing this same TaskMemory instance,
        # and every task's run() calls maybe_compress() on completion — two
        # sessions finishing close together would otherwise both select the
        # same oldest-20 batch, each run their own (slow) LLM summarization
        # call over it, and both write a task_summaries row covering
        # identical records, permanently duplicated (build_context() has no
        # dedup and surfaces both forever). The claiming UPDATE is
        # conditioned on tier still being 'archived', so whichever caller's
        # UPDATE commits first wins the batch outright — SQLite serializes
        # writers, so the second caller's UPDATE (against the same ids) is
        # guaranteed to see the first one's already-applied change and
        # affects zero rows, not a partial/inconsistent set.
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT id, goal, summary, artifacts, completed_at
                FROM task_records WHERE tier='archived'
                ORDER BY completed_at ASC LIMIT ?
            """, (self.COMPRESS_BATCH,)).fetchall()
            if not rows:
                return False
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            claimed = conn.execute(
                f"UPDATE task_records SET tier='compressing' WHERE tier='archived' AND id IN ({placeholders})",
                ids,
            ).rowcount

        if claimed < len(ids):
            # Another caller claimed this batch (fully or partially) first —
            # release anything we did manage to claim rather than compress
            # a batch we don't fully own, and let the next call pick a fresh
            # (now-different) batch.
            if claimed:
                with get_conn() as conn:
                    conn.executemany(
                        "UPDATE task_records SET tier='archived' WHERE id=? AND tier='compressing'",
                        [(rid,) for rid in ids],
                    )
            return False

        lines = []
        for r in rows:
            arts = json.loads(r["artifacts"] or "[]")
            art_str = ""
            if arts:
                descs = [a.get("desc") or a.get("path") or a.get("url") or a.get("content", "")[:40]
                         for a in arts[:3]]
                art_str = " | " + "、".join(d for d in descs if d)
            lines.append(f"[{r['completed_at'][:10]}] {r['goal']} → {r['summary']}{art_str}")

        prompt = f"""Below are {len(rows)} historical task records. Compress them into a concise summary (2-3 paragraphs, max ~500 words):

{chr(10).join(lines)}

Requirements:
- Keep key experience, important artifact paths, and system-access learnings
- Chronological order, earliest to latest
- Write as flowing paragraphs, not a list"""

        try:
            summary_text = llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
        except Exception:
            # Release the claim so this batch is retried on a later call
            # instead of stuck at tier='compressing' forever.
            with get_conn() as conn:
                conn.executemany(
                    "UPDATE task_records SET tier='archived' WHERE id=?",
                    [(rid,) for rid in ids],
                )
            return False

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record_ids = [r["id"] for r in rows]
        period_start = rows[0]["completed_at"][:10]
        period_end   = rows[-1]["completed_at"][:10]

        with get_conn() as conn:
            conn.execute("""
                INSERT INTO task_summaries (id, content, period_start, period_end, record_ids, created_at)
                VALUES (?,?,?,?,?,?)
            """, (
                uuid.uuid4().hex[:12],
                summary_text,
                period_start,
                period_end,
                json.dumps(record_ids),
                now,
            ))
            conn.executemany(
                "UPDATE task_records SET tier='summarized' WHERE id=?",
                [(rid,) for rid in record_ids]
            )

        return True

    def get_running(self) -> list[dict]:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT id, session_id, goal, summary, artifacts, tree, completed_at
                FROM task_records WHERE tier='running'
                ORDER BY completed_at DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def get_recent(self, n: int = 10) -> list[dict]:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT id, session_id, goal, summary, artifacts, completed_at
                FROM task_records WHERE tier='active'
                ORDER BY completed_at DESC LIMIT ?
            """, (n,)).fetchall()
        return [dict(r) for r in rows]

    def get_relevant(self, query: str, limit: int = 5) -> list[dict]:
        keywords = [w.strip() for w in query.split() if len(w.strip()) > 1][:5]
        if not keywords:
            return []
        conditions = " OR ".join(
            "goal LIKE ? OR summary LIKE ? OR artifacts LIKE ?" for _ in keywords
        )
        params = []
        for kw in keywords:
            params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])
        with get_conn() as conn:
            rows = conn.execute(f"""
                SELECT id, session_id, goal, summary, artifacts, completed_at
                FROM task_records
                WHERE tier != 'summarized' AND ({conditions})
                ORDER BY completed_at DESC LIMIT ?
            """, (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_summaries(self) -> list[dict]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, content, period_start, period_end FROM task_summaries ORDER BY period_start ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def build_context(self, current_goal: str = "") -> str:
        summaries = self.get_summaries()
        recent    = self.get_recent(self.ACTIVE_KEEP)

        recent_ids = {r["id"] for r in recent}
        relevant = []
        if current_goal:
            for r in self.get_relevant(current_goal, limit=5):
                if r["id"] not in recent_ids:
                    relevant.append(r)

        running = self.get_running()

        if not summaries and not recent and not relevant and not running:
            return ""

        parts = [T.work_history_header()]

        if running:
            parts.append(T.running_tasks_label())
            for r in running:
                tree = json.loads(r.get("tree") or "{}")
                lines = []
                _fmt_tree_node(tree, lines, 0)
                parts.append(T.goal_label(r['goal']))
                parts.extend(lines)

        if summaries:
            parts.append(T.summaries_label())
            for s in summaries:
                parts.append(f"（{s['period_start']} ~ {s['period_end']}）\n{s['content']}")

        if recent:
            parts.append(T.recent_tasks_label())
            for r in reversed(recent):
                parts.append(_fmt_record(r))

        if relevant:
            parts.append(T.relevant_history_label())
            for r in relevant:
                parts.append(_fmt_record(r))

        return "\n".join(parts)

    def get(self, record_id: str) -> dict | None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM task_records WHERE id=?", (record_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_all(self, limit: int = 20) -> list[dict]:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT id, session_id, goal, summary, artifacts, tree, tier, status, completed_at
                FROM task_records ORDER BY completed_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> dict:
        with get_conn() as conn:
            active    = conn.execute("SELECT COUNT(*) FROM task_records WHERE tier='active'").fetchone()[0]
            running   = conn.execute("SELECT COUNT(*) FROM task_records WHERE tier='running'").fetchone()[0]
            archived  = conn.execute("SELECT COUNT(*) FROM task_records WHERE tier='archived'").fetchone()[0]
            summarized= conn.execute("SELECT COUNT(*) FROM task_records WHERE tier='summarized'").fetchone()[0]
            sums      = conn.execute("SELECT COUNT(*) FROM task_summaries").fetchone()[0]
        return {"active": active, "running": running, "archived": archived,
                "summarized": summarized, "summaries": sums}


def _fmt_tree_node(node: dict, lines: list, depth: int) -> None:
    if not node:
        return
    icon = {"pending": "○", "running": "▶", "done": "✓", "failed": "✗"}.get(node.get("status", ""), "?")
    indent = "  " * depth
    result_str = f" → {node['result'][:50]}" if node.get("result") else ""
    lines.append(f"{indent}[{icon}] {node.get('goal', '')}{result_str}")
    for st in node.get("subtasks", []):
        _fmt_tree_node(st, lines, depth + 1)


def _tree_had_failure(node: dict) -> bool:
    """True if this node or any descendant ever ended in status='failed'."""
    if not node:
        return False
    if node.get("status") == "failed":
        return True
    return any(_tree_had_failure(st) for st in node.get("subtasks", []))


def _collect_failed_nodes(node: dict, out: list | None = None) -> list[dict]:
    """Flat list of every node (at any depth) whose status is 'failed'."""
    out = out if out is not None else []
    if not node:
        return out
    if node.get("status") == "failed":
        out.append(node)
    for st in node.get("subtasks", []):
        _collect_failed_nodes(st, out)
    return out


def _fmt_record(r: dict) -> str:
    arts = json.loads(r.get("artifacts") or "[]")
    art_str = ""
    if arts:
        descs = [a.get("desc") or a.get("path") or a.get("url") or a.get("content", "")[:50]
                 for a in arts[:3]]
        art_str = T.artifacts_label() + "、".join(d for d in descs if d)
    dt = (r.get("completed_at") or "")[:16]
    return f"[{dt}] {r['goal']} → {r['summary']}{art_str}"
