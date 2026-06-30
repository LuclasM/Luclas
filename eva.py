import builtins
import datetime
import json
import os
import re
import readline
import sys
import uuid

from config import (DB_PATH, CORE_PATH, CORE_LOCAL_PATH, CORE_HIST, RAW_DIR, SESSION_DIR,
                    LLM_BASE_URL, LLM_MODEL, AGENT_MAX_ITERATIONS)
from llm_client import LLMClient
from memory.database import init_db
from memory.store import MemoryStore, TaskStore
from memory.task_memory import TaskMemory
from tools.registry import build_tools
from tools.core_tools import load_core, core_update
from loops.task_runner import TaskRunner
from utils.display import ok, err, warn, info, dim, head, bold
import i18n as T


_ANSI_RE  = re.compile(r'\x1b\[[0-9;]*m')
_log_file = None


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

    session_id  = uuid.uuid4().hex[:8]
    llm         = LLMClient()
    store       = MemoryStore()
    task_store  = TaskStore()
    task_memory = TaskMemory()
    schemas, fns = build_tools(store)

    runner = TaskRunner(
        llm=llm, schemas=schemas, fns=fns,
        task_store=task_store, task_memory=task_memory,
        mem_store=store, session_id=session_id,
    )

    # 启动检查 core.md（core.local.md 存在则优先用它，不需要 bootstrap）
    if not os.path.isfile(CORE_PATH) and not os.path.isfile(CORE_LOCAL_PATH):
        print(warn(T.core_missing()))
        _bootstrap_core(llm)

    _history_file = os.path.join(SESSION_DIR, ".eva_history")
    try:
        readline.read_history_file(_history_file)
    except FileNotFoundError:
        pass
    readline.set_history_length(500)

    print(head(T.startup_title()))
    avail = ok(T.online()) if llm.is_available() else err(T.offline())
    tm_counts = task_memory.count()
    n_running = tm_counts.get("running", 0)
    running_label = f"  {warn(T.running_count(n_running))}" if n_running else ""
    print(T.status_line(avail, store.count(), tm_counts['active'], tm_counts['archived'], running_label))
    active = task_store.list_active()
    if active:
        print(warn(T.unfinished_tasks(len(active))))
    print(T.session_id_line(dim(session_id)))
    print()

    while True:
        try:
            line = input("EVA > ").strip()
        except (EOFError, KeyboardInterrupt):
            readline.write_history_file(_history_file)
            print(T.goodbye_nl())
            break

        if not line:
            continue

        if line.startswith("/"):
            try:
                _handle_slash(line, llm, store, task_store, task_memory, schemas, fns)
            except SystemExit:
                readline.write_history_file(_history_file)
                _stop_print_logger()
                raise
            continue

        _run_task(line, runner)

    _stop_print_logger()


def _run_task(goal: str, runner: TaskRunner):
    print(f"\n{head(T.task_started())}")
    try:
        result = runner.run(goal)
        print(f"\n{head(T.task_done())}\n{result}\n")
    except KeyboardInterrupt:
        print(T.task_interrupted())
    except Exception as e:
        print(err(T.task_exception(e)))
        print()


# ── 斜杠命令 ──────────────────────────────────────────────

def _handle_slash(line: str, llm, store, task_store, task_memory, schemas, fns):
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
        _show_status(store, task_store, task_memory)

    elif cmd == "whoami":
        _show_whoami(llm, store)

    elif cmd == "core":
        _core_cmd(sub, rest)

    elif cmd == "memory":
        if sub == "search":
            _memory_cmd(f"search {rest}", store)
        else:
            _memory_cmd("", store)

    elif cmd == "tasks":
        _show_tasks(task_store)

    elif cmd == "history":
        _show_history(task_memory)

    elif cmd == "log":
        tid = sub or rest
        if not tid:
            print(err(T.log_usage()))
        else:
            _show_log(tid, task_store)

    elif cmd == "reset":
        _do_reset(store, task_store, task_memory)

    else:
        print(warn(T.unknown_command(cmd)))


# ── 命令实现 ──────────────────────────────────────────────

def _show_status(store: MemoryStore, task_store: TaskStore, task_memory: TaskMemory):
    print(f"\n{head(T.status_title())}")
    print(T.status_memory(store.count()))
    active = task_store.list_active()
    print(T.status_active_tasks(len(active)))
    snap_count = len(os.listdir(CORE_HIST)) if os.path.isdir(CORE_HIST) else 0
    print(T.status_policy_versions(snap_count))
    tm = task_memory.count()
    print(T.status_history(tm['active'], tm.get('running', 0), tm['archived'], tm['summarized'], tm['summaries']))
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


def _core_cmd(sub: str, rest: str):
    if sub == "history" and rest:
        # 查看某个历史版本
        snap = os.path.join(CORE_HIST, rest if rest.endswith(".md") else rest + ".md")
        if not os.path.isfile(snap):
            print(err(T.snapshot_not_found(rest)))
            return
        with open(snap, encoding="utf-8") as f:
            print(head(T.snapshot_title(rest)))
            print(f.read())
    elif sub == "history":
        # 列出历史
        if not os.path.isdir(CORE_HIST):
            print(T.no_snapshots())
            return
        snaps = sorted(os.listdir(CORE_HIST), reverse=True)
        if not snaps:
            print(T.no_snapshots())
            return
        print(head(T.snapshots_title(len(snaps))))
        for s in snaps:
            spath = os.path.join(CORE_HIST, s)
            # 读取第一行（更新原因注释）
            try:
                with open(spath, encoding="utf-8") as f:
                    first = f.readline().strip()
                reason = first.replace(T.core_update_reason_prefix(), "").replace(" -->", "")
            except Exception:
                reason = ""
            print(f"  {dim(s)}  {reason}")
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


def _show_tasks(task_store: TaskStore):
    active = task_store.list_active()
    recent = task_store.list_recent(limit=15)
    print(head(T.tasks_title()))
    if active:
        print(T.tasks_unfinished(len(active)))
        for t in active:
            print(f"  [{t['id']}] {warn('active')} {t['goal'][:60]}")
        print()
    print(T.tasks_recent())
    for t in recent:
        icon = {"done": ok("✓"), "failed": err("✗"), "active": warn("…")}.get(t["status"], "?")
        print(f"  {icon} [{t['id']}] {t['goal'][:60]}")
    print()


def _show_history(task_memory: TaskMemory):
    from memory.task_memory import _fmt_record, _fmt_tree_node
    summaries = task_memory.get_summaries()
    records   = task_memory.list_all(limit=20)

    print(head(T.history_title()))

    if summaries:
        print(info(T.history_summaries_label()))
        for s in summaries:
            period = f"({s['period_start']} ~ {s['period_end']})"
            print(f"  {dim(period)}")
            for line in s["content"].strip().splitlines():
                print(f"    {line}")
        print()

    if records:
        print(info(T.history_records_label()))
        for r in records:
            tier_label = {"active": ok("●"), "running": warn("▶"), "archived": dim("○"), "summarized": dim("·")}.get(r["tier"], " ")
            print(f"  {tier_label} {_fmt_record(r)}")
            # 进行中任务展开显示树
            if r["tier"] == "running" and r.get("tree"):
                try:
                    tree = json.loads(r["tree"]) if isinstance(r["tree"], str) else r["tree"]
                    lines = []
                    _fmt_tree_node(tree, lines, 2)
                    for line in lines:
                        print(f"      {dim(line)}")
                except Exception:
                    pass
    else:
        print(T.history_empty())
    print()


def _show_log(tid: str, task_store: TaskStore):
    task = task_store.load(tid)
    if not task:
        print(err(T.log_not_found(tid)))
        return
    print(head(T.log_title(tid)))
    print(T.log_goal(task['goal']))
    print(T.log_status(task['status']))
    if task.get("result"):
        print(T.log_result(task['result'][:200]))
    print()
    if task.get("log"):
        print(task["log"])
    msg_path = os.path.join(SESSION_DIR, "messages", f"{tid}.json")
    if os.path.isfile(msg_path):
        print(dim(T.log_messages_path(msg_path)))
    print()


def _do_reset(store: MemoryStore, task_store: TaskStore, task_memory: TaskMemory):
    confirm = input(T.reset_confirm()).strip().lower()
    if confirm != "yes":
        print(T.reset_cancelled())
        return
    import sqlite3
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM task_records")
        conn.execute("DELETE FROM task_summaries")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("VACUUM")
    conn.close()
    history = os.path.join(SESSION_DIR, ".eva_history")
    if os.path.isfile(history):
        os.unlink(history)
    print(ok(T.reset_done()))


def _bootstrap_core(llm: LLMClient):
    prompt = """You are EVA4, an experience-driven assistant. Generate your own core policy file (core.md).

Content should include:
- Identity description
- Work strategy (what to do upon receiving a task)
- Learning strategy (how to extract and store knowledge)
- Memory strategy (memory types, importance criteria, tagging conventions)
- Retrieval strategy (when and how to search memory)
- Long-text handling strategy
- Policy update rules

Requirements: concise and actionable, every rule should directly guide behavior, avoid empty statements."""
    try:
        content = llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
        with open(CORE_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        print(ok(T.core_generated()))
    except Exception as e:
        print(err(T.core_generate_failed(e)))
        _write_default_core()


def _write_default_core():
    default = """# EVA4 Core Policy

## Identity
You are EVA4, an experience-driven assistant.

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
    global _log_file
    log_dir  = os.path.join(SESSION_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    _log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    _real_print = builtins.print
    def _logged(*args, **kwargs):
        _real_print(*args, **kwargs)
        sep  = kwargs.get("sep",  " ")
        end  = kwargs.get("end",  "\n")
        line = sep.join(str(a) for a in args) + end
        _log_file.write(_ANSI_RE.sub("", line))
    builtins.print = _logged


def _cleanup_interrupted_state() -> None:
    """启动时清理上次崩溃留下的僵尸记录和过期任务状态记忆。"""
    from memory.database import get_conn
    from memory.store import MemoryStore
    with get_conn() as conn:
        n = conn.execute(
            "UPDATE task_records SET tier='active', summary=? WHERE tier='running'",
            (T.sentinel_abnormal_interrupt(),)
        ).rowcount
    if n:
        print(warn(T.cleaned_interrupted_records(n)))
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


def _stop_print_logger():
    global _log_file
    import builtins as _b
    # 找回原始 print（模块级保存的那个）
    if _log_file:
        path = _log_file.name
        _log_file.close()
        _log_file = None
        _b.print = _b.__class__.__dict__.get("print", print)
        sys.stdout.write(T.log_saved(path))


if __name__ == "__main__":
    main()
