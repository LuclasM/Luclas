__version__ = "0.2.15"

import builtins
import datetime
import json
import os
import re
import readline
import sys
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
from memory.task_memory import TaskMemory
from tools.registry import build_tools
from tools.core_tools import load_core, core_update
from loops.task_runner import TaskRunner
from utils.display import ok, err, warn, info, dim, head, bold
import i18n as T


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
    # marks every tier='running' task_records row as failed and deletes
    # task_state memories — it can't tell "stale from a crash" apart from
    # "genuinely in progress right now in api.py". Running it here would let
    # a scheduled reminder or nightly reflection randomly clobber a live,
    # unrelated in-progress task (e.g. a WeChat conversation) just from bad
    # timing. Zombie cleanup for a genuinely-headless-only deployment (no
    # interactive CLI, no api.py ever run) still happens eventually the next
    # time either of those starts — a real but narrow gap, and strictly
    # safer than the alternative.

    session_id  = uuid.uuid4().hex[:8]
    llm         = _make_llm()
    store       = MemoryStore()
    task_memory = TaskMemory()
    schemas, fns = build_tools(store)
    runner = TaskRunner(
        llm=llm, schemas=schemas, fns=fns,
        task_memory=task_memory,
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

    session_id  = uuid.uuid4().hex[:8]
    llm         = _make_llm()
    store       = MemoryStore()
    task_memory = TaskMemory()
    schemas, fns = build_tools(store)

    runner = TaskRunner(
        llm=llm, schemas=schemas, fns=fns,
        task_memory=task_memory,
        mem_store=store, session_id=session_id,
    )

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
    tm_counts = task_memory.count()
    n_running = tm_counts.get("running", 0)
    running_label = f"  {warn(T.running_count(n_running))}" if n_running else ""
    print(T.status_line(avail, store.count(), tm_counts['active'], tm_counts['archived'], running_label))
    active = task_memory.get_running()
    if active:
        print(warn(T.unfinished_tasks(len(active))))
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
                _handle_slash(line, llm, store, task_memory, schemas, fns, runner)
            except SystemExit:
                readline.write_history_file(_history_file)
                _stop_print_logger()
                _remove_pid()
                raise
            continue

        _run_task(line, runner)

    _stop_print_logger()
    _remove_pid()


def _run_task(goal: str, runner: TaskRunner):
    print(f"\n{head(T.task_started())}")
    try:
        # Show the result before any feedback prompt runner.run() may trigger internally.
        runner.run(goal, on_result=lambda r: print(f"\n{head(T.task_done())}\n{r}\n"))
    except KeyboardInterrupt:
        print(T.task_interrupted())
    except Exception as e:
        print(err(T.task_exception(e)))
        print()


# ── 斜杠命令 ──────────────────────────────────────────────

def _handle_slash(line: str, llm, store, task_memory, schemas, fns, runner=None):
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
        _show_status(store, task_memory)

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
        _show_tasks(task_memory)

    elif cmd == "history":
        _show_history(task_memory)

    elif cmd == "log":
        tid = sub or rest
        if not tid:
            print(err(T.log_usage()))
        else:
            _show_log(tid, task_memory)

    elif cmd == "reset":
        _do_reset(store, task_memory)

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

def _show_status(store: MemoryStore, task_memory: TaskMemory):
    print(f"\n{head(T.status_title())}")
    print(T.status_memory(store.count()))
    tm = task_memory.count()
    print(T.status_active_tasks(tm.get('running', 0)))
    snap_count = len(os.listdir(CORE_HIST)) if os.path.isdir(CORE_HIST) else 0
    print(T.status_policy_versions(snap_count))
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


def _show_tasks(task_memory: TaskMemory):
    active = task_memory.get_running()
    recent = task_memory.list_all(limit=15)
    print(head(T.tasks_title()))
    if active:
        print(T.tasks_unfinished(len(active)))
        for t in active:
            print(f"  [{t['id']}] {warn('active')} {t['goal'][:60]}")
        print()
    print(T.tasks_recent())
    for t in recent:
        icon = {"done": ok("✓"), "failed": err("✗"), "running": warn("…")}.get(t.get("status", ""), "?")
        print(f"  {icon} [{t['id']}] {t['goal'][:60]}")
    print()


def _show_history(task_memory: TaskMemory):
    from memory.task_memory import _fmt_record, _fmt_tree_node, _tree_had_failure
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
            if r.get("tree"):
                try:
                    tree = json.loads(r["tree"]) if isinstance(r["tree"], str) else r["tree"]
                except Exception:
                    tree = None
                if tree:
                    # 进行中任务：始终展开显示树
                    # 已完成任务：只有出现过失败节点才展开（避免刷屏）
                    had_failure = _tree_had_failure(tree)
                    if r["tier"] == "running" or had_failure:
                        if had_failure and r["tier"] != "running":
                            print(f"      {warn(T.history_had_failure())}")
                        lines = []
                        _fmt_tree_node(tree, lines, 2)
                        for line in lines:
                            print(f"      {dim(line)}")
    else:
        print(T.history_empty())
    print()


def _show_log(tid: str, task_memory: TaskMemory):
    from memory.task_memory import _fmt_tree_node, _collect_failed_nodes

    record = task_memory.get(tid)
    if not record:
        print(err(T.log_not_found(tid)))
        return

    tree = None
    if record.get("tree"):
        try:
            tree = json.loads(record["tree"]) if isinstance(record["tree"], str) else record["tree"]
        except Exception:
            tree = None

    print(head(T.log_title(tid)))
    print(T.log_goal(record['goal']))
    print(T.log_status(record.get('status', '?')))
    if tree and tree.get("result"):
        print(T.log_result(tree['result'][:200]))
    print()

    if tree:
        lines = []
        _fmt_tree_node(tree, lines, 0)
        print("\n".join(lines))

        # 失败子任务的完整执行记录（工具调用/思考过程），按 exec_id 定位
        for node in _collect_failed_nodes(tree):
            exec_id = node.get("exec_id")
            if not exec_id:
                continue
            msg_path = os.path.join(SESSION_DIR, "messages", f"{exec_id}.json")
            if os.path.isfile(msg_path):
                print(dim(T.log_failed_node_messages(node.get("goal", ""), msg_path)))
    print()


def _do_reset(store: MemoryStore, task_memory: TaskMemory):
    confirm = input(T.reset_confirm()).strip().lower()
    if confirm != "yes":
        print(T.reset_cancelled())
        return
    import sqlite3
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM task_records")
        conn.execute("DELETE FROM task_summaries")
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
    """启动时清理上次崩溃留下的僵尸记录和过期任务状态记忆。"""
    from memory.database import get_conn
    from memory.store import MemoryStore
    with get_conn() as conn:
        n = conn.execute(
            "UPDATE task_records SET tier='active', status='failed', summary=? WHERE tier='running'",
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
