"""
CLI background dispatch_task never called set_channel_context(), so its
ask_user() fell through to tools/user_input.py's plain-tty branch and
called input() directly — on the background thread, while main()'s own
REPL loop is simultaneously blocked in its own input("LUC > ") call on the
exact same stdin. Two threads reading one fd is an actual race (unlike the
messaging-channel version of this bug, which just silently misrouted the
reply) — confirmed by tracking which thread input() got called from before
this fix.

luclas.py::_make_cli_dispatch now wires a real push+wait_queue for the
background path only (foreground still safely uses direct input(), since
it runs synchronously on the main thread with nothing else reading stdin)
and registers the queue in _cli_pending_queues so main()'s REPL loop can
route the next typed line there instead of starting a new conversation turn.
"""
import builtins
import sys
import threading
import time

from memory.conversation_store import ConversationStore
from memory.episode_store import EpisodeStore
from memory.store import MemoryStore
from llm_client import LLMClient
from tools.user_input import ask_user, has_pending_question
import luclas


def test_cli_background_ask_user_does_not_call_input_and_receives_routed_answer(isolated_db, monkeypatch):
    class _FakeRunner:
        def __init__(self, *a, **kw):
            pass

        def run(self, goal, on_result=None):
            answer = ask_user("要不要继续？A还是B？")
            result = f"用户选择了：{answer}"
            if on_result:
                on_result(result)
            return result

    monkeypatch.setattr(luclas, "TaskRunner", _FakeRunner)

    conv_store = ConversationStore()
    episode_store = EpisodeStore()
    store = MemoryStore()
    llm = LLMClient()
    dispatch = luclas._make_cli_dispatch(conv_store, episode_store, llm, store, [], {})

    input_calls = []

    def tracking_input(prompt=""):
        input_calls.append(threading.current_thread().name)
        return "SHOULD NOT HAPPEN"

    monkeypatch.setattr(builtins, "input", tracking_input)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    dispatch("cli_local", "问用户一个问题", False)  # background

    for _ in range(100):
        if has_pending_question("cli_local"):
            break
        time.sleep(0.02)

    assert not input_calls, f"background ask_user() must not call input() — stdin race with the main REPL loop: {input_calls}"
    assert has_pending_question("cli_local")
    assert "cli_local" in luclas._cli_pending_queues, "queue must be registered for main()'s REPL loop to route into"

    # Mirror main()'s own routing check.
    pending_q = luclas._cli_pending_queues.get("cli_local")
    assert pending_q is not None and has_pending_question("cli_local")
    pending_q.put("方案B")

    for _ in range(100):
        if not has_pending_question("cli_local"):
            break
        time.sleep(0.02)

    msgs = conv_store.get_messages("cli_local")
    assert any("用户选择了：方案B" in m["content"] for m in msgs), (
        "the routed answer must reach the background task and end up recorded in conversation history"
    )
