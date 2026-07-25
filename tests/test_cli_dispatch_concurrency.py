"""
luclas.py's interactive REPL keeps one shared TaskRunner/LLMClient alive for
the whole session (unlike api.py, which already builds a fresh LLMClient per
HTTP request — see api.py:_run_task's own comment on this). Once the
conversation layer's dispatch_task can fire a background task while the main
thread keeps chatting (see luclas.py::_make_cli_dispatch), reusing that one
shared LLMClient from a background dispatch thread would hit the exact
_model_queue/_current_idx race LLMClient.clone() exists to prevent elsewhere
(delegate_subtask branches, api.py's per-task client) — _make_cli_dispatch
must build its own fresh LLMClient per dispatch instead of reusing the
caller's shared one.
"""
import threading
import time

from llm_client import LLMClient
from memory.conversation_store import ConversationStore
from memory.episode_store import EpisodeStore
from memory.store import MemoryStore
import luclas


class _SlowLLM:
    """Stands in for the shared llm passed into _make_cli_dispatch — never
    actually called (TaskRunner gets its own fresh client), just here so a
    bug that accidentally reuses it would be observable via identity."""
    def is_available(self):
        return True


def test_cli_dispatch_builds_a_fresh_llm_client_per_call_not_shared(isolated_db, monkeypatch):
    calls = []

    class _FakeRunner:
        def __init__(self, *a, **kw):
            pass

        def run(self, goal, on_result=None):
            time.sleep(0.2)
            if on_result:
                on_result(f"done: {goal}")
            return f"done: {goal}"

    def fake_task_runner(*a, **kw):
        calls.append(kw.get("llm"))
        return _FakeRunner(*a, **kw)

    # dispatch() constructs TaskRunner via the module-level name
    # luclas.TaskRunner — patch that name directly.
    monkeypatch.setattr(luclas, "TaskRunner", fake_task_runner)

    conv_store = ConversationStore()
    episode_store = EpisodeStore()
    store = MemoryStore()
    shared_llm = LLMClient()

    dispatch = luclas._make_cli_dispatch(conv_store, episode_store, shared_llm, store, [], {})

    def worker(n):
        dispatch(f"conv{n}", f"goal {n}", False)

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)
    time.sleep(0.3)  # let both background threads finish their _run()

    assert len(calls) == 2, f"expected two dispatches to each construct a TaskRunner, got {len(calls)}"
    assert calls[0] is not calls[1], "each dispatch must get its own fresh LLMClient, not share the caller's"
    assert calls[0] is not shared_llm and calls[1] is not shared_llm, (
        "dispatch must never hand the shared long-lived LLMClient straight to a background TaskRunner — "
        "that's the exact _model_queue/_current_idx race LLMClient.clone() exists to avoid elsewhere"
    )
