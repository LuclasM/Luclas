"""
loops/task_runner.py — 单主线执行 + 按需分支（delegate_subtask）

一个任务从头到尾是一条连续的 run_agent 会话（root）；LLM 在这条会话里按步骤
推进，遇到值得独立处理的子任务时自己调用 delegate_subtask 工具分支出去
（TaskRunner._spawn_branch），分支本身又是一条独立、精炼上下文的嵌套
run_agent 会话，跑完只把最终结果折叠回调用方——分支还可以再分支（递归），
深度超过软上限后每次再分支前要求 LLM 自我审查是否真的必要。
决策永远基于已经发生的事实，而不是执行前一次性定死的计划。

任务的整棵树只存在于这次调用的内存里（root dict：{id, goal, status, result,
subtasks, atomic}），跑完就交给调用方（conversation_runner.py 的
dispatch_task 分支）落成一条 episode——不再有单独的 task_records 表持久化
执行过程中的每一步（那套三层记忆机制已经整个被持续对话层 + episodes/lessons
取代，见 memory/conversation_store.py、memory/episode_store.py）。
"""

import datetime
import json
import re
import threading
import uuid

from loops.agent_loop import run_agent
from loops._upgrade_eval import UpgradeEvaluator
from tools.delegate import make_delegate_tool
from tools.user_input import _NeedUserInput
from utils.display import info, ok, err, warn
import i18n as T

# Branch nesting soft cap (see TaskRunner._spawn_branch / _judge_deeper_branch):
# past this depth, every further delegate_subtask call first goes through an
# LLM self-review of the whole branch tree before being allowed.
_MAX_SOFT_DEPTH = 3


def _node(goal: str) -> dict:
    return {
        "id":       uuid.uuid4().hex[:8],
        "goal":     goal,
        "status":   "pending",
        "result":   "",
        "subtasks": [],
        "atomic":   False,
    }


class TaskRunner:

    def __init__(self, llm, schemas, fns,
                 mem_store, session_id: str,
                 progress_callback=None, supplement_queue=None):
        self.llm               = llm
        self.schemas           = schemas
        self.fns               = fns
        self.mem_store         = mem_store
        self.session_id        = session_id
        self.progress_callback = progress_callback
        self.supplement_queue  = supplement_queue
        # P0-4: 升级触发机制 - 跟踪 root 任务完成情况
        self._upgrade_evaluator = UpgradeEvaluator(self.llm, self.mem_store)
        # Guards the tree-append + _save/_write_mem read-modify-write when
        # multiple delegate_subtask branches finish concurrently (parallel
        # dispatch — see loops/agent_loop.py). A single TaskRunner instance
        # only ever has one root task in flight at a time (both call sites,
        # luclas.py and api.py, either reuse the instance sequentially or
        # construct a fresh one per task), so this only needs to protect
        # concurrent branches *within* one run(), not across runs.
        self._branch_lock = threading.Lock()

    # ── 入口 ─────────────────────────────────────────────

    def run(self, goal: str, on_result=None) -> str:
        display_goal = _strip_adapter_prefix(goal)   # clean goal for DB / display
        self.llm.set_goal(display_goal)              # classify without adapter noise
        root         = _node(goal)                   # full goal (with adapter context) for LLM
        mem_id      = [None]   # list 让分支闭包可以修改

        try:
            self._run_root(root, mem_id)
        except KeyboardInterrupt:
            self._mark_interrupted(root)
            self._cleanup_mem(mem_id)
            raise
        except _NeedUserInput as e:
            # No channel/terminal available to ask on (headless --run/--reflect,
            # or an API session with no recognized push channel). _run_root lets
            # this one propagate instead of swallowing it into a generic "failed"
            # result, so the real caller (api.py:_run_task's own
            # `except _NeedUserInput`) gets a clean "needs input" signal instead
            # of a garbled execution-error string.
            if not root.get("result"):
                root["result"] = T.sentinel_needs_input(e.question)
            self._mark_interrupted(root)
            self._cleanup_mem(mem_id)
            raise

        final = root.get("result", T.sentinel_not_completed())
        if on_result:
            on_result(final)

        self._cleanup_mem(mem_id)

        # P0-4: 任务完成后评估是否需要系统升级
        self._upgrade_evaluator.evaluate_after_task(goal, final)

        return final

    # ── 主线程 ───────────────────────────────────────────

    def _run_root(self, root: dict, mem_id: list) -> None:
        """单条连续的主线程会话：一次 run_agent 贯穿整个任务，遇到值得独立
        处理的子任务时，LLM 自己调用 delegate_subtask 分支出去（_spawn_branch）。"""
        delegate_schema, delegate_fn = make_delegate_tool(
            lambda g, c="": self._spawn_branch(
                root, g, c, ancestors=[], depth=0,
                root=root, mem_id=mem_id,
            )
        )
        schemas = self.schemas + [delegate_schema]
        fns     = {**self.fns, "delegate_subtask": delegate_fn}

        task = {"id": uuid.uuid4().hex[:12], "goal": root["goal"],
                "status": "active", "log": "", "result": ""}
        root["exec_id"] = task["id"]

        root["status"] = "running"
        mem_id[0] = self._write_mem(root, mem_id[0])

        try:
            result = run_agent(
                root["goal"], task, self.llm, schemas, fns,
                task_context="",
                progress_callback=self.progress_callback,
                supplement_queue=self.supplement_queue,
            )
            root["result"] = result
            root["status"] = "failed" if _is_failed(result) else "done"
        except _NeedUserInput:
            # Let this propagate to run() — see its except _NeedUserInput
            # handler for why this must not be folded into the generic
            # except Exception below.
            raise
        except Exception as e:
            root["status"] = "failed"
            root["result"] = T.sentinel_exec_error(e)
            print(err(T.tool_error_line(e)))

        icon = ok("✓") if root["status"] == "done" else err("✗")
        print(f"{icon} {root['goal'][:60]}")

        # P0-3: 没有分支出任何子任务的简单任务，root 自己就是"原子执行单元"
        # ——补一次 AAR，跟旧模型里 atomic 根节点会跑 AAR 是同一个语义。有过
        # 分支的任务，各分支自己在 _spawn_branch 里已经各跑过一次了，这里不
        # 重复（避免同一次任务里对"已经分支过的整体"再摘一遍经验，观感重复）。
        if not root.get("subtasks"):
            self._auto_aar(root, [])

        mem_id[0] = self._write_mem(root, mem_id[0])

    # ── 分支执行（delegate_subtask 的实际实现） ──────────

    def _spawn_branch(self, parent_node: dict, goal: str, context: str,
                      ancestors: list[str], depth: int,
                      root: dict,
                      mem_id: list) -> str:
        """由 delegate_subtask 工具调用：校验 → 建子节点 → 跑一段独立、精炼
        上下文的嵌套 run_agent → 结果折叠回调用方（只返回最终文本）。
        分支自己也带一个新的 delegate_subtask（绑定到这个子节点、depth+1），
        所以分支内部还能再分支，天然支持递归。
        """
        indent = "  " * depth

        # 护栏一：目标和自己或祖先重复 → 防死循环
        if any(_goals_similar(goal, a) for a in ancestors + [parent_node["goal"]]):
            return T.branch_refused_ancestor()

        # 护栏二：深度软上限——超过后要求 LLM 自我审查是否真的必要
        if depth >= _MAX_SOFT_DEPTH:
            print(f"{indent}{warn('⚠')} {T.branch_depth_review(depth)}")
            allowed, reason = self._judge_deeper_branch(root, goal, depth)
            if not allowed:
                print(f"{indent}  {err('✗')} {T.branch_refused_depth(reason)}")
                return T.branch_refused_depth(reason)

        child = _node(goal)
        with self._branch_lock:
            parent_node.setdefault("subtasks", []).append(child)
            mem_id[0] = self._write_mem(root, mem_id[0])
        print(f"{indent}{info('◈')} {T.branch_start_line(goal)}")

        child_ancestors = ancestors + [parent_node["goal"]]
        full_ctx = self._branch_context(goal, context, root, child_ancestors)

        branch_llm = self.llm.clone()
        branch_llm.set_goal(goal)

        delegate_schema, delegate_fn = make_delegate_tool(
            lambda g, c="": self._spawn_branch(
                child, g, c, ancestors=child_ancestors, depth=depth + 1,
                root=root, mem_id=mem_id,
            )
        )
        schemas = self.schemas + [delegate_schema]
        fns     = {**self.fns, "delegate_subtask": delegate_fn}

        task = {"id": uuid.uuid4().hex[:12], "goal": goal,
                "status": "active", "log": "", "result": ""}
        child["exec_id"] = task["id"]

        try:
            result = run_agent(
                goal, task, branch_llm, schemas, fns,
                task_context=full_ctx,
                parent_goal=parent_node["goal"],
                progress_callback=self.progress_callback,
                supplement_queue=self.supplement_queue,
                branch_tag=f"b:{child['id']}",
            )
            child["result"] = result
            child["status"] = "failed" if _is_failed(result) else "done"
        except _NeedUserInput as e:
            # Unlike the root task (see run()'s own except _NeedUserInput),
            # a branch has a parent conversation to report back to, so this
            # stays a contained branch failure rather than propagating and
            # aborting the whole task tree — the parent LLM can decide how to
            # proceed (try another approach, or hit the same wall itself at
            # the root level, where there's genuinely nowhere further to go).
            # Still worth a clean message instead of the generic
            # execution-error wording below, so it's obvious what happened.
            child["status"] = "failed"
            child["result"] = T.sentinel_needs_input(e.question)
        except Exception as e:
            child["status"] = "failed"
            child["result"] = T.sentinel_exec_error(e)
            print(err(T.tool_error_line(e)))

        icon = ok("✓") if child["status"] == "done" else err("✗")
        print(f"{indent}  {icon} {goal[:60]}")

        with self._branch_lock:
            mem_id[0] = self._write_mem(root, mem_id[0])

        # P0-3: 分支完成后自动执行 AAR（跟原来 atomic 节点的 AAR 是同一套逻辑）
        self._auto_aar(child, ancestors)

        return child["result"]

    def _branch_context(self, goal: str, context: str, root: dict,
                        ancestors: list[str]) -> str:
        """精炼上下文：只给分支目标 + 调用方主动交代的事实 + 完整任务树（供
        了解全局）——不拷贝调用方那条对话的原始思考/工具调用记录，避免分支的
        prompt 无限膨胀，也避免分支被调用方尚未确认的中间猜测带偏。长期记忆/
        历史经历的检索留给 memory_search 工具按需调用（见
        tools/memory_tools.py），不再在这里无条件注入。"""
        tree_str = self._tree_str_full(root)
        path     = " › ".join(ancestors + [goal]) if ancestors else goal

        parts = [
            f"=== Current task tree (for awareness) ===\n{tree_str}\n",
            f"=== Your branch's execution point ===\n{path}\n",
        ]
        if context.strip():
            parts.append(f"=== Context handed off from the calling task ===\n{context.strip()}\n")
        parts.append(
            "[Execution rules] You were branched out via delegate_subtask to handle the task below "
            "on your own. Use the context above plus your own tools; do not re-derive facts already "
            "given to you. Return a self-contained final answer — it is the only thing that flows "
            "back to the caller, so make it complete."
        )

        return "\n\n".join(parts)

    def _judge_deeper_branch(self, root: dict, goal: str, depth: int) -> tuple[bool, str]:
        """深度超过软上限后的审查调用：默认从紧（判断失败也算不通过），
        要求谨慎细分、在有限深度内收敛完成任务。"""
        tree_str = self._tree_str_full(root)
        prompt = (
            f"Branch tree so far:\n{tree_str}\n\n"
            f"Current branch depth: {depth} (soft cap: {_MAX_SOFT_DEPTH})\n"
            f"Proposed next branch: {goal}\n\n"
            "This task has already branched deeper than the normal soft cap. Before allowing yet "
            "another branch, judge carefully: is a new independent sub-conversation genuinely "
            "necessary here, or can this be done directly with tools in the current conversation? "
            "Be conservative — prefer finishing within a limited depth over decomposing further. "
            "Only approve if the work is truly independent and substantial enough to warrant its "
            "own sub-conversation.\n\n"
            'Return JSON only: {"allow": true/false, "reason": "short reason"}'
        )
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=200)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return bool(data.get("allow")), str(data.get("reason") or "")
        except Exception:
            pass
        return False, "review call failed — defaulting to disallow further branching"

    def _auto_aar(self, node: dict, ancestors: list[str]) -> str | None:
        """P0-3: 原子任务完成后自动执行 After Action Review。返回写入的记忆 id（若有）。"""
        log = node.get("result", "")
        if not log or len(log) < 50:
            return None  # 结果太短，跳过 AAR

        goal = node["goal"]
        status = node["status"]
        status_text = "success" if status == "done" else "failure"

        prompt = (
            f"You just completed a task: {goal}\n"
            f"Result status: {status_text}\n"
            f"Result summary: {log[:1500]}\n\n"
            "Please perform an After Action Review:\n"
            "1. What went well? (extract reusable methods)\n"
            "2. What problems came up? (record lessons learned)\n"
            "3. Anything worth remembering?\n\n"
            "If there is an experience worth recording, return JSON:\n"
            '{"experience": "...", "type": "experience or workflow", "tags": ["tag1"], "importance": 5}\n'
            "If not, return: {\"experience\": null}\n"
            "Return JSON only, no other text."
        )
        try:
            resp = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                exp = data.get("experience")
                if exp and isinstance(exp, str) and len(exp) > 10:
                    mid = self.mem_store.write(
                        content=exp,
                        type=data.get("type", "experience"),
                        tags=data.get("tags", [goal[:20]]),
                        importance=min(10, max(1, data.get("importance", 5))),
                        source="first_hand",       # own direct execution, not external material
                        credibility=9,
                    )
                    brain_icon = ok("\U0001f9e0")
                    print(f"{'  ' * (len(ancestors) + 1)}{brain_icon} {T.aar_saved(mid)}")
                    return mid
        except Exception:
            pass  # AAR 失败不应影响主任务
        return None

    # ── 持久化 ───────────────────────────────────────────
    # 任务树不再落 task_records（那张表已经整个退役）——调用方
    # （conversation_runner.py 的 dispatch_task 分支）在 run() 返回后自己把
    # 最终结果落成一条 episode。这里只留 _write_mem：跑动过程中把当前树写进
    # mem_store 一条临时 "task_state" 记忆，供进程还活着时的实时状态查看，
    # 任务结束 _cleanup_mem() 就删掉，跟 episodes/task_records 一直是两回事。

    def _write_mem(self, root: dict, existing_id: str | None) -> str:
        content = f"{T.current_task_tree_label()}\n{self._tree_str_full(root)}"
        if existing_id:
            try:
                if self.mem_store.update(existing_id, content=content):
                    return existing_id
            except Exception:
                pass
        return self.mem_store.write(
            content=content,
            type="task_state",
            tags=["task_state", root["goal"][:20]],
            importance=8,
        )

    # ── 树显示 ───────────────────────────────────────────

    def _tree_str_full(self, root: dict) -> str:
        """注入 LLM context 用（结果完整，不截断）。"""
        lines = []
        self._fmt_node_full(root, lines, 0)
        return "\n".join(lines)

    def _fmt_node_full(self, node: dict, lines: list, depth: int) -> None:
        """完整结果版本，供 LLM context 使用。"""
        icon   = {"pending": "○", "running": "▶", "done": "✓", "failed": "✗"}.get(node["status"], "?")
        indent = "  " * depth
        lines.append(f"{indent}[{icon}] {node['goal']}")
        if node.get("result"):
            for ln in node["result"].splitlines():
                lines.append(f"{indent}    {ln}")
        for st in node.get("subtasks", []):
            self._fmt_node_full(st, lines, depth + 1)

    def _cleanup_mem(self, mem_id: list) -> None:
        if mem_id[0]:
            try:
                self.mem_store.delete(mem_id[0])
            except Exception:
                pass
            mem_id[0] = None

    def _mark_interrupted(self, node: dict) -> None:
        if node["status"] not in ("done", "failed"):
            node["status"] = "failed"
            if not node.get("result"):
                node["result"] = T.sentinel_user_interrupted()
        for st in node.get("subtasks", []):
            self._mark_interrupted(st)


# ── 模块级辅助 ────────────────────────────────────────────

def _is_failed(result: str) -> bool:
    return any(result.startswith(p) for p in T.failed_prefixes())


def _strip_adapter_prefix(goal: str) -> str:
    """Remove messaging-adapter context prefixes injected before the actual task goal."""
    return re.sub(r'^\[[^\]]{0,200}\]\s*', '', goal).strip()


def _goals_similar(a: str, b: str) -> bool:
    """判断两个目标是否高度相似（防止子任务包含与祖先相同的目标）。"""
    a, b = a.strip().lower(), b.strip().lower()
    if a == b:
        return True
    # 一方是另一方的子串且长度接近
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter and shorter in longer and len(shorter) / len(longer) > 0.7:
        return True
    return False
