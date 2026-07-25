"""
conversation_runner.py — 持续对话层的轮次主循环

跟 loops/agent_loop.py 的 run_agent() 不是一回事：run_agent() 是"跑到给出最终
答案就结束"的任务执行循环（可能上百轮迭代，带 shell/file/web 等干活工具）；
这里是一个很轻量的、专门服务于持续聊天的循环——工具集只有 memory_search/
memory_write/dispatch_task 三个，最终目的是要么直接聊完，要么把真正的活儿
通过 dispatch_task 派发给现有的 TaskRunner/run_agent 机制去做。

调用方（api.py 的 /chat、luclas.py 的交互式 REPL）负责提供 dispatch_fn——一个
"如何真正执行一个任务"的回调，因为前台/后台任务派发要复用各自入口已有的
push/进度展示机制，这里不重复实现。dispatch_fn(conversation_id, goal,
foreground) -> {"delivered": bool, ...} 的约定见 _build_tools()。

话题切换检测：agent_turn() 返回的就是 OpenAI 风格 message 的 content/
tool_calls 原始字段，没有任何"thinking vs 最终回复"的结构化切分可以复用
（llm_client.py 里叫 thinking 的东西其实就是 content 本身）。所以这里自己定
了一个新的、只在这个循环内部使用的极简约定：模型在最终文字回复正文之后另起
一行输出 <topic>continue</topic> 或 <topic>new</topic>，解析完就从展示文本
里剥离，缺失时按 continue 处理（安全默认，不会把对话切得过碎）。
"""
import json
import re

import i18n as T
from memory.conversation_store import TASK_EPISODE_TAG
from tools.registry import execute_tool

CONVERSATION_MAX_ITERATIONS = 6

_TOPIC_TAG_RE = re.compile(r'<topic>\s*(continue|new)\s*</topic>\s*$', re.IGNORECASE)

DISPATCH_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dispatch_task",
        "description": (
            "Hand off real work (searching, executing operations, writing files, "
            "any multi-step task) to the task-execution engine — don't pretend to "
            "do it yourself in plain text. foreground=false (default) runs it in "
            "the background so the conversation stays responsive; use "
            "foreground=true only when the user explicitly asked to watch the "
            "work happen instead of chatting, which replaces the conversation "
            "with live task progress until it's done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What to do, phrased as a clear task instruction"},
                "foreground": {"type": "boolean", "description": "true = show live task progress in place of the conversation; false = run in the background"},
                "episode_refs": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Specific episode/task ids (from memory_search results, or ids already visible earlier in this conversation) to pull in as precise extra context for this task",
                },
            },
            "required": ["goal"],
        },
    },
}


def _system_prompt() -> str:
    return (
        "你是 Luclas，一个持续在线、能记住最近对话的个人助理，不是每条消息都独立处理的问答机器人。\n"
        "可以直接聊天回答问题；当用户的请求需要你实际去做一件事（搜索、执行操作、写文件、"
        "跑多步骤的任务等）时，调用 dispatch_task 工具把它派发出去执行，不要自己假装完成。\n"
        "需要回忆更早之前的事时用 memory_search；确认值得长期记住的事实/经验用 memory_write。\n\n"
        "每次你给出最终文字回复（没有调用任何工具）时，在回复正文结束后另起一行，输出 "
        "<topic>continue</topic>（这轮回复延续的还是当前话题）或 <topic>new</topic>"
        "（这轮回复开启了一个新话题）。这一行本身不会展示给用户，只是内部标记，不确定时用 continue。"
    )


def _build_messages(raw_messages: list) -> list:
    convo = [{"role": m["role"], "content": m["content"]} for m in raw_messages]
    return [{"role": "system", "content": _system_prompt()}] + convo


def _strip_topic_tag(content: str) -> tuple:
    """Returns (display_text, topic_changed). Deliberately no re.MULTILINE on
    _TOPIC_TAG_RE — with it, `$` matches before every line break, so a reply
    that merely *mentions* the tag format earlier (e.g. explaining it, or
    just coincidentally similar text) would match there instead of at the
    true trailing tag, truncating real reply content and leaking the actual
    tag as visible text. Without it, `$` only matches the end of the whole
    string (or just before one trailing newline), so this can only ever
    match the genuinely final line."""
    m = _TOPIC_TAG_RE.search(content)
    if not m:
        return content.strip(), False
    return content[:m.start()].strip(), m.group(1).lower() == "new"


def _resolve_episode_refs(episode_refs: list, episode_store, mem_store) -> str:
    if not episode_refs:
        return ""
    parts = []
    for ref in episode_refs:
        ep = episode_store.get(ref) or episode_store.get_by_task_id(ref)
        if ep:
            episode_store.reference(ep["id"])
            parts.append(f"[经历 {ep['id']}] {ep['content']}")
            continue
        lesson = mem_store.get(ref)
        if lesson:
            parts.append(f"[经验 {lesson['id']}] {lesson['content']}")
    if not parts:
        return ""
    return "参考以下历史记录：\n" + "\n\n".join(parts)


def _build_tools(mem_store, episode_store, dispatch_fn, conversation_id):
    from tools.memory_tools import make_memory_tools
    mem_schemas, mem_fns = make_memory_tools(mem_store, episode_store)
    # Only memory_search/memory_write belong to the conversation layer's own
    # tool set — memory_update/memory_delete and everything else (shell/file/
    # web/schedule tools) stay task-execution-only, reachable via dispatch_task.
    schemas = [s for s in mem_schemas if s["function"]["name"] in ("memory_search", "memory_write")]
    fns = {k: v for k, v in mem_fns.items() if k in ("memory_search", "memory_write")}

    def dispatch_task(goal: str, foreground: bool = False, episode_refs: list = None) -> dict:
        context_text = _resolve_episode_refs(episode_refs or [], episode_store, mem_store)
        full_goal = f"{context_text}\n\n---\n\n{goal}" if context_text else goal
        return dispatch_fn(conversation_id, full_goal, foreground)

    schemas.append(DISPATCH_TASK_SCHEMA)
    fns["dispatch_task"] = dispatch_task
    return schemas, fns


def handle_turn(conversation_id: str, message: str, llm, mem_store, episode_store,
                 conv_store, dispatch_fn) -> tuple:
    """One turn of the persistent conversation. Returns (reply_text,
    already_delivered) — already_delivered=True means a foreground task
    dispatch already pushed/printed its own result via the existing
    progress-display machinery, so the caller must not deliver it again."""
    conv_store.append_message(conversation_id, "user", message)
    schemas, fns = _build_tools(mem_store, episode_store, dispatch_fn, conversation_id)

    working = _build_messages(conv_store.get_messages(conversation_id))
    reply_text = ""
    already_delivered = False
    topic_changed = False

    for _ in range(CONVERSATION_MAX_ITERATIONS):
        turn = llm.agent_turn(working, schemas)
        content = turn.get("content") or ""
        tool_calls = turn.get("tool_calls") or []

        if not tool_calls:
            reply_text, topic_changed = _strip_topic_tag(content)
            if not reply_text:
                reply_text = content.strip()
            break

        working.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            name = call["function"]["name"]
            fn_args = call["function"].get("arguments", "{}")
            result_str, is_error = execute_tool(name, fn_args, fns)
            if name == "dispatch_task" and not is_error:
                try:
                    parsed = json.loads(result_str)
                except json.JSONDecodeError:
                    parsed = {}
                if parsed.get("delivered"):
                    already_delivered = True
                    reply_text = parsed.get("result", "")
            working.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result_str})
        if already_delivered:
            break
    else:
        if not reply_text:
            reply_text = T.sentinel_exceeded_max_iter()

    if reply_text:
        # Foreground task results already went through their own push and
        # aren't part of a topic-segment — tag them so eviction can drop them
        # once old without needing a topic-close first (see conversation_store.py).
        conv_store.append_message(conversation_id, "assistant", reply_text,
                                  episode_id=TASK_EPISODE_TAG if already_delivered else None)
    if topic_changed:
        conv_store.close_topic(conversation_id, episode_store)

    conv_store.evict_if_over_threshold(conversation_id, context_length=llm.context_length)

    return reply_text, already_delivered
