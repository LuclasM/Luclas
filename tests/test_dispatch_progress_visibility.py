"""
Background dispatch_task is supposed to run quietly — the conversation
keeps going, and only the final answer eventually lands. api.py's
_run_task() previously wired progress_callback=push unconditionally
regardless of foreground/background, so every tool-call round trip inside
a *background* task pushed a live "💭 thinking / ▶ tool_name" line to the
real messaging channel exactly like foreground ("watch it happen") mode —
confirmed via a real WeChat test session where a background-dispatched
task's full execution trace showed up in the chat. show_progress=False is
how background dispatch now stays quiet; this pins that down so it can't
silently regress.
"""
import queue

from memory.store import MemoryStore
import api


class _FakeRunner:
    def __init__(self, *a, **kw):
        self.progress_callback = kw.get("progress_callback")

    def run(self, goal, on_result=None):
        if on_result:
            on_result("fake result")
        return "fake result"


class _FakeLLM:
    _router = None


def test_background_dispatch_has_no_progress_callback(isolated_db, monkeypatch):
    monkeypatch.setattr(api, "_store", MemoryStore())
    monkeypatch.setattr(api, "_llm", _FakeLLM())

    captured = {}

    def fake_task_runner(*a, **kw):
        r = _FakeRunner(*a, **kw)
        captured["progress_callback"] = r.progress_callback
        return r

    monkeypatch.setattr(api, "TaskRunner", fake_task_runner)

    api._run_task("t-bg", "do something", "wecom_TEST", queue.Queue(), show_progress=False)
    assert captured["progress_callback"] is None, (
        "background dispatch must not wire a progress_callback — otherwise every tool-call "
        "round trip narrates itself to the live channel, defeating the point of running quietly"
    )


def test_foreground_dispatch_still_shows_progress(isolated_db, monkeypatch):
    monkeypatch.setattr(api, "_store", MemoryStore())
    monkeypatch.setattr(api, "_llm", _FakeLLM())

    captured = {}

    def fake_task_runner(*a, **kw):
        r = _FakeRunner(*a, **kw)
        captured["progress_callback"] = r.progress_callback
        return r

    monkeypatch.setattr(api, "TaskRunner", fake_task_runner)

    api._run_task("t-fg", "do something", "wecom_TEST", queue.Queue(), show_progress=True)
    assert captured["progress_callback"] is not None, (
        "foreground ('watch it happen') dispatch must still push live progress"
    )
