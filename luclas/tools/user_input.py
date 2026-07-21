"""
tools/user_input.py — 任务执行中向用户提问，等待回答后继续

三种运行模式（ask_user 按下面顺序判断）：
  1. 已设置 channel context（消息平台后台线程，见 api.py:_run_task）：
     把问题推送给用户，阻塞等待同一 session 的下一条消息作为回答
     （复用 supplement_queue），超时视为未作答。
  2. 交互式终端（tty）：直接阻塞 input()。
  3. 真正无渠道的无头模式（如 --run/--reflect 且未设置 channel context）：
     抛 _NeedUserInput，由调用方决定怎么处理。
"""

import queue
import sys
import threading

import i18n as T
from config import ASK_USER_TIMEOUT_SECONDS
from utils.display import bold, warn

ASK_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Pause the current task to ask the user a question, then continue after they answer. "
            "Use this when you need information only the user can provide — a decision, a missing "
            "credential, a preference, or a clarification — and proceeding without it would produce "
            "wrong results. Do not use it to report progress or confirm steps you can verify yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to ask the user. Be concise and concrete.",
                },
            },
            "required": ["question"],
        },
    },
}

# Per-thread: (push_fn, wait_queue) for the messaging channel driving the
# currently-executing task, if any. Each background task runs in its own
# thread (api.py:_run_task), so thread-local storage cleanly isolates
# concurrent sessions without threading extra params through execute_tool.
_channel_ctx = threading.local()

# delegate_subtask branches run on their own worker threads (see
# loops/agent_loop.py), which don't inherit threading.local() state from the
# thread that spawned them — set_channel_context() must be re-applied inside
# each branch thread using get_channel_context() read from the caller first.
#
# ask_user() also needs a lock: if two branches of the *same* task ask_user()
# concurrently over the same channel/terminal, the user's one reply can only
# resolve one of two waiting wait_queue.get() calls (or two tty prompts would
# interleave), so at most one ask_user() may be in flight per session at a
# time. That lock must be scoped *per session_id*, not process-wide — api.py
# serves many independent messaging sessions concurrently (see _run_task),
# and a single global lock would make an unrelated user's ask_user() block
# behind whichever other session happened to ask first, for up to
# ASK_USER_TIMEOUT_SECONDS. session_id is None for the plain-tty/no-channel
# case (there's genuinely only one terminal then, so one shared lock is
# correct there). Locks accumulate one per distinct session_id ever seen —
# unbounded strictly speaking, but bounded in practice by the number of real
# users on real channels.
_channel_locks: dict = {}
_channel_locks_meta_lock = threading.Lock()


def _lock_for_session(session_id) -> threading.Lock:
    with _channel_locks_meta_lock:
        lock = _channel_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _channel_locks[session_id] = lock
        return lock


def set_channel_context(push, wait_queue, session_id=None) -> None:
    """Call at the start of a channel-driven task thread so ask_user() can
    push questions to the user and block for their reply on wait_queue."""
    _channel_ctx.push = push
    _channel_ctx.wait_queue = wait_queue
    _channel_ctx.session_id = session_id


def clear_channel_context() -> None:
    _channel_ctx.push = None
    _channel_ctx.wait_queue = None
    _channel_ctx.session_id = None


def get_channel_context():
    """Read the current thread's channel context, to propagate into a
    delegate_subtask branch's own worker thread."""
    return (getattr(_channel_ctx, "push", None),
            getattr(_channel_ctx, "wait_queue", None),
            getattr(_channel_ctx, "session_id", None))


def ask_user(question: str) -> str:
    session_id = getattr(_channel_ctx, "session_id", None)
    with _lock_for_session(session_id):
        push = getattr(_channel_ctx, "push", None)
        wait_queue = getattr(_channel_ctx, "wait_queue", None)

        if push and wait_queue is not None:
            try:
                push(f"❓ {question}")
            except Exception:
                pass
            try:
                answer = wait_queue.get(timeout=ASK_USER_TIMEOUT_SECONDS)
            except queue.Empty:
                return T.ask_user_no_answer()
            return answer.strip() if answer and answer.strip() else T.ask_user_no_answer()

        if not sys.stdin.isatty():
            # 无渠道、无终端（如纯无头脚本）：把问题抛回给调用方
            raise _NeedUserInput(question)

        print(f"\n{warn('─' * 50)}")
        print(f"  {bold(T.ask_user_label())}")
        print(f"\n  {question}\n")
        try:
            answer = input(f"  {T.ask_user_prompt()} ").strip()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        print(f"{warn('─' * 50)}\n")
        return answer if answer else T.ask_user_no_answer()


class _NeedUserInput(Exception):
    def __init__(self, question: str):
        self.question = question
        super().__init__(question)
