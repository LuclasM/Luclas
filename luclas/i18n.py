"""
i18n.py — CLI display string localization.

Only user-facing terminal text lives here (print/input strings). Prompts sent
to the LLM are deliberately NOT localized — they stay in English regardless
of LUC_LANG, since the model's output language is governed by core.md and
the conversation content, not by the scaffold prompt's language.

Usage: import i18n as T; print(T.startup_banner())
"""
import random

from config import LANG

_ZH = (LANG == "zh")


def _pick(en: str, zh: str) -> str:
    return zh if _ZH else en


# ── status sentinels (also used for control-flow prefix matching) ──────────

def failed_prefixes() -> tuple:
    return _pick(
        ("(execution error", "(exceeded max iterations", "(interrupted:"),
        ("（执行异常", "（超出迭代上限", "（中断："),
    )


def sentinel_user_interrupted() -> str:
    return _pick("(interrupted by user)", "（用户中断）")


def sentinel_abnormal_interrupt() -> str:
    return _pick("(abnormally interrupted)", "（异常中断）")


def sentinel_not_completed() -> str:
    return _pick("(not completed)", "（未完成）")


def sentinel_exec_error(e) -> str:
    return _pick(f"(execution error: {e})", f"（执行异常：{e}）")


def sentinel_exceeded_max_iter() -> str:
    return _pick("(exceeded max iterations)", "（超出迭代上限）")


def sentinel_interrupted(reason: str) -> str:
    return _pick(f"(interrupted: {reason})", f"（中断：{reason}）")


def sentinel_paused_by_tool() -> str:
    return _pick("(tool execution paused by user)", "（工具执行被用户暂停）")


def sentinel_skipped() -> str:
    return _pick("(skipped)", "（跳过）")


# ── luc.py: help / banners ──────────────────────────────────────────────────

def ascii_banner() -> str:
    return r"""
██╗     ██╗   ██╗ ██████╗██╗      █████╗ ███████╗
██║     ██║   ██║██╔════╝██║     ██╔══██╗██╔════╝
██║     ██║   ██║██║     ██║     ███████║███████╗
██║     ██║   ██║██║     ██║     ██╔══██║╚════██║
███████╗╚██████╔╝╚██████╗███████╗██║  ██║███████║
╚══════╝ ╚═════╝  ╚═════╝╚══════╝╚═╝  ╚═╝╚══════╝

          Luclas AI System
"""


def author_line() -> str:
    return "LU MENG  <lu@mengs.ca>"


def version_line(version: str, date: str) -> str:
    return _pick(f"v{version}  ·  updated {date}", f"v{version}  ·  更新于 {date}")


_TIPS_ZH = [
    "每次反思之后，我会把更好的方法写进自己的策略文件——下一次做得更好，不是靠承诺，是靠改变。",
    "我有记忆。你告诉我的事，我会记住；做过的任务，我会留痕。不是当场聪明，是越用越懂你。",
    "任务中途觉得方向不对？按一下 Ctrl-C 暂停，告诉我该怎么调整，我继续。不需要重头来过。",
    "遇到不清楚的地方，我会先问你，不会蒙着头往前冲。把目标搞清楚，比快速开始更重要。",
    "你可以设定定时任务，让我在固定时间帮你做事。不用每次都来找我，我会自己醒来。",
    "每天凌晨四点，如果系统闲置超过一小时，我会自己做一次全面反思，把发现的问题写进策略。",
    "失败了我不会假装没事。连续出错会触发自我评估，找到问题，改进方法，再试。",
    "用 /reflect 可以随时让我做一次深度复盘——不只看最近，是把所有记录都拿出来想清楚。",
    "我的「聪明」存在两个地方：记忆库里的经验，和 core.md 里的工作方法。两者都会随时间变好。",
    "我能上网搜索、读文件、跑代码、发请求。工具不是目的，把你的事做完才是。",
    "第一次见面，我什么都不了解你。多用一段时间，我就开始懂你了。",
    "用 /core 可以看我现在的工作方法，用 /memory 可以看我记住了什么。我对你没有秘密。",
    "一次任务做完，我会从中提取有用的经验存进记忆。下次遇到类似的事，不会再从零开始。",
    "我不是聊天机器人，也不是搜索引擎。我是一个会做事、会学习、会记住你的工作伙伴。",
    "尽量多的让我做事情，尽量多的给我指示。不要怕我失败，做的越多失败越多我越聪明。",
]

_TIPS_EN = [
    "After each reflection, I rewrite my own strategy file with better methods. Improvement through action, not promises.",
    "I have memory. What you tell me, I keep. Tasks I've done leave traces. Not smart all at once — smarter over time.",
    "Task going sideways? Hit Ctrl-C to pause, give me a correction, and I'll keep going. No need to start over.",
    "When something's unclear, I'll ask before charging ahead. Getting the goal right matters more than starting fast.",
    "You can set scheduled tasks — I'll wake up at the right time and get things done without you having to ask.",
    "At 4am every night, if I've been idle for over an hour, I run a full self-reflection and update my strategy.",
    "Failures don't get swept under the rug. Repeated errors trigger self-evaluation — find the issue, fix the method.",
    "Use /reflect anytime for a deep review — not just recent work, but everything, thought through properly.",
    "My 'intelligence' lives in two places: memory (experience) and core.md (method). Both improve over time.",
    "I can search the web, read files, run code, make requests. Tools aren't the point — getting things done is.",
    "First meeting, I know nothing about you. Give it some time, and I'll start to understand how you work.",
    "Use /core to see my current working method, /memory to see what I remember. No secrets.",
    "After each task, I extract useful experience into memory. Next time something similar comes up, I won't start from zero.",
    "Not a chatbot. Not a search engine. A working partner that gets things done, learns, and remembers you.",
    "Give me as much work as you can. The more I do, the more I fail — and the smarter I get. Don't hold back.",
]


def identity_line() -> str:
    return _pick(
        "I am Luclas, a self-evolving agent. Give me time, and I'll grow alongside you.",
        "我是 Luclas，一个能自我进化的智能体。给我时间，让我陪你成长。",
    )


def tips_line() -> str:
    tip = random.choice(_TIPS_ZH) if _ZH else random.choice(_TIPS_EN)
    label = "提示" if _ZH else "Tips"
    return f"  {label}：{tip}"


def help_text() -> str:
    return _pick(
        """
  <any text>              Run a task or ask a question

  /help                   Show this help
  /status                 System status
  /whoami                 Configuration info
  /core                   View current core policy
  /core history           List historical policy versions
  /core history <file>    View a historical version
  /memory                 View memory (latest 30)
  /memory search <kw>     Search memory
  /tasks                  View task records
  /history                View work history (latest 20 task records)
  /log <task_id>          View task execution log
  /models                 Show configured LLM models
  /reflect                Full strategic reflection — improves core.md methodology
  /schedule               Manage scheduled tasks
  /reset                  Clear all memory and tasks (requires confirmation)
  /q  /quit               Quit
""",
        """
  <任意文字>              执行任务或提问

  /help                   显示帮助
  /status                 系统状态
  /whoami                 配置信息
  /core                   查看当前核心策略
  /core history           查看历史版本列表
  /core history <文件名>  查看某个历史版本
  /memory                 查看记忆（最新 30 条）
  /memory search <词>     搜索记忆
  /tasks                  查看任务记录
  /history                查看工作历史（最近 20 条任务记录）
  /log <task_id>          查看任务执行日志
  /models                 查看已配置的大模型
  /reflect                全面反思，改进 core.md 方法论
  /schedule               管理定时任务
  /reset                  清除所有记忆和任务（需确认）
  /q  /quit               退出
""",
    )


def startup_hint() -> str:
    return _pick(
        (
            "  /help  show all commands    /quit  exit\n"
            "  Ctrl-C  ① during task: pause & add instructions"
            "  ② again: stop task  ③ at prompt: exit"
        ),
        (
            "  /help  查看所有命令    /quit  退出\n"
            "  Ctrl-C  ① 任务中：暂停并补充指令"
            "  ② 再按：停止任务  ③ 空闲时：退出系统"
        ),
    )


def reflect_title() -> str:
    return _pick("Strategic Reflection", "全面反思")

def reflect_hint() -> str:
    return _pick(
        "Analyzing task history, memory patterns, and pending recommendations…",
        "正在分析任务历史、记忆规律和未处理建议……",
    )

def schedule_title(n: int) -> str:
    return _pick(f"Scheduled Tasks ({n})", f"定时任务（{n} 条）")

def schedule_empty() -> str:
    return _pick("No scheduled tasks.", "暂无定时任务。")

def schedule_never() -> str:
    return _pick("never", "从未运行")

def schedule_last_run(ts: str) -> str:
    return _pick(f"last run: {ts}", f"上次运行：{ts}")

def schedule_add_title() -> str:
    return _pick("Add Scheduled Task", "添加定时任务")

def schedule_prompt_name() -> str:
    return _pick("Name:", "名称：")

def schedule_prompt_goal() -> str:
    return _pick("Goal:", "任务目标：")

def schedule_prompt_freq() -> str:
    return _pick("Frequency [daily/weekly]:", "频率 [daily/weekly]：")

def schedule_prompt_day() -> str:
    return _pick("Day [mon/tue/wed/thu/fri/sat/sun]:", "星期 [mon/tue/wed/thu/fri/sat/sun]：")

def schedule_prompt_time() -> str:
    return _pick("Time [HH:MM]:", "时间 [HH:MM]：")

def schedule_cancelled() -> str:
    return _pick("Cancelled.", "已取消。")

def schedule_added(id_: str, name: str, stype: str, sday: str, stime: str) -> str:
    freq = f"{stype} {sday} {stime}".strip()
    return _pick(f"Scheduled: [{id_}] {name} @ {freq}", f"已添加：[{id_}] {name} @ {freq}")

def schedule_toggled(id_: str, enabled: bool) -> str:
    state = _pick("enabled", "已启用") if enabled else _pick("disabled", "已暂停")
    return f"[{id_}] {state}"

def schedule_not_found(id_: str) -> str:
    return _pick(f"Task [{id_}] not found.", f"未找到任务 [{id_}]。")

def schedule_del_confirm(id_: str) -> str:
    return _pick(f"Delete [{id_}]? [y/N] ", f"确认删除 [{id_}]？[y/N] ")

def schedule_deleted(id_: str) -> str:
    return _pick(f"Deleted [{id_}].", f"已删除 [{id_}]。")

def ask_user_label() -> str:
    return _pick("Agent needs your input:", "Agent 需要你的回答：")

def ask_user_prompt() -> str:
    return _pick("Your answer →", "你的回答 →")

def ask_user_no_answer() -> str:
    return _pick("(no answer provided)", "（用户未作答）")


def core_missing() -> str:
    return _pick("⚠ core.md not found, generating initial policy…", "⚠ 未找到 core.md，正在生成初始策略…")


def online() -> str:
    return _pick("online", "在线")


def offline() -> str:
    return _pick("offline", "离线")


def running_count(n: int) -> str:
    return _pick(f"in progress {n}", f"进行中 {n} 个任务")


def status_line(llm_avail: str, mem_count: int, active: int, archived: int, running_label: str) -> str:
    return _pick(
        f"  LLM: {llm_avail}   memory: {mem_count}   history: {active} active / {archived} archived{running_label}",
        f"  LLM：{llm_avail}   记忆：{mem_count} 条   历史：{active} 活跃 / {archived} 归档{running_label}",
    )


def unfinished_tasks(n: int) -> str:
    return _pick(f"  {n} unfinished task(s), see /tasks", f"  有 {n} 个未完成任务，/tasks 查看")


def session_id_line(sid: str) -> str:
    return _pick(f"  Session ID: {sid}", f"  会话 ID：{sid}")


def goodbye() -> str:
    return _pick("再见" if _ZH else "Goodbye", "再见")


def goodbye_nl() -> str:
    return _pick("\nGoodbye", "\n再见")


def task_started() -> str:
    return _pick("任务开始" if _ZH else "Task started", "任务开始")


def task_done() -> str:
    return _pick("完成" if _ZH else "Done", "完成")


def task_interrupted() -> str:
    return _pick("\n⚠ Task interrupted\n", "\n⚠ 任务已中断\n")


def task_exception(e) -> str:
    return _pick(f"✗ Exception: {e}", f"✗ 异常：{e}")


def unknown_command(cmd: str) -> str:
    return _pick(f"Unknown command: /{cmd}  type /help for help", f"未知命令：/{cmd}  输入 /help 查看帮助")


def log_usage() -> str:
    return _pick("✗ usage: /log <task_id>", "✗ 用法：/log <task_id>")


# ── status / whoami ─────────────────────────────────────────────────────────

def status_title() -> str:
    return _pick("系统状态" if _ZH else "System Status", "系统状态")


def status_memory(n: int) -> str:
    return _pick(f"  Memory:        {n}", f"  记忆：      {n} 条")


def status_active_tasks(n: int) -> str:
    return _pick(f"  Active tasks:  {n}", f"  活跃任务：  {n} 个")


def status_policy_versions(n: int) -> str:
    return _pick(f"  Policy versions: current + {n} snapshot(s)", f"  策略版本：  当前 + {n} 个历史快照")


def status_history(active, running, archived, summarized, summaries) -> str:
    return _pick(
        f"  Work history:  active {active} | running {running} | archived {archived} | "
        f"summarized {summarized} | {summaries} summary block(s)",
        f"  工作历史：  活跃 {active} | 进行中 {running} | 归档 {archived} | 压缩 {summarized} | 摘要 {summaries} 段",
    )


def models_title(n: int) -> str:
    return _pick(f"Model Configuration ({n})", f"模型配置（{n} 个）")


def whoami_title() -> str:
    return _pick("Luclas Configuration", "Luclas 配置")


def whoami_model(v) -> str:
    return _pick(f"  Model:         {v}", f"  模型：      {v}")


def whoami_endpoint(v) -> str:
    return _pick(f"  Endpoint:      {v}", f"  服务：      {v}")


def whoami_db(v) -> str:
    return _pick(f"  Database:      {v}", f"  数据库：    {v}")


def whoami_core(v) -> str:
    return _pick(f"  Policy file:   {v}", f"  策略文件：  {v}")


def whoami_max_iter(v) -> str:
    return _pick(f"  Max iterations:{v}", f"  最大迭代：  {v}")


def whoami_llm_status(v) -> str:
    return _pick(f"  LLM status:    {v}", f"  LLM 状态：  {v}")


def whoami_time(v) -> str:
    return _pick(f"  Time:          {v}", f"  时间：      {v}")


# ── /core ────────────────────────────────────────────────────────────────────

def snapshot_not_found(name: str) -> str:
    return _pick(f"✗ snapshot not found: {name}", f"✗ 找不到快照：{name}")


def snapshot_title(name: str) -> str:
    return _pick(f"\nSnapshot: {name}\n", f"\n历史快照：{name}\n")


def no_snapshots() -> str:
    return _pick("\nNo historical snapshots\n", "\n无历史快照\n")


def snapshots_title(n: int) -> str:
    return _pick(f"\nPolicy history ({n} snapshot(s))\n", f"\n策略历史（{n} 个快照）\n")


def core_update_reason_prefix() -> str:
    return _pick("<!-- update reason: ", "<!-- 更新原因：")


def core_missing_warn() -> str:
    return _pick("⚠ core.md does not exist", "⚠ core.md 不存在")


def current_core_title() -> str:
    return _pick("\nCurrent core policy\n", "\n当前核心策略\n")


# ── /memory ──────────────────────────────────────────────────────────────────

def memory_not_found(kw: str) -> str:
    return _pick(f"No memory found containing \"{kw}\"", f"未找到包含「{kw}」的记忆")


def memory_search_title(kw: str, n: int) -> str:
    return _pick(f"\nMemory search \"{kw}\", {n} result(s)\n", f"\n记忆搜索「{kw}」，共 {n} 条\n")


def no_tags() -> str:
    return _pick("no tags", "无标签")


def memory_empty() -> str:
    return _pick("\nMemory store is empty\n", "\n记忆库为空\n")


def untyped() -> str:
    return _pick("untyped", "未分类")


def memory_store_title(n: int, stats: str) -> str:
    return _pick(f"\nMemory store ({n} total)  {stats}\n", f"\n记忆库（共 {n} 条）  {stats}\n")


# ── /tasks /history /log ────────────────────────────────────────────────────

def tasks_title() -> str:
    return _pick("\nTask records\n", "\n任务记录\n")


def tasks_unfinished(n: int) -> str:
    return _pick(f"Unfinished ({n}):", f"未完成（{n} 个）：")

def tasks_recent() -> str:
    return _pick("Recent:", "最近记录：")


def history_title() -> str:
    return _pick("\nWork history\n", "\n工作历史\n")


def history_summaries_label() -> str:
    return _pick("【Summaries】", "【历史摘要】")


def history_records_label() -> str:
    return _pick("【Task records】", "【任务记录】")


def history_empty() -> str:
    return _pick("  (no task records yet)", "  （暂无任务记录）")


def log_not_found(tid: str) -> str:
    return _pick(f"✗ task not found: {tid}", f"✗ 未找到任务：{tid}")


def log_title(tid: str) -> str:
    return _pick(f"\nTask log [{tid}]", f"\n任务日志 [{tid}]")


def log_goal(v) -> str:
    return _pick(f"  Goal:   {v}", f"  目标：{v}")


def log_status(v) -> str:
    return _pick(f"  Status: {v}", f"  状态：{v}")


def log_result(v) -> str:
    return _pick(f"  Result: {v}", f"  结果：{v}")


def log_messages_path(p: str) -> str:
    return _pick(f"\nFull conversation log: {p}", f"\n完整对话消息：{p}")


# ── /reset ───────────────────────────────────────────────────────────────────

def reset_confirm() -> str:
    return _pick("Confirm clearing all memory and tasks? Type yes to confirm: ",
                 "确认清除所有记忆和任务？输入 yes 确认：")


def reset_cancelled() -> str:
    return _pick("Cancelled", "已取消")


def reset_done() -> str:
    return _pick("✓ Reset done — memory, tasks and work history cleared (core.md kept)",
                 "✓ 已重置，记忆、任务和工作历史已清除（core.md 保留）")


# ── bootstrap core ───────────────────────────────────────────────────────────

def core_generated() -> str:
    return _pick("✓ core.md generated", "✓ core.md 已生成")


def core_generate_failed(e) -> str:
    return _pick(f"✗ generation failed: {e}, using built-in default policy",
                 f"✗ 生成失败：{e}，使用内置默认策略")


# ── startup cleanup / migration ──────────────────────────────────────────────

def cleaned_interrupted_records(n: int) -> str:
    return _pick(f"  ⚠ cleaned up {n} interrupted task record(s)", f"  ⚠ 已清理 {n} 条异常中断的任务记录")


def cleaned_stale_memories(n: int) -> str:
    return _pick(f"  ⚠ cleaned up {n} stale task-state memory record(s)", f"  ⚠ 已清理 {n} 条过期任务状态记忆")


def embedding_migrating(n: int) -> str:
    return _pick(f"  ⟳ generating vector index for {n} memory record(s)…",
                 f"  ⟳ 正在为 {n} 条记忆生成向量索引…")


def embedding_migrated(n: int) -> str:
    return _pick(f"  ✓ vector index done ({n} record(s))", f"  ✓ 向量索引完成（{n} 条）")


def log_saved(path: str) -> str:
    return _pick(f"Log saved: {path}\n", f"日志已保存：{path}\n")


# ── task_runner.py ───────────────────────────────────────────────────────────

def archived_note(n: int) -> str:
    return _pick(f"  (archived {n} task record(s))", f"  （任务记录归档 {n} 条）")


def compressed_note() -> str:
    return _pick("  (history compressed)", "  （历史记录已压缩）")


def decompose_line(goal: str) -> str:
    return _pick(f"decompose: {goal}", f"分解：{goal}")


def merge_line(goal: str) -> str:
    return _pick(f"merge: {goal}", f"合并：{goal}")


def current_task_tree_label() -> str:
    return _pick("Current task tree (in progress):", "当前任务树（执行中）：")


def aar_saved(mid: str) -> str:
    return _pick(f"AAR: experience saved (id={mid})", f"AAR: 经验已存入记忆 (id={mid})")


def tool_error_line(e) -> str:
    return _pick(f"✗ {e}", f"✗ {e}")


# ── agent_loop.py ────────────────────────────────────────────────────────────

def round_header(i: int, max_i: int) -> str:
    return f"\n  ┌── {_pick('round', '轮次')} {i}/{max_i} " + "─" * 28


def llm_call_failed(e) -> str:
    return _pick(f"LLM call failed: {e}", f"LLM 调用失败：{e}")


def stalled_loop(window: int) -> str:
    return _pick(f"detected repeated loop ({window} identical calls in a row)",
                 f"检测到循环调用（连续 {window} 次相同）")


def too_many_errors(n: int) -> str:
    return _pick(f"{n} consecutive tool errors", f"连续 {n} 次工具出错")


def paused_label() -> str:
    return _pick("⏸  paused", "⏸  已暂停")


def paused_hint() -> str:
    return _pick("type a new instruction to continue, press enter to skip, Ctrl-C again to stop the task",
                 "输入新指令继续，直接回车跳过，再次 Ctrl-C 停止任务")


def task_stopped() -> str:
    return _pick("✗ task stopped", "✗ 任务已停止")


def resumed_with_input() -> str:
    return _pick("▶ instruction received, resuming...\n", "▶ 收到指令，继续执行...\n")


def resumed() -> str:
    return _pick("▶ resuming...\n", "▶ 继续执行...\n")


def n_matches(n: int) -> str:
    return _pick(f"{n} match(es)", f"{n} 个匹配")


def n_files(n: int) -> str:
    return _pick(f"{n} file(s)", f"{n} 个文件")


def n_memories(n: int) -> str:
    return _pick(f"memory {n} result(s)", f"记忆 {n} 条")


def more_lines(n: int) -> str:
    return _pick(f"…({n} lines total)", f"…（共{n}行）")


def more_chars(n: int) -> str:
    return _pick(f"…({n} chars total)", f"…（共{n}字）")


# ── task_memory.py: build_context labels (shared by LLM context + /history) ─

def work_history_header() -> str:
    return _pick("=== Work History ===", "=== 工作历史 ===")


def running_tasks_label() -> str:
    return _pick("\n[Tasks in progress]", "\n【进行中任务】")


def goal_label(v: str) -> str:
    return _pick(f"Goal: {v}", f"目标：{v}")


def summaries_label() -> str:
    return _pick("\n[Summaries]", "\n【历史摘要】")


def recent_tasks_label() -> str:
    return _pick("\n[Recent tasks]", "\n【最近任务】")


def relevant_history_label() -> str:
    return _pick("\n[Relevant history]", "\n【当前相关历史】")


def artifacts_label() -> str:
    return _pick(" | artifacts: ", " | 产出: ")
