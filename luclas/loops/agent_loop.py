"""
loops/agent_loop.py — 核心执行循环

极简设计：调用 LLM → 执行工具 → 循环，直到 LLM 给出最终回答。
所有决策（何时停止、如何拆解、何时存记忆）由 LLM 根据 core.md 策略自主决定。
"""

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from llm_client import LLMClient
from tools.registry import execute_tool
from tools.core_tools import load_core
from tools.user_input import get_channel_context, set_channel_context, clear_channel_context
from config import AGENT_MAX_ITERATIONS, AGENT_STALL_WINDOW, AGENT_MAX_ERRORS, SESSION_DIR
from utils.display import dim, info, ok, err, warn
import i18n as T

_ANSI_RE = None

# Serializes terminal output across concurrently running delegate branches
# (each branch is its own run_agent() call on its own thread) so interleaved
# prints from different branches don't garble mid-line.
_PRINT_LOCK = threading.Lock()

_DELEGATE_TOOL_NAME = "delegate_subtask"


def run_agent(goal: str, task: dict, llm: LLMClient,
              schemas: list, fns: dict,
              task_context: str = "", parent_goal: str = "",
              progress_callback=None, supplement_queue=None,
              branch_tag: str = "") -> str:
    """
    执行一个目标，返回最终回答。
    task: dict with id, goal, log (mutated in-place)
    task_context: 工作历史字符串，注入 system prompt
    parent_goal: 父任务目标（子任务时传入）
    branch_tag: 非空时表示这是一次 delegate_subtask 分支执行，用于给终端输出
      加前缀，避免和并行跑的其他分支/主线程输出交叉错行。
    """
    core = load_core()
    system_prompt = _build_system(core)
    user_message  = _build_user_message(goal, task_context, parent_goal)
    messages = [
        {"role": "system",  "content": system_prompt},
        {"role": "user",    "content": user_message},
    ]

    call_history:   list[tuple] = []
    consecutive_errors = 0

    _log(task, f"\n=== Goal: {goal} ===")

    for iteration in range(1, AGENT_MAX_ITERATIONS + 1):
        # Inject any pending supplement messages from the user
        if supplement_queue is not None:
            _drain_supplements(supplement_queue, messages, task)

        with _PRINT_LOCK:
            tag = f"{dim('[' + branch_tag + ']')} " if branch_tag else ""
            print(f"{tag}{T.round_header(iteration, AGENT_MAX_ITERATIONS)}")
            if iteration == 1:
                if parent_goal and parent_goal != goal:
                    print(f"{tag}  │  {dim(parent_goal[:70])}")
                    print(f"{tag}  │  {'  › ' + info(goal[:66])}")
                else:
                    print(f"{tag}  │  {info(goal[:70])}")

        try:
            turn = llm.agent_turn(messages, schemas)
        except KeyboardInterrupt:
            _handle_pause(task, goal, messages)  # 二次 Ctrl-C 时内部 raise
            continue
        except RuntimeError as e:
            err_str = str(e)
            # Escalate on rate-limit, server error, or connection failure.
            # "500" belongs here for the same reason "502"/"503" do — a bare
            # Internal Server Error from the LLM endpoint (OOM, model still
            # loading, etc.) is exactly the kind of transient server-side
            # failure this comment already says should escalate, not the
            # fatal break below.
            if any(sig in err_str for sig in ("429", "500", "502", "503", "Could not connect", "timed out")):
                if llm.escalate():
                    _log(task, f"LLM error, escalated to next model: {err_str[:120]}")
                    continue
            print(f"  {err('✗')} {T.llm_call_failed(e)}")
            _log(task, T.llm_call_failed(e))
            break

        thinking   = turn.get("content") or ""
        tool_calls = turn.get("tool_calls") or []

        if thinking.strip():
            _print_thinking(thinking, branch_tag)
            _log(task, _strip_ansi(thinking))

        # 无工具调用 → 最终回答
        if not tool_calls:
            _log(task, f"Final answer: {thinking[:1000]}")
            _save_messages(task["id"], messages)
            return thinking.strip()

        # 进度推送：思考摘要 + 本轮工具名列表（每轮一条）
        if progress_callback:
            parts = []
            if thinking.strip():
                first_line = thinking.strip().splitlines()[0][:120]
                parts.append(f"💭 {first_line}")
            for tc in tool_calls:
                parts.append(f"▶ {tc['function']['name']}")
            try:
                progress_callback("\n".join(parts))
            except Exception:
                pass

        messages.append({"role": "assistant", **turn["raw"]})

        delegate_calls = [tc for tc in tool_calls if tc["function"]["name"] == _DELEGATE_TOOL_NAME]
        other_calls    = [tc for tc in tool_calls if tc["function"]["name"] != _DELEGATE_TOOL_NAME]

        # Parallel branch dispatch: every delegate_subtask call in this turn runs
        # concurrently, each on its own thread (LLM/tool calls are I/O-bound, so a
        # plain thread pool is enough — no asyncio rewrite needed). These are kept
        # out of the stall/consecutive-error tracking below, which exists to catch
        # one atomic tool being hammered on repeat, not independent branches.
        if delegate_calls:
            # threading.local() channel context (see tools/user_input.py) doesn't
            # cross into new worker threads on its own — read it here on the
            # calling thread and re-apply it inside each branch's own thread so
            # ask_user() still routes to the right messaging channel/terminal.
            channel_push, channel_wait_queue, channel_session_id = get_channel_context()

            def _run_delegate(name, args):
                set_channel_context(channel_push, channel_wait_queue, channel_session_id)
                try:
                    return execute_tool(name, args, fns)
                finally:
                    clear_channel_context()

            # Announce every branch before waiting on any of them — otherwise the
            # 2nd+ call's "▶ delegate_subtask" header only prints once we get
            # around to waiting on it, well after it actually started running.
            for tc in delegate_calls:
                _print_tool_call(tc["function"]["name"],
                                 tc["function"].get("arguments", "{}"), branch_tag)

            pool = ThreadPoolExecutor(max_workers=len(delegate_calls))
            futures = {
                tc["id"]: pool.submit(_run_delegate, tc["function"]["name"],
                                       tc["function"].get("arguments", "{}"))
                for tc in delegate_calls
            }
            for tc in delegate_calls:
                fn_args = tc["function"].get("arguments", "{}")
                fut = futures[tc["id"]]
                while True:
                    try:
                        result, is_error = fut.result()
                        break
                    except KeyboardInterrupt:
                        # Background branch threads aren't interruptible (CPython
                        # only delivers SIGINT to the main thread) — they keep
                        # running regardless; we can only pause/resume *waiting*
                        # for them here.
                        _handle_pause(task, goal, messages)  # 二次 Ctrl-C 时内部 raise
                    except Exception as e:
                        result, is_error = f"Delegate execution error: {e}", True
                        break
                _print_tool_result(result, is_error, branch_tag)
                _log(task, f"  {tc['function']['name']}({fn_args[:200]}) → {result[:300]}")
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            pool.shutdown(wait=False)

        tool_interrupted = False
        for i, tc in enumerate(other_calls):
            fn_name = tc["function"]["name"]
            fn_args = tc["function"].get("arguments", "{}")
            tc_id   = tc["id"]

            _print_tool_call(fn_name, fn_args, branch_tag)

            try:
                result, is_error = execute_tool(fn_name, fn_args, fns)
            except KeyboardInterrupt:
                # 补全本轮所有未完成的 tool result，否则 LLM API 会报错
                messages.append({"role": "tool", "tool_call_id": tc_id,
                                  "content": T.sentinel_paused_by_tool()})
                for rtc in other_calls[i + 1:]:
                    messages.append({"role": "tool", "tool_call_id": rtc["id"],
                                     "content": T.sentinel_skipped()})
                _handle_pause(task, goal, messages)  # 二次 Ctrl-C 时内部 raise
                tool_interrupted = True
                break

            _print_tool_result(result, is_error, branch_tag)

            # 卡死检测
            args_hash = hashlib.md5(fn_args.encode()).hexdigest()
            call_history.append((fn_name, args_hash))
            if _is_stalled(call_history):
                if llm.escalate():
                    call_history.clear()
                    consecutive_errors = 0
                    _log(task, f"  {fn_name}({fn_args[:200]}) → {result[:300]}")
                    # Complete pending tool results before injecting the system note,
                    # otherwise the API rejects the next call (tool_call_id mismatch).
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                    for rtc in other_calls[i + 1:]:
                        messages.append({"role": "tool", "tool_call_id": rtc["id"],
                                         "content": "[escalating — skipped]"})
                    messages.append({"role": "user", "content": "[系统：检测到循环，已切换模型，请换一种方式继续]"})
                    _log(task, "Stall detected — escalated to next model")
                    tool_interrupted = True
                    break
                reason = T.stalled_loop(AGENT_STALL_WINDOW)
                print(f"  {warn('⚠')} {reason}")
                _log(task, f"⚠ {reason}")
                _save_messages(task["id"], messages)
                return T.sentinel_interrupted(reason)

            if is_error:
                consecutive_errors += 1
            else:
                consecutive_errors = 0

            if consecutive_errors >= AGENT_MAX_ERRORS:
                if llm.escalate():
                    call_history.clear()
                    consecutive_errors = 0
                    _log(task, f"  {fn_name}({fn_args[:200]}) → {result[:300]}")
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                    for rtc in other_calls[i + 1:]:
                        messages.append({"role": "tool", "tool_call_id": rtc["id"],
                                         "content": "[escalating — skipped]"})
                    messages.append({"role": "user", "content": "[系统：连续错误，已切换模型，请继续]"})
                    _log(task, "Too many errors — escalated to next model")
                    tool_interrupted = True
                    break
                reason = T.too_many_errors(AGENT_MAX_ERRORS)
                print(f"  {warn('⚠')} {reason}")
                _log(task, f"⚠ {reason}")
                _save_messages(task["id"], messages)
                return T.sentinel_interrupted(reason)

            _log(task, f"  {fn_name}({fn_args[:200]}) → {result[:300]}")

            messages.append({
                "role":         "tool",
                "tool_call_id": tc_id,
                "content":      result,
            })

        if tool_interrupted:
            continue

    _log(task, f"⚠ exceeded max iterations {AGENT_MAX_ITERATIONS}")
    _save_messages(task["id"], messages)
    return T.sentinel_exceeded_max_iter()


# ── 内部工具 ───────────────────────────────────────────────

def _build_system(core: str) -> str:
    parts = ["You are Luclas, an experience-driven assistant."]
    if core.strip():
        parts.append(f"\n\n=== Core Policy ===\n{core}")
    return "\n".join(parts)


def _build_user_message(goal: str, task_context: str = "", parent_goal: str = "") -> str:
    """Task tree, prior-step status/results, and subtask framing go in the user
    message, not the system message — this is part of "what to do right now",
    and models respond more directly and literally to the user turn. The same
    content buried mid-way through a long system prompt is easy to treat as
    optional background rather than a binding constraint (see the case-828
    postmortem: subtasks repeatedly ignored completed prior results and
    re-fetched data from scratch until manually interrupted).
    goal is placed last, keeping "what to do now" in the most salient position.
    """
    parts = []
    if task_context.strip():
        parts.append(task_context.strip())
    if parent_goal:
        parts.append(
            f"=== Subtask execution mode ===\n"
            f"This is a subtask branched out via delegate_subtask; the calling task is: {parent_goal}\n"
            f"Prefer completing it directly via tool calls. Only call delegate_subtask again if a "
            f"genuinely independent chunk of this subtask is worth branching out further."
        )
    parts.append(f"=== Your task ===\n{goal}")
    return "\n\n".join(parts)


def _log(task: dict, text: str) -> None:
    task["log"] = task.get("log", "") + text + "\n"


def _handle_pause(task: dict, goal: str, messages: list) -> None:
    """
    首次 Ctrl-C：暂停并等待用户输入。
    - 用户输入指令 → 注入 messages，返回（调用方 continue 继续执行）
    - 直接回车     → 不注入，返回（调用方 continue 继续执行）
    - 再次 Ctrl-C  → 保存现场，raise KeyboardInterrupt（任务终止）
    """
    print(f"\n\n  {warn(T.paused_label())}  {dim(goal[:60])}")
    print(f"  {dim(T.paused_hint())}")
    try:
        user_input = input("  ▷ ").strip()
    except KeyboardInterrupt:
        print(f"\n  {err(T.task_stopped())}\n")
        _log(task, "User interrupted a second time, task stopped")
        _save_messages(task["id"], messages)
        raise
    if user_input:
        messages.append({"role": "user", "content": user_input})
        print(f"  {ok(T.resumed_with_input())}")
    else:
        print(f"  {ok(T.resumed())}")


def _is_stalled(history: list) -> bool:
    """P0-2 增强：检测精确循环 + 相似调用循环。
    精确循环：连续 N 次完全相同的 (fn, args_hash)
    相似循环：连续 N 次同一函数且参数关键字重叠度高
    """
    if len(history) < AGENT_STALL_WINDOW:
        return False
    window = history[-AGENT_STALL_WINDOW:]

    # 精确循环检测（原有逻辑）
    if len(set(window)) == 1:
        return True

    # 相似调用检测：同一函数名连续出现，参数 hash 高度相似
    fn_names = [h[0] for h in window]
    if len(set(fn_names)) == 1:
        # 同一函数名，检查参数 hash 是否不同但数量有限
        arg_hashes = [h[1] for h in window]
        unique_hashes = len(set(arg_hashes))
        # 窗口内恰好2种参数 → A/B交替模式，视为相似循环
        if unique_hashes == 2:
            return True

    return False


_DISPLAY_LIMIT = 500


def _truncate(text: str, limit: int = _DISPLAY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + T.more_chars(len(text))


def _tag_prefix(tag: str) -> str:
    return f"{dim('[' + tag + ']')} " if tag else ""


def _print_thinking(thinking: str, tag: str = "") -> None:
    head = _tag_prefix(tag)
    with _PRINT_LOCK:
        for line in thinking.strip().splitlines():
            if line.strip():
                print(f"{head}  {dim('💭 ' + line)}")


def _print_tool_call(fn_name: str, fn_args: str, tag: str = "") -> None:
    head = _tag_prefix(tag)
    with _PRINT_LOCK:
        print(f"{head}  {info('▶')} {info(fn_name)}")
        try:
            args = json.loads(fn_args) if fn_args else {}
            budget = _DISPLAY_LIMIT
            for k, v in args.items():
                if budget <= 0:
                    print(f"{head}      {dim('…')}")
                    break
                v_str = _truncate(str(v), budget)
                print(f"{head}      {dim(k + ':')} {v_str}")
                budget -= len(v_str)
        except Exception:
            if fn_args:
                print(f"{head}      {dim(_truncate(fn_args))}")


def _print_tool_result(result: str, is_error: bool, tag: str = "") -> None:
    head   = _tag_prefix(tag)
    icon   = ok("✓") if not is_error else err("✗")
    budget = _DISPLAY_LIMIT

    def _line(text: str) -> str:
        nonlocal budget
        text = str(text)
        if budget <= 0:
            return None
        out = _truncate(text, budget)
        budget -= len(out)
        return out

    with _PRINT_LOCK:
        try:
            d = json.loads(result)
            if isinstance(d, dict):
                if "output" in d:
                    lines = (d.get("output") or "").splitlines()
                    print(f"{head}  {icon} rc={d.get('rc','?')}")
                    for ln in lines:
                        s = _line(ln)
                        if s is None:
                            print(f"{head}      {dim(T.more_lines(len(lines)))}")
                            break
                        print(f"{head}      {s}")
                    return
                if "matches" in d:
                    print(f"{head}  {icon} {T.n_matches(d.get('count', 0))}")
                    for m in d["matches"]:
                        s = _line(m)
                        if s is None:
                            print(f"{head}      {dim('…')}")
                            break
                        print(f"{head}      {dim(s)}")
                    return
                if "files" in d:
                    print(f"{head}  {icon} {T.n_files(d.get('count', len(d['files'])))}")
                    for f in d["files"]:
                        s = _line(f)
                        if s is None:
                            print(f"{head}      {dim('…')}")
                            break
                        print(f"{head}      {dim(s)}")
                    return
                if "results" in d:
                    print(f"{head}  {icon} {T.n_memories(d.get('count', 0))}")
                    for m in d["results"]:
                        s = _line(str(m.get("content", "")))
                        if s is None:
                            print(f"{head}      {dim('…')}")
                            break
                        print(f"{head}      {dim(s)}")
                    return
                if "status" in d:
                    body = d.get("body", "")
                    body_str = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
                    print(f"{head}  {icon} HTTP {d['status']}")
                    if body_str:
                        print(f"{head}      {dim(_truncate(body_str))}")
                    return
        except Exception:
            pass

        lines = result.strip().splitlines()
        print(f"{head}  {icon}")
        for ln in lines:
            s = _line(ln)
            if s is None:
                print(f"{head}      {dim(T.more_lines(len(lines)))}")
                break
            print(f"{head}      {s}")


def _save_messages(task_id: str, messages: list) -> None:
    import json as _json
    msg_dir = os.path.join(SESSION_DIR, "messages")
    os.makedirs(msg_dir, exist_ok=True)
    path = os.path.join(msg_dir, f"{task_id}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(messages, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _strip_ansi(text: str) -> str:
    import re
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def _drain_supplements(q, messages: list, task: dict) -> None:
    """Pull any pending supplement messages and inject them as user turns."""
    import queue as _queue
    while True:
        try:
            msg = q.get_nowait()
            note = f"[用户补充信息] {msg}"
            messages.append({"role": "user", "content": note})
            _log(task, f"Supplement injected: {msg[:120]}")
        except _queue.Empty:
            break
