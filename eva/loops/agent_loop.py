"""
loops/agent_loop.py — 核心执行循环

极简设计：调用 LLM → 执行工具 → 循环，直到 LLM 给出最终回答。
所有决策（何时停止、如何拆解、何时存记忆）由 LLM 根据 core.md 策略自主决定。
"""

import hashlib
import json
import os
from llm_client import LLMClient
from tools.registry import execute_tool
from tools.core_tools import load_core
from config import AGENT_MAX_ITERATIONS, AGENT_STALL_WINDOW, AGENT_MAX_ERRORS, SESSION_DIR
from utils.display import dim, info, ok, err, warn
import i18n as T

_ANSI_RE = None


def run_agent(goal: str, task: dict, llm: LLMClient,
              schemas: list, fns: dict,
              task_context: str = "", parent_goal: str = "",
              progress_callback=None) -> str:
    """
    执行一个目标，返回最终回答。
    task: dict with id, goal, log (mutated in-place)
    task_context: 工作历史字符串，注入 system prompt
    parent_goal: 父任务目标（子任务时传入）
    """
    core = load_core()
    system_prompt = _build_system(core, task_context, parent_goal)
    messages = [
        {"role": "system",  "content": system_prompt},
        {"role": "user",    "content": goal},
    ]

    call_history:   list[tuple] = []
    consecutive_errors = 0

    _log(task, f"\n=== Goal: {goal} ===")

    for iteration in range(1, AGENT_MAX_ITERATIONS + 1):
        print(T.round_header(iteration, AGENT_MAX_ITERATIONS))
        if iteration == 1:
            if parent_goal and parent_goal != goal:
                print(f"  │  {dim(parent_goal[:70])}")
                print(f"  │  {'  › ' + info(goal[:66])}")
            else:
                print(f"  │  {info(goal[:70])}")

        try:
            turn = llm.agent_turn(messages, schemas)
        except KeyboardInterrupt:
            _handle_pause(task, goal, messages)  # 二次 Ctrl-C 时内部 raise
            continue
        except RuntimeError as e:
            print(f"  {err('✗')} {T.llm_call_failed(e)}")
            _log(task, T.llm_call_failed(e))
            break

        thinking   = turn.get("content") or ""
        tool_calls = turn.get("tool_calls") or []

        if thinking.strip():
            _print_thinking(thinking)
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

        tool_interrupted = False
        for i, tc in enumerate(tool_calls):
            fn_name = tc["function"]["name"]
            fn_args = tc["function"].get("arguments", "{}")
            tc_id   = tc["id"]

            _print_tool_call(fn_name, fn_args)

            try:
                result, is_error = execute_tool(fn_name, fn_args, fns)
            except KeyboardInterrupt:
                # 补全本轮所有未完成的 tool result，否则 LLM API 会报错
                messages.append({"role": "tool", "tool_call_id": tc_id,
                                  "content": T.sentinel_paused_by_tool()})
                for rtc in tool_calls[i + 1:]:
                    messages.append({"role": "tool", "tool_call_id": rtc["id"],
                                     "content": T.sentinel_skipped()})
                _handle_pause(task, goal, messages)  # 二次 Ctrl-C 时内部 raise
                tool_interrupted = True
                break

            _print_tool_result(result, is_error)

            # 卡死检测
            args_hash = hashlib.md5(fn_args.encode()).hexdigest()
            call_history.append((fn_name, args_hash))
            if _is_stalled(call_history):
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
                reason = T.too_many_errors(AGENT_MAX_ERRORS)
                print(f"  {warn('⚠')} {reason}")
                _log(task, f"⚠ {reason}")
                _save_messages(task["id"], messages)
                return T.sentinel_interrupted(reason)

            icon = ok("✓") if not is_error else err("✗")
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

def _build_system(core: str, task_context: str = "", parent_goal: str = "") -> str:
    parts = ["You are EVA4, an experience-driven assistant."]
    if core.strip():
        parts.append(f"\n\n=== Core Policy ===\n{core}")
    if task_context.strip():
        parts.append(f"\n\n{task_context}")
    if parent_goal:
        parts.append(
            f"\n\n=== Subtask execution mode ===\n"
            f"You are executing an atomic subtask that has already been decomposed; the parent task is: {parent_goal}\n"
            f"Focus on completing only the one subtask specified in the user message, calling tools directly. Do not decompose further."
        )
    return "\n".join(parts)


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


def _print_thinking(thinking: str) -> None:
    for line in thinking.strip().splitlines():
        if line.strip():
            print(f"  {dim('💭 ' + line)}")


def _print_tool_call(fn_name: str, fn_args: str) -> None:
    print(f"  {info('▶')} {info(fn_name)}")
    try:
        args = json.loads(fn_args) if fn_args else {}
        budget = _DISPLAY_LIMIT
        for k, v in args.items():
            if budget <= 0:
                print(f"      {dim('…')}")
                break
            v_str = _truncate(str(v), budget)
            print(f"      {dim(k + ':')} {v_str}")
            budget -= len(v_str)
    except Exception:
        if fn_args:
            print(f"      {dim(_truncate(fn_args))}")


def _print_tool_result(result: str, is_error: bool) -> None:
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

    try:
        d = json.loads(result)
        if isinstance(d, dict):
            if "output" in d:
                lines = (d.get("output") or "").splitlines()
                print(f"  {icon} rc={d.get('rc','?')}")
                for ln in lines:
                    s = _line(ln)
                    if s is None:
                        print(f"      {dim(T.more_lines(len(lines)))}")
                        break
                    print(f"      {s}")
                return
            if "matches" in d:
                print(f"  {icon} {T.n_matches(d.get('count', 0))}")
                for m in d["matches"]:
                    s = _line(m)
                    if s is None:
                        print(f"      {dim('…')}")
                        break
                    print(f"      {dim(s)}")
                return
            if "files" in d:
                print(f"  {icon} {T.n_files(d.get('count', len(d['files'])))}")
                for f in d["files"]:
                    s = _line(f)
                    if s is None:
                        print(f"      {dim('…')}")
                        break
                    print(f"      {dim(s)}")
                return
            if "results" in d:
                print(f"  {icon} {T.n_memories(d.get('count', 0))}")
                for m in d["results"]:
                    s = _line(str(m.get("content", "")))
                    if s is None:
                        print(f"      {dim('…')}")
                        break
                    print(f"      {dim(s)}")
                return
            if "status" in d:
                body = d.get("body", "")
                body_str = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
                print(f"  {icon} HTTP {d['status']}")
                if body_str:
                    print(f"      {dim(_truncate(body_str))}")
                return
    except Exception:
        pass

    lines = result.strip().splitlines()
    print(f"  {icon}")
    for ln in lines:
        s = _line(ln)
        if s is None:
            print(f"      {dim(T.more_lines(len(lines)))}")
            break
        print(f"      {s}")


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
