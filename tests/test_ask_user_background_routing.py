"""
A background-dispatched task (active_task_foreground=False) is supposed to
let a new incoming message start its own independent conversation turn —
that's the whole point of running it in the background. But the moment the
task is actually sitting in ask_user() waiting for an answer, the next
message on that same conversation IS the answer, not a new topic, and must
reach the task's wait_queue instead.

Before tools/user_input.py::has_pending_question() existed, api.py's /chat
only checked active_task_foreground to decide this, which is always False
for background dispatch — the question was pushed correctly, but the
user's reply was silently routed into a brand-new conversation turn instead
of the waiting queue, so ask_user() always burned the full
ASK_USER_TIMEOUT_SECONDS before giving up regardless of how fast the user
actually answered. Confirmed via a real WeChat test session.
"""
import queue
import threading
import time

from memory.store import MemoryStore
from tools.user_input import ask_user, set_channel_context, clear_channel_context, has_pending_question
import api


def test_background_task_ask_user_receives_the_answer(isolated_db, monkeypatch):
    monkeypatch.setattr(api, "_store", MemoryStore())

    conversation_id = "wecom_TESTUSER"
    api._conversations.get_or_create(conversation_id)

    q = queue.Queue()
    api._session_queues[conversation_id] = q
    api._session_tasks[conversation_id] = "task-1"
    api._results["task-1"] = {"status": "running", "result": "", "started_at": "", "finished_at": ""}
    api._conversations.set_active_task(conversation_id, "task-1", False)  # background, not foreground

    pushed = []
    answer_holder = {}

    def task_thread():
        set_channel_context(push=pushed.append, wait_queue=q, session_id=conversation_id)
        try:
            answer_holder["answer"] = ask_user("你要选哪个方案？A还是B？")
        finally:
            clear_channel_context()

    t = threading.Thread(target=task_thread)
    t.start()
    try:
        for _ in range(50):
            if pushed:
                break
            time.sleep(0.02)

        assert pushed == ["❓ 你要选哪个方案？A还是B？"], "the question must still be pushed to the channel"
        assert has_pending_question(conversation_id), (
            "ask_user() must mark this session as having a pending question while blocked, "
            "so /chat's routing can tell a background task apart from a genuinely free conversation"
        )

        # Mirror api.py:/chat's actual routing decision.
        conv = api._conversations.get_or_create(conversation_id)
        should_route_to_supplement = conv["active_task_id"] and (
            conv["active_task_foreground"] or has_pending_question(conversation_id)
        )
        assert should_route_to_supplement, (
            "a reply to a background task's pending question must be routed into its wait_queue, "
            "not diverted into a fresh conversation turn"
        )

        q.put("方案A")
        t.join(timeout=5)
        assert answer_holder.get("answer") == "方案A"
        assert not has_pending_question(conversation_id), "pending flag must clear once answered"
    finally:
        t.join(timeout=1)
