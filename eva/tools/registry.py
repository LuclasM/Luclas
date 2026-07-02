import json
from tools.shell import shell_exec, SHELL_EXEC_SCHEMA
from tools.file_ops import file_read, file_write, file_list, FILE_READ_SCHEMA, FILE_WRITE_SCHEMA, FILE_LIST_SCHEMA
from tools.web import web_search, web_fetch, WEB_SEARCH_SCHEMA, WEB_FETCH_SCHEMA
from tools.python_exec import python_exec, PYTHON_EXEC_SCHEMA
from tools.search import grep, find_files, GREP_SCHEMA, FIND_FILES_SCHEMA
from tools.http_client import http_request, HTTP_REQUEST_SCHEMA
from tools.memory_tools import make_memory_tools
from tools.core_tools import core_update, CORE_UPDATE_SCHEMA
from tools.user_input import ask_user, ASK_USER_SCHEMA
from tools.schedule_tools import (
    schedule_add, schedule_list, schedule_delete, schedule_toggle,
    SCHEDULE_ADD_SCHEMA, SCHEDULE_LIST_SCHEMA, SCHEDULE_DELETE_SCHEMA, SCHEDULE_TOGGLE_SCHEMA,
)
from memory.store import MemoryStore


def build_tools(store: MemoryStore):
    mem_schemas, mem_fns = make_memory_tools(store)

    schemas = [
        SHELL_EXEC_SCHEMA,
        PYTHON_EXEC_SCHEMA,
        FILE_READ_SCHEMA,
        FILE_WRITE_SCHEMA,
        FILE_LIST_SCHEMA,
        GREP_SCHEMA,
        FIND_FILES_SCHEMA,
        WEB_SEARCH_SCHEMA,
        WEB_FETCH_SCHEMA,
        HTTP_REQUEST_SCHEMA,
        CORE_UPDATE_SCHEMA,
        ASK_USER_SCHEMA,
        SCHEDULE_ADD_SCHEMA,
        SCHEDULE_LIST_SCHEMA,
        SCHEDULE_DELETE_SCHEMA,
        SCHEDULE_TOGGLE_SCHEMA,
        *mem_schemas,
    ]
    fns = {
        "shell_exec":    shell_exec,
        "python_exec":   python_exec,
        "file_read":     file_read,
        "file_write":    file_write,
        "file_list":     file_list,
        "grep":          grep,
        "find_files":    find_files,
        "web_search":    web_search,
        "web_fetch":     web_fetch,
        "http_request":  http_request,
        "core_update":   core_update,
        "ask_user":        ask_user,
        "schedule_add":    schedule_add,
        "schedule_list":   schedule_list,
        "schedule_delete": schedule_delete,
        "schedule_toggle": schedule_toggle,
        **mem_fns,
    }
    return schemas, fns


def execute_tool(fn_name: str, fn_args: str, fns: dict) -> tuple[str, bool]:
    fn = fns.get(fn_name)
    if not fn:
        available = ", ".join(sorted(fns.keys()))
        return f"Unknown tool: {fn_name}. Available tools: {available}", True
    try:
        args = json.loads(fn_args) if fn_args else {}
        result = fn(**args)
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False, indent=2), False
        return str(result), False
    except json.JSONDecodeError as e:
        return f"Argument parsing failed: {e}", True
    except TypeError as e:
        return f"Argument error: {e}", True
    except Exception as e:
        return f"Execution error: {e}", True
