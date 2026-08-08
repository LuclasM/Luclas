import json

import conversation_runner
from memory.conversation_store import ConversationStore
from memory.episode_store import EpisodeStore
from memory.store import MemoryStore


class _LLM:
    context_length = 100_000

    def agent_turn(self, messages, tools):
        return {
            "content": "new subject reply\n<topic>new</topic>",
            "tool_calls": [],
            "finish_reason": "stop",
        }

    def chat(self, messages, **kwargs):
        return "compressed history"


def test_semantic_new_boundary_moves_current_turn_to_new_episode(isolated_db):
    conv = ConversationStore()
    episodes = EpisodeStore()
    store = MemoryStore()
    conv.append_message("c1", "user", "old subject")
    conv.append_message("c1", "assistant", "old answer")
    old_id = conv.get_or_create("c1")["current_episode_id"]

    reply, delivered = conversation_runner.handle_turn(
        "c1", "c1", "start a different subject", _LLM(), store, episodes,
        conv, lambda *args: {}, needs_identity_check=False,
    )

    assert reply == "new subject reply"
    assert delivered is False
    archived = episodes.get(old_id)
    assert "old subject" in archived["content"]
    assert "start a different subject" not in archived["content"]
    state = conv.get_or_create("c1")
    live = [m for m in state["messages"] if m["episode_id"] == state["current_episode_id"]]
    assert [m["content"] for m in live] == ["start a different subject", "new subject reply"]


def test_context_pressure_rolls_current_topic_and_compresses_it(isolated_db):
    conv = ConversationStore()
    episodes = EpisodeStore()
    llm = _LLM()
    # 400 ASCII chars ~= 100 estimated tokens; with context_length=100 this
    # is safely beyond the new 50% ceiling.
    conv.append_message("c1", "user", "x" * 400)
    old_id = conv.get_or_create("c1")["current_episode_id"]

    result = conv.compress_if_over_threshold("c1", episodes, llm, context_length=100)

    assert result["triggered"] is True
    assert result["processed"] == 1
    assert conv.get_or_create("c1")["current_episode_id"] != old_id
    episode = episodes.get(old_id)
    assert episode["granularity"] == "summary"
    assert episode["content"] == "compressed history"
    messages = conv.get_messages("c1")
    assert len(messages) == 1
    assert messages[0]["hidden"] is True
    assert messages[0]["granularity"] == "summary"


def test_lower_importance_episode_is_compressed_first(isolated_db):
    conv = ConversationStore()
    episodes = EpisodeStore()
    llm = _LLM()

    conv.append_message("c1", "user", "a" * 240)
    low_id = conv.close_topic("c1", episodes, importance=2)
    conv.append_message("c1", "user", "b" * 240)
    high_id = conv.close_topic("c1", episodes, importance=9)

    result = conv.compress_if_over_threshold("c1", episodes, llm, context_length=200)

    assert result["triggered"] is True
    assert episodes.get(low_id)["granularity"] == "summary"
    assert episodes.get(high_id)["granularity"] == "raw"


def test_task_episode_in_live_context_participates_in_pressure_compression(isolated_db):
    conv = ConversationStore()
    episodes = EpisodeStore()
    llm = _LLM()
    eid = episodes.create_task_episode("c1", "task1", "task result " + "x" * 400,
                                       importance=7)
    conv.append_message("c1", "assistant", "task result " + "x" * 400,
                        episode_id=eid)

    result = conv.compress_if_over_threshold("c1", episodes, llm, context_length=100)

    assert result["triggered"] is True
    assert episodes.get(eid)["granularity"] == "summary"
    assert conv.get_messages("c1")[0]["episode_id"] == eid


def test_manual_new_topic_closes_current_segment(isolated_db, capsys):
    from luclas import _handle_slash

    conv = ConversationStore()
    episodes = EpisodeStore()
    conv.append_message("c1", "user", "close this manually")
    old_id = conv.get_or_create("c1")["current_episode_id"]

    _handle_slash(
        "/new topic", llm=None, store=MemoryStore(), schemas=[], fns={},
        conversation_id="c1", conv_store=conv, episode_store=episodes,
    )

    assert episodes.get(old_id) is not None
    assert conv.get_or_create("c1")["current_episode_id"] != old_id
    assert "New topic started" in capsys.readouterr().out
