import json
from memory.store import MemoryStore

_SOURCE_DESC = (
    "Where this knowledge came from: "
    "first_hand (this agent's own direct task execution/observation), "
    "user_instruction (the user told you directly), "
    "learning_material (textbook/course/reference doc), "
    "others_experience (someone else's reported experience, not verified first-hand), "
    "web (general web search result). Leave empty if unclear."
)
_CREDIBILITY_DESC = (
    "How trustworthy this is, on a 1-10 scale (same scale as importance; 0/omitted = unset). "
    "Guidance — first_hand and user_instruction: 9-10. learning_material (textbooks/official "
    "docs): 8-9. web: judge by the site's authority (e.g. a government agency's own site: 8-9; "
    "a random blog: 3-5). others_experience: judge by how detailed/plausible it is (3-7). "
    "General/unverified material: 5-6. Leave unset (0) if unclear."
)

MEMORY_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_write",
        "description": "Store knowledge, experience, or opinions into long-term memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "content":     {"type": "string",  "description": "Memory content"},
                "type":        {"type": "string",  "description": "Memory type, e.g. fact/experience/workflow/opinion/keypoint"},
                "tags":        {"type": "array",   "items": {"type": "string"}, "description": "List of tags"},
                "importance":  {"type": "integer", "description": "Importance level 1-10, default 5"},
                "source":      {"type": "string",  "description": _SOURCE_DESC},
                "credibility": {"type": "integer", "description": _CREDIBILITY_DESC},
            },
            "required": ["content"],
        },
    },
}

MEMORY_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": (
            "Search long-term memory: both lessons (experience/opinions/facts, "
            "keyword+type+tag filterable) and episodes (past conversation topics "
            "and finished tasks, keyword-searchable, returned alongside lessons "
            "when a query is given). Each episode result's 'id' can be cited "
            "directly in dispatch_task's episode_refs for a precise, non-fuzzy "
            "reference instead of relying on this search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query":          {"type": "string",  "description": "Search keyword"},
                "type":           {"type": "string",  "description": "Filter by type (lessons only)"},
                "tags":           {"type": "array",   "items": {"type": "string"}, "description": "Filter by tags (lessons only)"},
                "min_importance":   {"type": "integer", "description": "Minimum importance filter (lessons only)"},
                "source":           {"type": "string",  "description": "Filter by source (lessons only)"},
                "min_credibility":  {"type": "integer", "description": "Minimum credibility filter, 1-10 scale (lessons only)"},
                "limit":            {"type": "integer", "description": "Max number of results per kind, default 20"},
            },
        },
    },
}

MEMORY_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_update",
        "description": "Update the content, type, tags, importance, source, or credibility of an existing memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "id":          {"type": "string",  "description": "Memory ID"},
                "content":     {"type": "string",  "description": "New content"},
                "type":        {"type": "string",  "description": "New type"},
                "tags":        {"type": "array",   "items": {"type": "string"}},
                "importance":  {"type": "integer"},
                "source":      {"type": "string",  "description": _SOURCE_DESC},
                "credibility": {"type": "integer", "description": _CREDIBILITY_DESC},
            },
            "required": ["id"],
        },
    },
}

MEMORY_DELETE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_delete",
        "description": "Delete the specified memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory ID"},
            },
            "required": ["id"],
        },
    },
}


def make_memory_tools(store: MemoryStore, episode_store=None):
    schemas = [MEMORY_WRITE_SCHEMA, MEMORY_SEARCH_SCHEMA,
               MEMORY_UPDATE_SCHEMA, MEMORY_DELETE_SCHEMA]

    def memory_write(content: str, type: str = "", tags: list = None,
                     importance: int = 5, source: str = "", credibility: int = 0) -> dict:
        mid = store.write(content, type=type, tags=tags, importance=importance,
                          source=source, credibility=credibility)
        return {"ok": True, "id": mid}

    def memory_search(query: str = "", type: str = "", tags: list = None,
                      min_importance: int = 0, source: str = "", min_credibility: int = 0,
                      limit: int = 20) -> dict:
        results = store.search(query=query, type=type, tags=tags,
                               min_importance=min_importance, source=source,
                               min_credibility=min_credibility, limit=limit)
        out = {"count": len(results), "results": results}
        # episode_store is only passed for the conversation layer (see
        # conversation_runner.py) — task-execution's memory_search stays
        # lessons-only, matching its pre-existing behavior.
        if episode_store is not None:
            episodes = episode_store.search(query=query, limit=limit)
            for e in episodes:
                episode_store.reference(e["id"])  # a search hit is a usage reference
            out["episodes"] = episodes
            out["count"] += len(episodes)
        return out

    def memory_update(id: str, content: str = None, type: str = None,
                      tags: list = None, importance: int = None,
                      source: str = None, credibility: int = None) -> dict:
        ok = store.update(id, content=content, type=type,
                          tags=tags, importance=importance,
                          source=source, credibility=credibility)
        return {"ok": ok}

    def memory_delete(id: str) -> dict:
        ok = store.delete(id)
        return {"ok": ok}

    fns = {
        "memory_write":  memory_write,
        "memory_search": memory_search,
        "memory_update": memory_update,
        "memory_delete": memory_delete,
    }
    return schemas, fns
