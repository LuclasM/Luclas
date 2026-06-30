"""
tools/file_ops.py — 文件读写操作
"""

import os

FILE_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_read",
        "description": "Read a file's content as text. Truncated past 8000 characters.",
        "parameters": {
            "type": "object",
            "properties": {
                "path":   {"type": "string", "description": "File path"},
                "offset": {"type": "integer", "description": "Line to start reading from (0-based), default 0"},
                "limit":  {"type": "integer", "description": "Max number of lines to read, default unlimited"},
            },
            "required": ["path"],
        },
    },
}

FILE_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_write",
        "description": "Write content to a file. mode='w' overwrites, mode='a' appends.",
        "parameters": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "Content to write"},
                "mode":    {"type": "string", "description": "w=overwrite, a=append", "default": "w"},
            },
            "required": ["path", "content"],
        },
    },
}

FILE_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_list",
        "description": "List files and subdirectories under a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path":      {"type": "string", "description": "Directory path, default current directory"},
                "recursive": {"type": "boolean", "description": "Whether to list recursively", "default": False},
            },
        },
    },
}


def file_read(path: str, offset: int = 0, limit: int = None) -> dict:
    try:
        path = os.path.expanduser(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if offset:
            lines = lines[offset:]
        if limit:
            lines = lines[:limit]
        content = "".join(lines)
        truncated = len(content) > 8000
        return {
            "content":   content[:8000],
            "truncated": truncated,
            "total_lines": len(lines),
        }
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception as e:
        return {"error": str(e)}


def file_write(path: str, content: str, mode: str = "w") -> dict:
    try:
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, mode, encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "path": path, "bytes": len(content.encode())}
    except Exception as e:
        return {"error": str(e)}


def file_list(path: str = ".", recursive: bool = False) -> dict:
    try:
        path = os.path.expanduser(path)
        if recursive:
            result = []
            for root, dirs, files in os.walk(path):
                for fname in files:
                    result.append(os.path.join(root, fname))
            return {"entries": result[:200]}
        else:
            entries = os.listdir(path)
            return {"entries": sorted(entries)}
    except Exception as e:
        return {"error": str(e)}
