__version__ = "0.3.13"

import builtins
import datetime
import json
import os
import queue
import re
import readline
import sys
import threading
import uuid

from config import (CODE_DIR, BASE_DIR, DB_PATH, DATA_DIR, CORE_PATH, CORE_LOCAL_PATH, CORE_HIST, REFLECT_PATH,
                    RAW_DIR, SESSION_DIR, LLM_BASE_URL, LLM_MODEL, MODELS_CONFIG_PATH,
                    AGENT_MAX_ITERATIONS, VERSION_DATE)
from llm_client import LLMClient
from llm_router import ModelRouter, load_models


def _make_llm() -> LLMClient:
    models = load_models(MODELS_CONFIG_PATH)
    router = ModelRouter(models) if models else None
    if router:
        print(f"[router] loaded {len(models)} model(s)")
    return LLMClient(router=router)
from memory.database import init_db
from memory.store import MemoryStore
from memory.conversation_store import ConversationStore, TASK_EPISODE_TAG
from memory.episode_store import EpisodeStore
from tools.registry import build_tools
from tools.core_tools import load_core, core_update
from tools.user_input import set_channel_context, clear_channel_context, has_pending_question
from loops.task_runner import TaskRunner
from utils.display import ok, err, warn, info, dim, head, bold
import conversation_runner
import i18n as T

# One fixed conversation regardless of which terminal/process connects — CLI
# headless invocations (--run/--reflect, cron-launched terminal tasks) stay
# outside the conversation layer (see conversation_runner.py's module
# docstring); only the interactive REPL below uses this.
CLI_CONVERSATION_ID = "cli_local"

# conversation_id -> the supplement_queue a background dispatch_task is
# using, registered for the task's whole run (see _make_cli_dispatch).
# main()'s REPL loop only actually routes a typed line here when
# tools.user_input.has_pending_question() is also true (i.e. the task is
# genuinely sitting in ask_user() right now) — mirrors api.py's /chat
# routing for messaging channels; the rest of the time a typed line is a
# normal new conversation turn, letting the user keep chatting about other
# things while the background task runs quietly.
_cli_pending_queues: dict = {}


_ANSI_RE     = re.compile(r'\x1b\[[0-9;]*m')
_log_file    = None
_real_print  = None   # the un-patched builtins.print, saved by _start_print_logger()
_PID_FILE    = os.path.join(DATA_DIR, "luclas.pid")
_ACTIVE_FILE = os.path.join(DATA_DIR, "last_active")


def _write_pid() -> None:
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid() -> None:
    try:
        os.unlink(_PID_FILE)
    except FileNotFoundError:
        pass


def _touch_active() -> None:
    with open(_ACTIVE_FILE, "w") as f:
        f.write(datetime.datetime.now().isoformat())


def _reflect_goal() -> str:
    protocol = ""
    if os.path.isfile(REFLECT_PATH):
        with open(REFLECT_PATH, encoding="utf-8") as f:
            protocol = f"\n\n---\n{f.read()}"
    return (
        "Perform a comprehensive strategic reflection on Luclas's recent performance. "
        "Follow the protocol below exactly — do not skip data collection. "
        "Do not modify any .py files or suggest code changes."
        + protocol
    )


def _run_headless(goal: str) -> None:
    """Non-interactive single-task run (--run / --reflect). Exits when done."""
    for d in [RAW_DIR, SESSION_DIR,
              os.path.join(SESSION_DIR, "logs"),
              os.path.join(SESSION_DIR, "messages"),
              CORE_HIST]:
        os.makedirs(d, exist_ok=True)

    init_db()
    _start_print_logger()
    # Deliberately NOT calling _cleanup_interrupted_state() here (unlike the
    # interactive main() and api.py's own startup) — this function runs on
    # every single cron-triggered `--run`/`--reflect` invocation, which is
    # routine and can happen *while api.py is running as a persistent
    # service* (they're designed to coexist, not mutually exclusive like the
    # interactive CLI vs cron). _cleanup_interrupted_state() unconditionally
    # deletes task_state memories — it can't tell "stale from a crash" apart
    # from "genuinely in progress right now in api.py". Running it here
    # would let a scheduled reminder or nightly reflection randomly clobber
    # a live, unrelated in-progress task (e.g. a WeChat conversation) just
    # from bad timing. Zombie cleanup for a genuinely-headless-only
    # deployment (no interactive CLI, no api.py ever run) still happens
    # eventually the next time either of those starts — a real but narrow
    # gap, and strictly safer than the alternative.

    session_id  = uuid.uuid4().hex[:8]
    llm         = _make_llm()
    store       = MemoryStore()
    schemas, fns = build_tools(store)
    runner = TaskRunner(
        llm=llm, schemas=schemas, fns=fns,
        mem_store=store, session_id=session_id,
    )

    print(f"\n[headless {datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {goal[:80]}")
    try:
        runner.run(goal, on_result=lambda r: print(f"\n[done] {r[:300]}"))
    except Exception as e:
        print(f"\n[error] {e}")
    finally:
        _stop_print_logger()


def main():
    for d in [RAW_DIR, SESSION_DIR,
              os.path.join(SESSION_DIR, "logs"),
              os.path.join(SESSION_DIR, "messages"),
              CORE_HIST]:
        os.makedirs(d, exist_ok=True)

    init_db()
    _start_print_logger()
    _cleanup_interrupted_state()
    _migrate_embeddings()
    _ensure_cron()

    session_id  = CLI_CONVERSATION_ID
    llm         = _make_llm()
    store       = MemoryStore()
    schemas, fns = build_tools(store)

    runner = TaskRunner(
        llm=llm, schemas=schemas, fns=fns,
        mem_store=store, session_id=session_id,
    )

    conv_store    = ConversationStore()
    episode_store = EpisodeStore()
    dispatch_fn   = _make_cli_dispatch(conv_store, episode_store, llm, store, schemas, fns)

    # 启动检查 core.md（core.local.md 存在则优先用它，不需要 bootstrap）
    if not os.path.isfile(CORE_PATH) and not os.path.isfile(CORE_LOCAL_PATH):
        print(warn(T.core_missing()))
        _bootstrap_core(llm)

    _history_file = os.path.join(SESSION_DIR, ".luclas_history")
    try:
        readline.read_history_file(_history_file)
    except FileNotFoundError:
        pass
    readline.set_history_length(500)

    _write_pid()
    _touch_active()

    print(head(T.ascii_banner()))
    print(f"  {bold(T.author_line())}")
    print(f"  {dim(T.version_line(__version__, VERSION_DATE))}")
    print(f"\n  {T.identity_line()}")
    print(f"  {dim(T.tips_line())}")
    print(f"\n{dim(T.startup_hint())}")
    avail = ok(T.online()) if llm.is_available() else err(T.offline())
    print(T.status_line(avail, store.count()))
    print(T.session_id_line(dim(session_id)))
    print()

    while True:
        try:
            line = input("LUC > ").strip()
        except (EOFError, KeyboardInterrupt):
            readline.write_history_file(_history_file)
            print(T.goodbye_nl())
            break

        if not line:
            continue

        _touch_active()

        if line.lower() in ("exit", "quit", "q"):
            readline.write_history_file(_history_file)
            print(T.goodbye_nl())
            break

        if line.startswith("/"):
            try:
                _handle_slash(line, llm, store, schemas, fns, runner)
            except SystemExit:
                readline.write_history_file(_history_file)
                _stop_print_logger()
                _remove_pid()
                raise
            continue

        # A background dispatch_task is sitting in ask_user() right now —
        # this line is the answer to that question, not a new conversation
        # topic. Must be checked before _run_conversation_turn(), or the
        # answer gets processed as an unrelated fresh turn while ask_user()
        # just burns its timeout waiting on a queue nothing ever reaches.
        pending_q = _cli_pending_queues.get(CLI_CONVERSATION_ID)
        if pending_q is not None and has_pending_question(CLI_CONVERSATION_ID):
            pending_q.put(line)
            continue

        _run_conversation_turn(line, llm, store, episode_store, conv_store, dispatch_fn)

    _stop_print_logger()
    _remove_pid()


def _run_task(goal: str, runner: TaskRunner):
    print(f"\n{head(T.task_started())}")
    try:
        runner.run(goal, on_result=lambda r: print(f"\n{head(T.task_done())}\n{r}\n"))
    except KeyboardInterrupt:
        print(T.task_interrupted())
    except Exception as e:
        print(err(T.task_exception(e)))
        print()


def _make_cli_dispatch(conv_store, episode_store, llm, store, schemas, fns):
    """dispatch_task's implementation for the CLI's persistent conversation.
    Unlike api.py (a fresh TaskRunner+LLMClient per HTTP request already),
    the CLI's main() keeps one shared TaskRunner/LLMClient alive for its
    whole REPL session — reusing that same LLMClient from a background
    dispatch thread while the main thread might also be mid-turn would hit
    the exact _model_queue/_current_idx race LLMClient.clone() exists to
    avoid (see llm_client.py), so each dispatch gets its own fresh
    TaskRunner+LLMClient here instead, mirroring api.py's _run_task.

    Foreground dispatch runs synchronously on the main thread (nothing else
    is reading stdin meanwhile), so its ask_user() can safely fall through
    to a plain blocking input() — tools/user_input.py's tty branch, unchanged.
    Background dispatch runs on its own thread while the main thread is
    still sitting in its own input("LUC > ") call — without wiring a real
    channel context here, ask_user() would *also* fall through to that same
    tty branch and call input() from the background thread too: two threads
    blocked reading the same stdin at once, an actual race, not just a
    missed answer like the messaging-channel version of this bug. So
    background dispatch gets a real push (prints to the terminal) and
    wait_queue — registered in _cli_pending_queues so main()'s REPL loop can
    route the next typed line there instead of starting a new conversation
    turn, exactly like api.py's /chat does for a pending question.
    """
    def dispatch(session_id: str, memory_id: str, goal: str, foreground: bool) -> dict:
        task_llm = LLMClient(router=llm._router if llm else None)
        q = queue.Queue()
        task_runner = TaskRunner(
            llm=task_llm, schemas=schemas, fns=fns,
            mem_store=store, session_id=session_id,
            supplement_queue=q,
        )

        def _run(record_in_history: bool) -> str:
            if record_in_history:
                _cli_pending_queues[session_id] = q
                set_channel_context(
                    push=lambda msg: print(f"\n{warn(T.ask_user_label())} {msg}\n"),
                    wait_queue=q, session_id=session_id,
                )
            print(f"\n{head(T.task_started())}")
            try:
                result = task_runner.run(goal, on_result=lambda r: print(f"\n{head(T.task_done())}\n{r}\n"))
            except KeyboardInterrupt:
                result = T.task_interrupted()
                print(result)
            except Exception as e:
                result = str(e)
                print(err(T.task_exception(e)))
                print()
            finally:
                if record_in_history:
                    clear_channel_context()
                    _cli_pending_queues.pop(session_id, None)
            episode_store.create_task_episode(memory_id, uuid.uuid4().hex[:12], result, importance=7)
            conv_store.clear_active_task(session_id)
            if record_in_history:
                # Background dispatch: handle_turn() already returned (its
                # own reply was the immediate ack, e.g. "好的，我这就去处理"),
                # so this result needs its own append to actually land in the
                # conversation — unlike the foreground path, where handle_turn()
                # is still on the call stack and appends reply_text itself
                # once dispatch_fn() returns (see conversation_runner.py:
                # already_delivered=True). Without this, a background task's
                # outcome would only ever be visible in the printed terminal
                # output, never recalled naturally in later conversation turns.
                conv_store.append_message(memory_id, "assistant", result, episode_id=TASK_EPISODE_TAG)
            return result

        task_id = uuid.uuid4().hex[:12]
        conv_store.set_active_task(session_id, task_id, foreground)
        if foreground:
            return {"delivered": True, "result": _run(record_in_history=False)}
        threading.Thread(target=_run, kwargs={"record_in_history": True}, daemon=True).start()
        return {"delivered": False, "started": True, "task_id": task_id}

    return dispatch


def _run_conversation_turn(message: str, llm, store, episode_store, conv_store, dispatch_fn):
    try:
        reply, already_delivered = conversation_runner.handle_turn(
            CLI_CONVERSATION_ID, CLI_CONVERSATION_ID, message, llm=llm, mem_store=store,
            episode_store=episode_store, conv_store=conv_store,
            dispatch_fn=dispatch_fn,
        )
        if reply and not already_delivered:
            print(f"\n{reply}\n")
    except KeyboardInterrupt:
        print(T.task_interrupted())
    except Exception as e:
        print(err(T.task_exception(e)))
        print()


# ── 斜杠命令 ──────────────────────────────────────────────

def _handle_slash(line: str, llm, store, schemas, fns, runner=None):
    parts = line[1:].split(None, 2)
    cmd  = parts[0].lower() if parts else ""
    sub  = parts[1].lower() if len(parts) > 1 else ""
    rest = parts[2]         if len(parts) > 2 else ""

    if cmd in ("q", "quit", "exit"):
        print(T.goodbye())
        _stop_print_logger()
        raise SystemExit(0)

    elif cmd == "help":
        print(T.help_text())

    elif cmd == "status":
        _show_status(store)

    elif cmd == "whoami":
        _show_whoami(llm, store)

    elif cmd == "core":
        _core_cmd(sub, rest)

    elif cmd == "memory":
        if sub == "search":
            _memory_cmd(f"search {rest}", store)
        else:
            _memory_cmd("", store)

    elif cmd == "reset":
        _do_reset(store)

    elif cmd == "reflect":
        if runner is None:
            print(warn("reflect not available in this context"))
        else:
            _reflect_cmd(runner)

    elif cmd == "models":
        if sub == "edit":
            from model_manager import run as _edit_models
            _edit_models()
            llm.reload_router()
        else:
            _show_models(llm)

    elif cmd == "schedule":
        _schedule_cmd(sub, rest)

    elif cmd == "api":
        if sub == "restart":
            restarted, msg = _restart_api_service()
            print((ok(msg) if restarted else err(msg)))
        else:
            print(err(T.api_usage()))

    else:
        print(warn(T.unknown_command(cmd)))


# ── 命令实现 ──────────────────────────────────────────────

def _show_status(store: MemoryStore):
    print(f"\n{head(T.status_title())}")
    print(T.status_memory(store.count()))
    snap_count = len(os.listdir(CORE_HIST)) if os.path.isdir(CORE_HIST) else 0
    print(T.status_policy_versions(snap_count))
    from llm_client import usage_summary
    u = usage_summary(days=1)
    print(T.status_token_usage(u["calls"], u["total_tokens"], u["all_time_calls"], u["all_time_total_tokens"]))
    print()


def _show_whoami(llm: LLMClient, store: MemoryStore):
    print(f"\n{head(T.whoami_title())}")
    print(T.whoami_model(LLM_MODEL))
    print(T.whoami_endpoint(LLM_BASE_URL))
    print(T.whoami_db(DB_PATH))
    print(T.whoami_core(CORE_LOCAL_PATH if os.path.isfile(CORE_LOCAL_PATH) else CORE_PATH))
    print(T.whoami_max_iter(AGENT_MAX_ITERATIONS))
    avail = ok(T.online()) if llm.is_available() else err(T.offline())
    print(T.whoami_llm_status(avail))
    print(T.whoami_time(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    print()


def _show_models(llm: LLMClient):
    from llm_router import load_models
    models = load_models(MODELS_CONFIG_PATH)
    if not models:
        # Fallback: show the single env-var model
        print(f"\n{head(T.models_title(1))}")
        status = ok(T.online()) if llm.is_available() else err(T.offline())
        print(f"  [default]  {llm.model}  {status}")
        print(f"  endpoint:  {llm.base_url}")
        print()
        return

    print(f"\n{head(T.models_title(len(models)))}")
    active_name = llm.model  # currently active default (no goal set yet)
    for m in models:
        marker = "◉" if m.classifier else "○"
        types  = ", ".join(m.task_types) if m.task_types != ["general"] else "general"
        complexity_label = {"low": "低", "mid": "中", "high": "高"}.get(m.complexity, m.complexity)
        print(
            f"  [{marker}] {m.id:<18} {m.name:<32}"
            f"  complexity={m.complexity}({complexity_label})"
            f"  priority={m.priority}"
        )
        print(f"       {dim(m.base_url)}  types: {types}")
        if m.classifier:
            print(f"       {dim('(used as classifier for task classification)')}")
    print()


def _core_cmd(sub: str, rest: str):
    from tools.core_tools import list_core_snapshots, read_core_snapshot

    if sub == "history" and rest:
        # 查看某个历史版本
        name = rest if rest.endswith(".md") else rest + ".md"
        try:
            content = read_core_snapshot(name)
        except FileNotFoundError:
            print(err(T.snapshot_not_found(rest)))
            return
        print(head(T.snapshot_title(rest)))
        print(content)
    elif sub == "history":
        # 列出历史
        snaps = list_core_snapshots()
        if not snaps:
            print(T.no_snapshots())
            return
        print(head(T.snapshots_title(len(snaps))))
        for s in snaps:
            print(f"  {dim(s['name'])}  {s['reason']}")
        print()
    else:
        # 显示当前策略
        core = load_core()
        if not core:
            print(warn(T.core_missing_warn()))
            return
        print(head(T.current_core_title()))
        print(core)
        print()


def _memory_cmd(sub: str, store: MemoryStore):
    if sub.startswith("search "):
        kw = sub[7:].strip()
        results = store.search(query=kw, limit=20)
        if not results:
            print(warn(T.memory_not_found(kw)))
            return
        print(head(T.memory_search_title(kw, len(results))))
        for m in results:
            tags = "、".join(m["tags"][:5])
            print(f"  [{m['id']}] {info(m['type'] or '-')} ★{m['importance']} {m['content'][:100]}")
            print(f"       {dim(tags or T.no_tags())}")
        print()
    else:
        entries = store.get_all(limit=30)
        if not entries:
            print(T.memory_empty())
            return
        from collections import Counter
        all_entries = store.get_all(limit=9999)
        type_counts = Counter(e["type"] or T.untyped() for e in all_entries)
        stats = "  ".join(f"{k}:{v}" for k, v in type_counts.most_common())
        print(head(T.memory_store_title(store.count(), dim(stats))))
        for m in entries:
            tags = "、".join(m["tags"][:4])
            print(f"  [{m['id']}] {info(m['type'] or '-')} ★{m['importance']} {m['content'][:80]}")
            print(f"       {dim(tags or T.no_tags())}")
        print()


def _do_reset(store: MemoryStore):
    confirm = input(T.reset_confirm()).strip().lower()
    if confirm != "yes":
        print(T.reset_cancelled())
        return
    import sqlite3
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM episodes")
        conn.execute("DELETE FROM conversations")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("VACUUM")
    conn.close()
    history = os.path.join(SESSION_DIR, ".luclas_history")
    if os.path.isfile(history):
        os.unlink(history)
    print(ok(T.reset_done()))


def _reflect_cmd(runner: TaskRunner):
    print(f"\n{head(T.reflect_title())}")
    print(dim(T.reflect_hint()))
    print()
    _run_task(_reflect_goal(), runner)


def _schedule_cmd(sub: str, rest: str):
    from memory.schedule import ScheduledTaskStore
    sched = ScheduledTaskStore()

    if not sub or sub == "list":
        tasks = sched.list_all()
        if not tasks:
            print(T.schedule_empty())
            return
        print(head(T.schedule_title(len(tasks))))
        for t in tasks:
            status   = ok("on ") if t["enabled"] else dim("off")
            schedule = t["schedule_type"]
            if t["schedule_type"] == "weekly":
                schedule += f" {t['schedule_day']}"
            schedule += f" {t['schedule_time']}"
            last = t["last_run"] or T.schedule_never()
            print(f"  [{t['id']}] {status} {bold(t['name'])}  @ {schedule}")
            print(f"       {dim(t['goal'][:70])}")
            print(f"       {dim(T.schedule_last_run(last))}")
        print()

    elif sub == "add":
        print(head(T.schedule_add_title()))
        name = input(f"  {T.schedule_prompt_name()} ").strip()
        if not name:
            print(T.schedule_cancelled()); return
        goal = input(f"  {T.schedule_prompt_goal()} ").strip()
        if not goal:
            print(T.schedule_cancelled()); return
        stype = ""
        while stype not in ("daily", "weekly"):
            stype = input(f"  {T.schedule_prompt_freq()} ").strip().lower()
            if not stype:
                print(T.schedule_cancelled()); return
        sday = ""
        if stype == "weekly":
            valid_days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
            while sday not in valid_days:
                sday = input(f"  {T.schedule_prompt_day()} ").strip().lower()
                if not sday:
                    print(T.schedule_cancelled()); return
        stime = ""
        while not re.match(r'^\d{2}:\d{2}$', stime):
            stime = input(f"  {T.schedule_prompt_time()} ").strip()
            if not stime:
                print(T.schedule_cancelled()); return
        id_ = sched.add(name, goal, stype, stime, sday)
        print(ok(T.schedule_added(id_, name, stype, sday, stime)))

    elif sub in ("on", "off"):
        if not rest:
            print(warn(f"Usage: /schedule {sub} <id>")); return
        if sched.toggle(rest, sub == "on"):
            print(ok(T.schedule_toggled(rest, sub == "on")))
        else:
            print(err(T.schedule_not_found(rest)))

    elif sub == "del":
        if not rest:
            print(warn("Usage: /schedule del <id>")); return
        confirm = input(T.schedule_del_confirm(rest)).strip().lower()
        if confirm == "y":
            if sched.delete(rest):
                print(ok(T.schedule_deleted(rest)))
            else:
                print(err(T.schedule_not_found(rest)))
        else:
            print(T.schedule_cancelled())

    else:
        print(warn(T.unknown_command(f"schedule {sub}")))


def _bootstrap_core(llm: LLMClient):
    user_dir_path = os.path.join(DATA_DIR, "user_direction.md")
    user_section = ""
    if os.path.isfile(user_dir_path):
        with open(user_dir_path, encoding="utf-8") as f:
            user_section = f"\n\nUser context (from setup):\n{f.read()}"

    prompt = f"""You are Luclas, an experience-driven assistant. Generate your own core policy file (core.md).

Content should include:
- Identity description
- Work strategy (what to do upon receiving a task)
- Learning strategy (how to extract and store knowledge)
- Memory strategy (memory types, importance criteria, tagging conventions)
- Retrieval strategy (when and how to search memory)
- Long-text handling strategy
- Policy update rules

Requirements: concise and actionable, every rule should directly guide behavior, avoid empty statements.{user_section}"""
    try:
        content = llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
        with open(CORE_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        print(ok(T.core_generated()))
    except Exception as e:
        print(err(T.core_generate_failed(e)))
        _write_default_core()


def _write_default_core():
    default = """# Luclas Core Policy

## Identity
You are Luclas, an experience-driven assistant.

## Work strategy
1. Use memory_search first to check for relevant memory
2. Decide whether tools are needed based on existing knowledge
3. When you don't know something: check memory → search the web → tell the user

## Learning strategy
After completing a task, extract knowledge into memory. Be specific, tag accurately, avoid duplicates.

## Memory strategy
type: fact/experience/workflow/opinion/keypoint
importance: 1-10

## Retrieval strategy
Search before starting a task; try multiple keyword angles.

## Long-text handling
Read in chunks, extract key points into memory, don't store raw text.

## Policy updates
When you discover a better approach, use core_update to update this file.
"""
    with open(CORE_PATH, "w", encoding="utf-8") as f:
        f.write(default)


# ── 日志 ──────────────────────────────────────────────────

def _start_print_logger():
    global _log_file, _real_print
    log_dir  = os.path.join(SESSION_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    _log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    # Saved at module level (not just as a local closure variable) so
    # _stop_print_logger() can actually restore it later.
    _real_print = builtins.print
    def _logged(*args, **kwargs):
        _real_print(*args, **kwargs)
        sep  = kwargs.get("sep",  " ")
        end  = kwargs.get("end",  "\n")
        line = sep.join(str(a) for a in args) + end
        _log_file.write(_ANSI_RE.sub("", line))
    builtins.print = _logged


def _cleanup_interrupted_state() -> None:
    """启动时清理上次崩溃留下的过期任务状态记忆（_write_mem 写的临时
    "task_state" 记忆，正常结束会被 _cleanup_mem 删掉，只有崩溃/被杀掉的
    进程才会遗留）。"""
    from memory.store import MemoryStore
    mem = MemoryStore()
    stale = mem.search(type="task_state", limit=100)
    for m in stale:
        mem.delete(m["id"])
    if stale:
        print(warn(T.cleaned_stale_memories(len(stale))))


def _migrate_embeddings() -> None:
    """启动时为旧记忆补全 embedding（首次运行会加载模型，稍慢）。"""
    from memory.store import MemoryStore
    store = MemoryStore()
    # 快速检查：有无缺 embedding 的记录
    from memory.database import get_conn
    with get_conn() as conn:
        missing = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NULL"
        ).fetchone()[0]
    if not missing:
        return
    print(info(T.embedding_migrating(missing)))
    n = store.migrate_embeddings()
    print(ok(T.embedding_migrated(n)))


def _ensure_cron() -> None:
    """确保 cron_runner.py 已注册到 crontab，没有则自动添加。"""
    import subprocess
    cron_entry = f"* * * * * /usr/bin/python3 {CODE_DIR}/cron_runner.py >> {DATA_DIR}/sessions/logs/cron.log 2>&1"
    try:
        try:
            existing = subprocess.check_output(["crontab", "-l"], stderr=subprocess.DEVNULL).decode()
        except subprocess.CalledProcessError:
            existing = ""
        if "cron_runner.py" not in existing:
            new_crontab = existing.rstrip("\n") + "\n" + cron_entry + "\n"
            subprocess.run(["crontab", "-"], input=new_crontab.encode(), check=True)
            print(ok("✓ cron_runner registered in crontab"))
    except FileNotFoundError:
        # No `crontab` binary on this system — scheduled tasks/nightly reflection
        # just won't run automatically. Not a reason to crash every interactive
        # startup over (this runs unconditionally at the top of main()).
        print(warn("⚠ crontab not found — scheduled tasks and nightly reflection won't run automatically"))
    except Exception as e:
        print(warn(f"⚠ could not register cron_runner in crontab: {e}"))


def _restart_api_service() -> tuple[bool, str]:
    """Restart the background systemd --user service running api.py (the HTTP API
    that messaging adapters talk to). Returns (ok, message).
    """
    import subprocess
    from config import API_SERVICE_NAME
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", API_SERVICE_NAME],
            check=True, capture_output=True, text=True, timeout=15,
        )
        return True, T.api_restart_ok(API_SERVICE_NAME)
    except FileNotFoundError:
        return False, T.api_restart_no_systemctl()
    except subprocess.TimeoutExpired:
        return False, T.api_restart_timeout()
    except subprocess.CalledProcessError as e:
        return False, T.api_restart_failed(API_SERVICE_NAME, (e.stderr or str(e)).strip())


def _stop_print_logger():
    global _log_file, _real_print
    # 找回原始 print（模块级保存的那个）——之前这里用
    # `builtins.__class__.__dict__.get("print", print)` 试图找回，但模块对象的
    # __class__（types.ModuleType）根本不含 "print"，永远落到默认值 print，而
    # 那时 print 早已被 patch 成 _logged 自己，等于什么也没恢复，_log_file 置
    # None 后后续任何 print() 都会因为 _logged 里 _log_file.write(...) 报错。
    if _log_file:
        path = _log_file.name
        _log_file.close()
        _log_file = None
        if _real_print is not None:
            builtins.print = _real_print
            _real_print = None
        sys.stdout.write(T.log_saved(path))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "setup":
        from setup import run as _run_setup
        _run_setup(BASE_DIR)
    elif args and args[0] == "--reflect":
        _run_headless(_reflect_goal())
    elif args and args[0] == "--run" and len(args) > 1:
        _run_headless(args[1])
    elif args and args[0] == "api" and len(args) > 1 and args[1] == "restart":
        restarted, msg = _restart_api_service()
        print(ok(msg) if restarted else err(msg))
        sys.exit(0 if restarted else 1)
    elif not args and not os.path.isfile(os.path.join(BASE_DIR, ".env")):
        print(warn(T.first_run_setup_hint()))
        from setup import run as _run_setup
        _run_setup(BASE_DIR)
    else:
        main()
