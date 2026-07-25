"""Shared importance/freshness/compression logic for episodes ("经历",
memory/episode_store.py) and lessons ("经验", the memories table in
memory/store.py). Both share the same compression algorithm — lowest
importance+freshness compressed first, granularity downgraded one level at
a time, deleted once already at the coarsest level — they differ only in
which timestamp column freshness decays from and whether a reference
refreshes it, which is handled by the callers, not here.
"""
from datetime import datetime
from typing import Optional

from memory.database import get_conn

HALF_LIFE_DAYS = 30.0
MAX_IMPORTANCE = 10

GRANULARITY_ORDER = ["raw", "summary", "gist"]
COMPRESS_BATCH = 10
_CLAIM_SUFFIX = "__claiming"


def bump_importance(importance: int) -> int:
    return min(MAX_IMPORTANCE, (importance or 0) + 1)


def compute_freshness(base_freshness: float, last_touch_at: str, now: Optional[datetime] = None) -> float:
    """Lazily-decayed freshness, computed on read rather than kept fresh by a
    background job: base_freshness * 0.5 ** (days_elapsed / HALF_LIFE_DAYS).
    """
    base = base_freshness if base_freshness is not None else 1.0
    if not last_touch_at:
        return base
    now = now or datetime.now()
    try:
        touched = datetime.strptime(last_touch_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return base
    days_elapsed = max(0.0, (now - touched).total_seconds() / 86400.0)
    return base * (0.5 ** (days_elapsed / HALF_LIFE_DAYS))


def next_granularity(current: str) -> Optional[str]:
    """Next coarser granularity, or None if `current` is already the
    coarsest — callers should delete the row instead of downgrading it."""
    try:
        idx = GRANULARITY_ORDER.index(current or "raw")
    except ValueError:
        idx = 0
    if idx + 1 >= len(GRANULARITY_ORDER):
        return None
    return GRANULARITY_ORDER[idx + 1]


def rank_key(importance: int, freshness: float) -> float:
    """Lower = more compressible first. Importance (1-10) and freshness
    (0.0-1.0) are weighted onto a comparable scale and summed."""
    return (importance or 0) + (freshness or 0) * 10


def compress_due(llm, table: str, timestamp_column: str, batch_size: int = COMPRESS_BATCH) -> int:
    """Generic claim-then-compress pass shared by memory/episode_store.py
    (table='episodes') and memory/store.py (table='memories') — both tables
    share the same id/granularity/importance/freshness/content shape needed
    here, differing only in which column freshness decays from
    (created_at for episodes, which never refresh on reference; updated_at
    for lessons, which do — see each store's module docstring).

    Claim-then-process mirrors memory/task_memory.py's now-retired
    maybe_compress(): candidates are claimed via a conditional UPDATE
    checked by rowcount before any LLM call is spent on them, so two
    concurrent callers can't double-compress (or double-delete) the same
    batch; a partial/failed claim releases back to the original granularity.
    """
    pool = batch_size * 5
    with get_conn() as conn:
        candidates = conn.execute(f"""
            SELECT id, granularity, importance, freshness, {timestamp_column} as touched_at
            FROM {table}
            WHERE granularity IN ('raw','summary','gist')
            ORDER BY importance ASC, freshness ASC LIMIT ?
        """, (pool,)).fetchall()
    if not candidates:
        return 0

    now = datetime.now()
    scored = sorted(
        candidates,
        key=lambda r: rank_key(r["importance"], compute_freshness(r["freshness"], r["touched_at"], now)),
    )[:batch_size]

    by_granularity = {}
    for r in scored:
        by_granularity.setdefault(r["granularity"], []).append(r["id"])

    claimed_ids = []
    with get_conn() as conn:
        for gran, ids in by_granularity.items():
            placeholders = ",".join("?" * len(ids))
            claimed = conn.execute(
                f"UPDATE {table} SET granularity=? WHERE granularity=? AND id IN ({placeholders})",
                (gran + _CLAIM_SUFFIX, gran, *ids),
            ).rowcount
            if claimed == len(ids):
                claimed_ids.extend((rid, gran) for rid in ids)
            elif claimed:
                # Partial claim within this granularity group — release what
                # we did grab, a later call will retry with a fresh candidate set.
                with get_conn() as inner:
                    inner.executemany(
                        f"UPDATE {table} SET granularity=? WHERE id=? AND granularity=?",
                        [(gran, rid, gran + _CLAIM_SUFFIX) for rid in ids],
                    )

    if not claimed_ids:
        return 0

    processed = 0
    for item_id, orig_gran in claimed_ids:
        target = next_granularity(orig_gran)
        if target is None:
            with get_conn() as conn:
                conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
            processed += 1
            continue
        with get_conn() as conn:
            row = conn.execute(f"SELECT content FROM {table} WHERE id=?", (item_id,)).fetchone()
        content = row["content"] if row else ""
        try:
            summary = llm.chat([{
                "role": "user",
                "content": f"将下面这段内容压缩成更简短的版本（保留关键结论和线索，去掉细节），"
                           f"目标是从「{orig_gran}」颗粒度压到「{target}」颗粒度：\n\n{content}",
            }], temperature=0.2)
        except Exception:
            with get_conn() as conn:
                conn.execute(f"UPDATE {table} SET granularity=? WHERE id=?", (orig_gran, item_id))
            continue
        with get_conn() as conn:
            conn.execute(f"UPDATE {table} SET granularity=?, content=? WHERE id=?",
                         (target, summary, item_id))
        processed += 1

    return processed
