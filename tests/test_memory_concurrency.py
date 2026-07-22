"""
api.py runs one TaskRunner per messaging session, all sharing the same
TaskMemory instance and the same UpgradeEvaluator-creation pattern (a fresh
UpgradeEvaluator per task, all touching the same on-disk history file).
These tests exercise the two races found there: maybe_compress() double-
summarizing an archived batch, and the upgrade-assessment LLM call blocking
unrelated sessions' bookkeeping.
"""
import json
import threading
import time
import uuid

from memory.database import get_conn
from memory.task_memory import TaskMemory


def _seed_archived_records(n=25):
    with get_conn() as conn:
        for i in range(n):
            conn.execute("""
                INSERT INTO task_records (id, session_id, goal, summary, artifacts, tree,
                                           importance, tier, status, created_at, completed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                uuid.uuid4().hex[:12], "s1", f"goal {i}", f"summary {i}", "[]", "{}",
                7, "archived", "done",
                f"2026-01-{i + 1:02d} 00:00:00", f"2026-01-{i + 1:02d} 00:00:00",
            ))


class _SlowLLM:
    def chat(self, *a, **k):
        time.sleep(0.3)  # widen the race window between concurrent callers
        return "a compressed summary"


def test_maybe_compress_no_duplicate_summaries_under_concurrency(isolated_db):
    _seed_archived_records(25)

    results = {}

    def worker(name):
        tm = TaskMemory()
        tm.ARCHIVE_THRESHOLD = 20
        tm.COMPRESS_BATCH = 20
        results[name] = tm.maybe_compress(_SlowLLM())

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    time.sleep(0.05)  # let A get partway into its select+claim before B starts
    t2.start()
    t1.join()
    t2.join()

    with get_conn() as conn:
        summaries = conn.execute("SELECT * FROM task_summaries").fetchall()
        seen_ids = set()
        for s in summaries:
            ids = set(json.loads(s["record_ids"]))
            overlap = seen_ids & ids
            assert not overlap, f"two summaries covering the same records: {overlap}"
            seen_ids |= ids

        stuck = conn.execute(
            "SELECT COUNT(*) as n FROM task_records WHERE tier='compressing'"
        ).fetchone()["n"]
        assert stuck == 0, "no row should be permanently stuck at the intermediate 'compressing' tier"

    assert len(summaries) == 1, (
        f"two concurrent maybe_compress() calls over the same batch must produce exactly one "
        f"summary (whichever claims the batch first), got {len(summaries)}"
    )


def test_upgrade_eval_lock_does_not_block_unrelated_session_during_llm_call(isolated_db, monkeypatch):
    import os
    import loops._upgrade_eval as ue
    import i18n as T

    # _UPGRADE_TRIGGER_FILE is derived from DATA_DIR at import time (captured
    # by value, not read dynamically) — point it at the isolated temp dir too.
    monkeypatch.setattr(ue, "_UPGRADE_TRIGGER_FILE", os.path.join(isolated_db["tmpdir"], "upgrade_trigger.json"))

    class SlowLLM:
        def chat(self, *a, **k):
            time.sleep(1.0)
            return '{"upgrade_needed": false, "common_cause": "test"}'

    fail_text = T.sentinel_exec_error("boom")

    # Prime two prior failures so a third failure crosses UPGRADE_THRESHOLD.
    setup_ev = ue.UpgradeEvaluator(SlowLLM(), None, None)
    setup_ev.evaluate_after_task("g1", fail_text)
    setup_ev.evaluate_after_task("g2", fail_text)

    results = {}

    def slow_session():
        ev = ue.UpgradeEvaluator(SlowLLM(), None, None)
        t0 = time.time()
        ev.evaluate_after_task("g3-triggers-assessment", fail_text)  # 3rd failure -> 1s LLM call
        results["slow_session_total"] = time.time() - t0

    def unrelated_session():
        time.sleep(0.1)  # ensure the slow session has already grabbed the lock
        ev = ue.UpgradeEvaluator(SlowLLM(), None, None)
        t0 = time.time()
        ev.evaluate_after_task("unrelated-goal", "done: fine")
        results["unrelated_session_wait"] = time.time() - t0

    t1 = threading.Thread(target=slow_session)
    t2 = threading.Thread(target=unrelated_session)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["slow_session_total"] >= 0.9, "sanity check: the assessment LLM call actually ran"
    assert results["unrelated_session_wait"] < 0.5, (
        f"an unrelated session's own bookkeeping must not be blocked behind another "
        f"session's slow upgrade-assessment LLM call, took {results['unrelated_session_wait']:.2f}s"
    )
