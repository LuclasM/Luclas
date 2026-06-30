import json
from memory.store import MemoryStore

MEMORY_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_write",
        "description": "Store knowledge, experience, or opinions into long-term memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "content":    {"type": "string",  "description": "Memory content"},
                "type":       {"type": "string",  "description": "Memory type, e.g. fact/experience/workflow/opinion/keypoint"},
                "tags":       {"type": "array",   "items": {"type": "string"}, "description": "List of tags"},
                "importance": {"type": "integer", "description": "Importance level 1-10, default 5"},
            },
            "required": ["content"],
        },
    },
}

MEMORY_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": "Search long-term memory. Supports keyword, type, and tag filtering.",
        "parameters": {
            "type": "object",
            "properties": {
                "query":          {"type": "string",  "description": "Search keyword"},
                "type":           {"type": "string",  "description": "Filter by type"},
                "tags":           {"type": "array",   "items": {"type": "string"}, "description": "Filter by tags"},
                "min_importance": {"type": "integer", "description": "Minimum importance filter"},
                "limit":          {"type": "integer", "description": "Max number of results, default 20"},
            },
        },
    },
}

MEMORY_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_update",
        "description": "Update the content, type, tags, or importance of an existing memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "id":         {"type": "string",  "description": "Memory ID"},
                "content":    {"type": "string",  "description": "New content"},
                "type":       {"type": "string",  "description": "New type"},
                "tags":       {"type": "array",   "items": {"type": "string"}},
                "importance": {"type": "integer"},
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


def make_memory_tools(store: MemoryStore):
    schemas = [MEMORY_WRITE_SCHEMA, MEMORY_SEARCH_SCHEMA,
               MEMORY_UPDATE_SCHEMA, MEMORY_DELETE_SCHEMA]

    def memory_write(content: str, type: str = "", tags: list = None,
                     importance: int = 5) -> dict:
        mid = store.write(content, type=type, tags=tags, importance=importance)
        return {"ok": True, "id": mid}

    def memory_search(query: str = "", type: str = "", tags: list = None,
                      min_importance: int = 0, limit: int = 20) -> dict:
        results = store.search(query=query, type=type, tags=tags,
                               min_importance=min_importance, limit=limit)
        return {"count": len(results), "results": results}

    def memory_update(id: str, content: str = None, type: str = None,
                      tags: list = None, importance: int = None) -> dict:
        ok = store.update(id, content=content, type=type,
                          tags=tags, importance=importance)
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
