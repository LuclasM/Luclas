from memory.store import MemoryStore
from tools.memory_tools import make_memory_tools


def test_search_does_not_count_as_lesson_use(isolated_db):
    store = MemoryStore()
    mid = store.write("selected lesson content", importance=4)

    results = store.search(query="selected lesson")

    assert any(row["id"] == mid for row in results)
    row = store.get(mid)
    assert row["access_count"] == 0
    assert row["importance"] == 4


def test_memory_read_counts_only_selected_lesson_as_used(isolated_db):
    store = MemoryStore()
    used_id = store.write("lesson that will be used", importance=4)
    unused_id = store.write("another search candidate", importance=4)
    _, fns = make_memory_tools(store)

    searched = fns["memory_search"](query="lesson candidate")
    assert all("content" not in row for row in searched["results"])
    assert store.get(used_id)["access_count"] == 0
    assert store.get(unused_id)["access_count"] == 0

    read = fns["memory_read"](ids=[used_id, used_id])

    assert read["count"] == 1
    assert read["results"][0]["content"] == "lesson that will be used"
    used = store.get(used_id)
    unused = store.get(unused_id)
    assert used["access_count"] == 1
    assert used["importance"] == 5
    assert used["freshness"] == 1.0
    assert unused["access_count"] == 0
    assert unused["importance"] == 4
