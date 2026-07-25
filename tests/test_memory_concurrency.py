"""
api.py runs one TaskRunner per messaging session, all sharing the same
UpgradeEvaluator-creation pattern (a fresh UpgradeEvaluator per task, all
touching the same on-disk history file). This test exercises the race found
there: the upgrade-assessment LLM call blocking unrelated sessions'
bookkeeping.

(The other race this file used to cover — TaskMemory.maybe_compress()
double-summarizing an archived batch — was retired along with task_records/
TaskMemory itself; its replacement, the same claim-before-LLM-call pattern
now shared by episodes and lessons, is covered by
tests/test_episode_lesson_concurrency.py.)
"""
import threading
import time


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
    setup_ev = ue.UpgradeEvaluator(SlowLLM(), None)
    setup_ev.evaluate_after_task("g1", fail_text)
    setup_ev.evaluate_after_task("g2", fail_text)

    results = {}

    def slow_session():
        ev = ue.UpgradeEvaluator(SlowLLM(), None)
        t0 = time.time()
        ev.evaluate_after_task("g3-triggers-assessment", fail_text)  # 3rd failure -> 1s LLM call
        results["slow_session_total"] = time.time() - t0

    def unrelated_session():
        time.sleep(0.1)  # ensure the slow session has already grabbed the lock
        ev = ue.UpgradeEvaluator(SlowLLM(), None)
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
