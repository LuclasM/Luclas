"""
tools/delegate.py — delegate_subtask 工具

只是一层薄绑定：真正的分支逻辑（护栏、建子节点、跑嵌套 run_agent、AAR、落库）
全部在 loops/task_runner.py 的 TaskRunner._spawn_branch 里，因为那些都是
TaskRunner 已有的职责。这里只负责把工具 schema 接到调用方传入的闭包上
——跟 tools/memory_tools.py 的 make_memory_tools(store) 是同一种模式。
"""

DELEGATE_SUBTASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_subtask",
        "description": (
            "Branch a genuinely independent chunk of work into its own focused sub-conversation, then "
            "fold back just its final result. Use this for exploratory work that would otherwise clutter "
            "this conversation with many intermediate tool calls, or for a chunk that's clearly independent "
            "of what you're doing right now. Call it more than once in the same turn to run multiple "
            "independent subtasks in parallel — you'll get all their results back together before "
            "continuing. Prefer doing small, quick steps directly with your own tools instead of "
            "delegating them; only branch when it's genuinely worth a separate sub-conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "The subtask's goal, stated completely and self-containedly. The branch will NOT "
                        "see this conversation's raw history — only what you put here and in `context`."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Facts, constraints, or prior results the branch needs but has no other way to "
                        "know (already-confirmed data, IDs, decisions made so far in this conversation). "
                        "Optional, but include it for any non-trivial subtask — the branch only has this "
                        "plus the task tree and long-term memory, nothing else from this conversation."
                    ),
                },
            },
            "required": ["goal"],
        },
    },
}


def make_delegate_tool(spawn_branch_fn):
    """spawn_branch_fn(goal, context) -> str — provided by the caller (a
    TaskRunner method closure bound to the specific node/depth/ancestors this
    tool instance belongs to)."""

    def delegate_subtask(goal: str, context: str = "") -> str:
        return spawn_branch_fn(goal, context)

    return DELEGATE_SUBTASK_SCHEMA, delegate_subtask
