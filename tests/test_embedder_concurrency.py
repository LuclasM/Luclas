"""
memory/embedder.py's model lazy-load is a check-then-set that many concurrent
task threads can hit at once (every memory write/search, across every
concurrent session) — this stubs out the actual model class (loading a real
sentence-transformers model is slow and irrelevant to what's being tested)
to prove the double-checked lock prevents a duplicate load under a real race.
"""
import sys
import threading
import time
import types


def test_no_duplicate_model_load_under_concurrent_first_use(monkeypatch):
    import memory.embedder as emb

    # Reset module-level state so this test doesn't depend on (or pollute)
    # whatever other tests already triggered a load.
    monkeypatch.setattr(emb, "_model", None)

    load_count = {"n": 0}
    count_lock = threading.Lock()

    class FakeSentenceTransformer:
        def __init__(self, *a, **kw):
            with count_lock:
                load_count["n"] += 1
            time.sleep(0.2)  # widen the race window

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    threads = [threading.Thread(target=emb._load) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert load_count["n"] == 1, (
        f"20 concurrent first-calls to _load() must construct the model exactly once, "
        f"got {load_count['n']} — the double-checked lock isn't preventing the race"
    )
