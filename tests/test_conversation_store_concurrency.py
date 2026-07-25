"""
memory/conversation_store.py's append_message()/close_topic()/
evict_if_over_threshold() are all read-modify-write on the same
conversations.messages JSON blob (read the whole row, mutate in Python,
write the whole row back) — SQLite's writer serialization doesn't protect
against that, since two threads can each successfully SELECT before either
UPDATEs. This isn't a rare edge case here: it's the normal shape of
background dispatch_task usage — the conversation is designed to keep
going (new messages, more turns) while a background task runs, and that
task's own completion handler (api.py's _bg()/luclas.py's
_make_cli_dispatch's _run()) appends its result from a separate thread at
whatever moment the task actually finishes. Without a lock, one of the two
concurrent appends is silently dropped — confirmed by reproducing the exact
pre-fix read-modify-write shape directly: 50 concurrent appends without the
lock lost 48 of them in one run.
"""
import threading

from memory.conversation_store import ConversationStore


def test_append_message_no_lost_updates_under_concurrency(isolated_db):
    cs = ConversationStore()
    cs.get_or_create("conv1")

    n = 50

    def worker(i):
        cs.append_message("conv1", "user", f"msg-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    msgs = cs.get_messages("conv1")
    assert len(msgs) == n, (
        f"lost-update race: {n} concurrent append_message() calls should all survive, "
        f"only {len(msgs)} did"
    )
    contents = {m["content"] for m in msgs}
    assert contents == {f"msg-{i}" for i in range(n)}, "every message's content must be intact, not just the count"
