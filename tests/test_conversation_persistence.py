"""
The whole point of the persistent-conversation redesign (replacing the old
per-message task_records/build_context model) is that a conversation
survives process restarts — "断线，重启，不会打断" was the explicit
requirement. ConversationStore holds no in-memory state of its own (every
method reads/writes straight through to the DB), so a "restart" is just
constructing a fresh instance — this test exists to pin that down as an
actual guarantee, not just an accident of the current implementation.
"""
from memory.conversation_store import ConversationStore
from memory.episode_store import EpisodeStore


def test_conversation_state_survives_a_simulated_process_restart(isolated_db):
    cs1 = ConversationStore()
    es1 = EpisodeStore()

    cs1.append_message("wecom_U1", "user", "hello")
    cs1.append_message("wecom_U1", "assistant", "hi there")
    cs1.set_active_task("wecom_U1", "task-123", True)

    conv_before = cs1.get_or_create("wecom_U1")
    closed_id = cs1.close_topic("wecom_U1", es1, importance=6)

    # Simulate the process dying and restarting: fresh instances, no shared
    # in-memory state — only the DB (which "restart" doesn't touch) carries over.
    cs2 = ConversationStore()
    es2 = EpisodeStore()

    conv_after = cs2.get_or_create("wecom_U1")
    assert conv_after["messages"] == conv_before["messages"] or len(conv_after["messages"]) == 2
    assert conv_after["active_task_id"] == "task-123"
    assert conv_after["active_task_foreground"] is True
    assert conv_after["current_episode_id"] != closed_id, (
        "the closed topic's id must not still be the live current_episode_id after restart"
    )

    episode = es2.get(closed_id)
    assert episode is not None, "the episode closed before the 'restart' must still be readable after it"
    assert "hello" in episode["content"] and "hi there" in episode["content"]

    # Conversation continues exactly where it left off: appending now must
    # land in the *new* topic-segment opened before the restart, not the
    # closed one, and the closed episode's content must be untouched.
    cs2.append_message("wecom_U1", "user", "continuing after restart")
    msgs = cs2.get_messages("wecom_U1")
    assert msgs[-1]["content"] == "continuing after restart"
    assert msgs[-1]["episode_id"] == conv_after["current_episode_id"]
    assert es2.get(closed_id)["content"] == episode["content"]
