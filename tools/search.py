"""
tools/search.py — 文件搜索（grep / find）
"""

import subprocess

GREP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search text content in files or directories (regex supported). "
            "Returns matching lines with line numbers. Useful for finding keywords, function definitions, "
            "error messages, etc. in code/docs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern":   {"type": "string", "description": "Regex or keyword to search for"},
                "path":      {"type": "string", "description": "Search path (file or directory), default current directory", "default": "."},
                "recursive": {"type": "boolean", "description": "Whether to search directories recursively, default true", "default": True},
                "ignore_case": {"type": "boolean", "description": "Whether to ignore case, default false", "default": False},
                "max_results": {"type": "integer", "description": "Max number of lines returned, default 50", "default": 50},
            },
            "required": ["pattern"],
        },
    },
}

FIND_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_files",
        "description": (
            "Find files in a directory tree by filename pattern. "
            "Supports wildcards (*.py, *.log, etc.) and filtering by modification time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Filename wildcard, e.g. *.py, config.*"},
                "path":    {"type": "string", "description": "Root directory to search, default current directory", "default": "."},
                "max_results": {"type": "integer", "description": "Max number of results, default 50", "default": 50},
            },
            "required": ["pattern"],
        },
    },
}


def grep(pattern: str, path: str = ".", recursive: bool = True,
         ignore_case: bool = False, max_results: int = 50) -> dict:
    flags = ["-n", "--color=never"]
    if recursive:
        flags.append("-r")
    if ignore_case:
        flags.append("-i")
    flags += ["-m", str(max_results)]

    cmd = ["grep"] + flags + [pattern, path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = proc.stdout.splitlines()
        return {
            "matches": lines[:max_results],
            "count":   len(lines),
            "truncated": len(lines) >= max_results,
        }
    except subprocess.TimeoutExpired:
        return {"matches": [], "count": 0, "error": "Search timed out"}
    except Exception as e:
        return {"matches": [], "count": 0, "error": str(e)}


def find_files(pattern: str, path: str = ".", max_results: int = 50) -> dict:
    cmd = ["find", path, "-name", pattern, "-type", "f"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        files = [l for l in proc.stdout.splitlines() if l]
        return {
            "files":     files[:max_results],
            "count":     len(files),
            "truncated": len(files) >= max_results,
        }
    except subprocess.TimeoutExpired:
        return {"files": [], "count": 0, "error": "Search timed out"}
    except Exception as e:
        return {"files": [], "count": 0, "error": str(e)}
