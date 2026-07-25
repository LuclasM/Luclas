"""
memory/decay.py::compress_due() is shared by memory/episode_store.py
(table='episodes') and memory/store.py (table='memories') — both use the
same claim-via-conditional-UPDATE-checked-by-rowcount pattern that
memory/task_memory.py::maybe_compress() used before it was retired (see
tests/test_memory_concurrency.py's now-removed equivalent). Concurrent cron
compression ticks (or a compression tick overlapping a conversation-turn
eviction) must not double-compress or double-delete the same batch.
"""
import threading
import time
import uuid

from memory.database import get_conn
from memory.episode_store import EpisodeStore
import memory.decay as decay


def _seed_episodes(n=10):
    now = "2026-01-01 00:00:00"
    with get_conn() as conn:
        for i in range(n):
            conn.execute("""
                INSERT INTO episodes (id, conversation_id, kind, task_id, content, granularity,
                                       importance, freshness, reference_count, linked_lesson_ids,
                                       created_at, last_referenced_at)
                VALUES (?,?,?,?,?,'raw',?,1.0,0,'[]',?,?)
            """, (uuid.uuid4().hex[:12], "conv1", "conversation", "", f"episode content {i}", 5, now, now))


class _CountingLLM:
    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def chat(self, *a, **k):
        with self._lock:
            self.calls += 1
        time.sleep(0.3)  # widen the race window between concurrent callers
        return "a compressed summary"


def test_compress_due_no_double_processing_under_concurrency(isolated_db):
    _seed_episodes(10)
    llm = _CountingLLM()
    results = {}

    def worker(name):
        results[name] = EpisodeStore().compress_due(llm)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    time.sleep(0.05)  # let A get partway into its select+claim before B starts
    t2.start()
    t1.join()
    t2.join()

    assert llm.calls == 10, f"each of the 10 seeded rows should be summarized exactly once, got {llm.calls} LLM calls"

    with get_conn() as conn:
        rows = conn.execute("SELECT granularity FROM episodes").fetchall()
        stuck = [r["granularity"] for r in rows if r["granularity"].endswith(decay._CLAIM_SUFFIX)]
        assert not stuck, f"no row should be left stuck at a claimed-but-unprocessed granularity: {stuck}"
        assert all(r["granularity"] == "summary" for r in rows), "all 10 rows should have downgraded raw->summary exactly once"

    assert results["A"] + results["B"] == 10, (
        f"the two concurrent calls' processed counts must add up to exactly the 10 rows that existed, "
        f"got A={results['A']} B={results['B']}"
    )


def test_reference_no_lost_increments_under_concurrency(isolated_db):
    """reference() bumps importance via UPDATE ... SET importance=MIN(?,
    importance+1) entirely in SQL rather than SELECT-then-UPDATE in Python —
    a popular episode (memory_search hits from more than one conversation
    at once) must not lose increments to a lost-update race."""
    store = EpisodeStore()
    eid = store.create_task_episode("conv1", "task1", "some content", importance=1)

    n = 30

    def worker():
        store.reference(eid)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    row = store.get(eid)
    assert row["reference_count"] == n, f"expected {n} references recorded, got {row['reference_count']}"
    assert row["importance"] == decay.MAX_IMPORTANCE, (
        f"{n} references from importance=1 should saturate at MAX_IMPORTANCE={decay.MAX_IMPORTANCE}, "
        f"got {row['importance']} (a lost-update race would leave it short)"
    )
