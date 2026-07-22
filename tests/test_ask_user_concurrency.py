"""
ask_user()'s lock used to be a single process-wide threading.Lock, so one
messaging session's pending question could block an entirely unrelated
session's ask_user() call for up to ASK_USER_TIMEOUT_SECONDS — api.py serves
many independent sessions concurrently, so this is a real cross-tenant
blocking bug, not a hypothetical. The lock is now scoped per session_id.
"""
import queue
import threading
import time

from tools import user_input as ui


def test_unrelated_session_not_blocked_by_another_sessions_pending_question():
    results = {}

    def slow_session():
        # session A's ask_user() blocks waiting for an answer that never
        # comes (an empty queue with a short timeout so the test stays fast).
        ui.set_channel_context(push=lambda m: None, wait_queue=queue.Queue(), session_id="sessA")
        t0 = time.time()
        ui.ask_user("question from A")
        results["A_elapsed"] = time.time() - t0
        ui.clear_channel_context()

    def fast_session():
        time.sleep(0.1)  # let A grab its lock and start waiting first
        q = queue.Queue()
        q.put("reply from B")
        ui.set_channel_context(push=lambda m: None, wait_queue=q, session_id="sessB")
        t0 = time.time()
        answer = ui.ask_user("question from B")
        results["B_elapsed"] = time.time() - t0
        results["B_answer"] = answer
        ui.clear_channel_context()

    # Keep the test fast: shrink A's timeout so the thread doesn't linger,
    # without weakening what's actually being asserted (B's independence).
    orig_timeout = ui.ASK_USER_TIMEOUT_SECONDS
    ui.ASK_USER_TIMEOUT_SECONDS = 1
    try:
        tA = threading.Thread(target=slow_session, daemon=True)
        tB = threading.Thread(target=fast_session)
        tA.start()
        tB.start()
        tB.join(timeout=3)

        assert "B_elapsed" in results, "session B's ask_user() did not return in time — it was blocked"
        assert results["B_elapsed"] < 0.5, (
            f"an unrelated session's ask_user() must not be blocked behind another session's "
            f"pending question, took {results['B_elapsed']:.2f}s"
        )
        assert results["B_answer"] == "reply from B"
    finally:
        ui.ASK_USER_TIMEOUT_SECONDS = orig_timeout
        tA.join(timeout=2)
