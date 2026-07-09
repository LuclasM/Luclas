"""
loops/task_runner.py — 递归任务分解与执行

LLM 决定每个节点是否继续分解，无深度限制。
_run_node 递归调用自身：分解 → 对每个子任务递归 → 合并结果。
每个节点状态变化后立即持久化整棵树到 DB 和 MemoryStore。
"""

import datetime
import json
import re
import uuid

from loops.agent_loop import run_agent
from loops._upgrade_eval import UpgradeEvaluator
from memory.task_memory import TaskMemory
from utils.display import info, dim, ok, err
import i18n as T


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

    def __init__(self, llm, schemas, fns, task_store,
                 task_memory: TaskMemory, mem_store, session_id: str,
                 progress_callback=None, supplement_queue=None):
        self.llm               = llm
        self.schemas           = schemas
        self.fns               = fns
        self.task_store        = task_store
        self.task_memory       = task_memory
        self.mem_store         = mem_store
        self.session_id        = session_id
        self.progress_callback = progress_callback
        self.supplement_queue  = supplement_queue
        # P0-4: 升级触发机制 - 跟踪 root 任务完成情况
        self._upgrade_evaluator = UpgradeEvaluator(self.llm, self.task_memory, self.mem_store)

    # ── 入口 ─────────────────────────────────────────────

    def run(self, goal: str) -> str:
        display_goal = _strip_adapter_prefix(goal)   # clean goal for DB / display
        self.llm.set_goal(display_goal)              # classify without adapter noise
        root         = _node(goal)                   # full goal (with adapter context) for LLM
        record_id   = uuid.uuid4().hex[:12]
        started     = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mem_id      = [None]   # list 让递归内层可以修改

        self._persist(record_id, root, "running", "", [], started, display_goal)
        root_task = {"id": record_id, "goal": display_goal, "status": "active", "log": "", "result": ""}
        self.task_store.save(root_task)

        try:
            history_ctx = self.task_memory.build_context(goal)
            self._run_node(root, root, record_id, started, history_ctx, mem_id, depth=0)
        except KeyboardInterrupt:
            self._mark_interrupted(root)
            self._persist(record_id, root, "active", T.sentinel_user_interrupted(), [], started, display_goal)
            root_task["status"] = "failed"
            root_task["result"] = T.sentinel_user_interrupted()
            self.task_store.save(root_task)
            self._cleanup_mem(mem_id)
            raise

        final = root.get("result", T.sentinel_not_completed())
        summary, artifacts = self._post_process(goal, final)
        self._collect_feedback(display_goal, summary)
        self._persist(record_id, root, "active", summary, artifacts, started, display_goal)
        root_task["status"] = "done"
        root_task["result"] = final[:500]
        self.task_store.save(root_task)

        self._cleanup_mem(mem_id)

        archived = self.task_memory.archive_old()
        if archived:
            print(dim(T.archived_note(archived)))
        if self.task_memory.maybe_compress(self.llm):
            print(dim(T.compressed_note()))

        # P0-4: 任务完成后评估是否需要系统升级
        self._upgrade_evaluator.evaluate_after_task(goal, final)

        return final

    # ── 递归核心 ─────────────────────────────────────────

    def _run_node(self, node: dict, root: dict, record_id: str, started: str,
                  history_ctx: str, mem_id: list, depth: int,
                  ancestor_goals: list[str] | None = None,
                  prior_results: list[dict] | None = None) -> None:
        indent   = "  " * depth
        ancestors = ancestor_goals or []
        prior     = prior_results or []

        subtask_goals = self._decompose(node["goal"], history_ctx, root, ancestors)

        if subtask_goals:
            node["subtasks"] = [_node(st) for st in subtask_goals]
            self._print_decompose(node["goal"], subtask_goals, depth)
            self._save(record_id, root, started)
            mem_id[0] = self._write_mem(root, mem_id[0])

            child_ancestors = ancestors + [node["goal"]]
            child_prior: list[dict] = []          # 本层已完成的兄弟结果
            for child in node["subtasks"]:
                self._run_node(child, root, record_id, started, history_ctx,
                               mem_id, depth + 1, child_ancestors, child_prior)
                # 子节点完成后，把完整结果加入兄弟列表供下一个子节点使用
                if child["status"] in ("done", "failed"):
                    child_prior.append({"goal": child["goal"], "result": child["result"]})

            node["status"] = "running"
            result = self._synthesize(node)
            node["result"] = result
            node["status"] = "done"
            print(f"{indent}{ok('◉')} {T.merge_line(node['goal'][:60])}")

        else:
            node["atomic"] = True
            node["status"] = "running"
            self._save(record_id, root, started)
            mem_id[0] = self._write_mem(root, mem_id[0])

            result = self._execute(node, history_ctx, root, ancestors, prior)
            node["result"] = result
            node["status"] = "failed" if _is_failed(result) else "done"
            icon = ok("✓") if node["status"] == "done" else err("✗")
            print(f"{indent}  {icon} {node['goal'][:60]}")

            # P0-3: 原子任务完成后自动执行 AAR
            self._auto_aar(node, ancestors)
        # 每个节点完成后持久化
        self._save(record_id, root, started)
        mem_id[0] = self._write_mem(root, mem_id[0])

    # ── 分解决策 ─────────────────────────────────────────

    def _decompose(self, goal: str, history_ctx: str, root: dict,
                   ancestors: list[str]) -> list[str] | None:
        # 祖先路径中已有相同目标 → 强制原子，防止无限递归
        if any(_goals_similar(goal, a) for a in ancestors):
            return None

        tree_ctx     = self._tree_str(root)
        ancestor_str = " › ".join(ancestors[-3:]) if ancestors else ""
        prompt = (
            f"{('Work history:\n' + history_ctx[:600] + '\n\n---\n\n') if history_ctx else ''}"
            f"Current task tree:\n{tree_ctx}\n\n---\n\n"
            f"{'Task path: ' + ancestor_str + ' › ' + goal + chr(10) + chr(10) if ancestor_str else ''}"
            f"Pending task: {goal}\n\n"
            "Does this task need to be decomposed into multiple independent subtasks?\n"
            "- Can be done via tool calls or direct reasoning → {\"atomic\": true}\n"
            "- Needs multiple independent steps → {\"subtasks\": [\"step1\", \"step2\", ...]}\n\n"
            "Note: subtasks must not duplicate the current task or any ancestor task's goal.\n"
            "Return JSON only, no other text."
        )
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                if data.get("atomic"):
                    return None
                sts = data.get("subtasks", [])
                if isinstance(sts, list) and len(sts) >= 2:
                    # 过滤掉与祖先相似的子任务
                    filtered = [
                        str(s).strip() for s in sts
                        if str(s).strip()
                        and not any(_goals_similar(str(s), a) for a in ancestors + [goal])
                    ]
                    if len(filtered) >= 2:
                        return filtered
        except Exception:
            pass
        return None

    # ── 原子执行 ─────────────────────────────────────────

    def _execute(self, node: dict, history_ctx: str,
                 root: dict, ancestors: list[str],
                 prior_results: list[dict] | None = None) -> str:
        prior = prior_results or []

        # 完整任务树（含各节点完整结果，供 LLM 了解全局）
        tree_str         = self._tree_str_full(root)
        path             = " › ".join(ancestors + [node["goal"]]) if ancestors else node["goal"]
        immediate_parent = ancestors[-1] if ancestors else ""

        # 已完成的前置步骤结果（完整，不截断）
        prior_section = ""
        if prior:
            lines = ["=== Completed prior step results (use directly, do not redo) ==="]
            for i, pr in enumerate(prior, 1):
                lines.append(f"\n[Step {i}] {pr['goal']}")
                lines.append(pr["result"])
            prior_section = "\n".join(lines) + "\n\n"

        full_ctx = (
            f"=== Current task tree ===\n{tree_str}\n\n"
            f"=== Current execution point ===\n{path}\n\n"
            + prior_section +
            "[Execution rules] This is an atomic subtask produced by the task planner.\n"
            "1. Check the 'completed prior step results' above first, and reuse that data "
            "(paths, cookies, IDs, etc.) directly — do not redo steps already completed.\n"
            "2. Use tools to complete this task. Do not decompose it further.\n"
        )
        # 重新拉取最新上下文（任务执行中可能写入了新记忆）
        fresh_ctx = self.task_memory.build_context(node["goal"])
        if fresh_ctx:
            full_ctx += f"\n{fresh_ctx}"

        task = {
            "id":     uuid.uuid4().hex[:12],
            "goal":   node["goal"],
            "status": "active",
            "log":    "",
            "result": "",
        }

        try:
            result = run_agent(
                node["goal"], task, self.llm, self.schemas, self.fns,
                task_context=full_ctx,
                parent_goal=immediate_parent,
                progress_callback=self.progress_callback,
                supplement_queue=self.supplement_queue,
            )
            task["result"] = result
            task["status"] = "done"
        except Exception as e:
            task["status"] = "failed"
            result = T.sentinel_exec_error(e)
            print(err(T.tool_error_line(e)))

        return result



    def _auto_aar(self, node: dict, ancestors: list[str]) -> None:
        """P0-3: 原子任务完成后自动执行 After Action Review。"""
        log = node.get("result", "")
        if not log or len(log) < 50:
            return  # 结果太短，跳过 AAR

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
                    )
                    print(f"{'  ' * (len(ancestors) + 1)}{ok('\U0001f9e0')} {T.aar_saved(mid)}")
        except Exception:
            pass  # AAR 失败不应影响主任务

    def _synthesize(self, node: dict) -> str:
        lines = [
            f"Subtask {i+1}: {st['goal']}\nResult: {st['result']}"
            for i, st in enumerate(node["subtasks"])
        ]
        prompt = (
            f"You completed the task: {node['goal']}\n\n"
            "Subtask results:\n" + "─" * 40 + "\n"
            + "\n\n".join(lines)
            + "\n" + "─" * 40 + "\n\n"
            "Synthesize the results above into a complete answer to the original task. Be concise and accurate."
        )
        try:
            return self.llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
        except Exception:
            return "\n".join(f"{st['goal']}: {st['result'][:200]}" for st in node["subtasks"])

    # ── 持久化 ───────────────────────────────────────────

    def _persist(self, record_id: str, root: dict, tier: str,
                 summary: str, artifacts: list, created_at: str,
                 display_goal: str | None = None) -> None:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.task_memory.save({
            "id":           record_id,
            "session_id":   self.session_id,
            "goal":         display_goal or root["goal"],
            "summary":      summary,
            "artifacts":    artifacts,
            "tree":         root,
            "importance":   7,
            "tier":         tier,
            "created_at":   created_at,
            "completed_at": now,
        })

    def _save(self, record_id: str, root: dict, started: str) -> None:
        self._persist(record_id, root, "running", "", [], started)

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

    # ── 完成摘要 ─────────────────────────────────────────

    def _post_process(self, goal: str, result: str) -> tuple[str, list]:
        prompt = (
            f"Task: {goal}\n\nResult:\n{result[:2000]}\n\n"
            "Generate:\n1. A one-sentence summary (max ~80 words)\n"
            "2. A list of artifacts (file paths, URLs, key findings; empty array if none)\n\n"
            'Return JSON only: {"summary": "...", "artifacts": [{"type": "file", "path": "...", "desc": "..."}]}'
        )
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return data.get("summary", result[:80]), data.get("artifacts", [])
        except Exception:
            pass
        return result[:80], []

    def _collect_feedback(self, goal: str, summary: str) -> str:
        """Ask the user for feedback on the completed task; saved to memory for future learning.
        Only runs interactively — in non-interactive channels (API/adapters) ask_user raises
        _NeedUserInput, which we treat as "no feedback loop available here" and skip.
        """
        from tools.user_input import ask_user, _NeedUserInput
        try:
            answer = ask_user(T.feedback_question(summary))
        except _NeedUserInput:
            return ""
        if not answer or answer == T.ask_user_no_answer():
            return ""
        self.mem_store.write(
            content=f"Task: {goal}\nSummary: {summary}\nUser feedback: {answer}",
            type="feedback",
            tags=["user_feedback", goal[:20]],
            importance=7,
        )
        print(dim(T.feedback_saved()))
        return answer

    # ── 树显示 ───────────────────────────────────────────

    def _tree_str(self, root: dict) -> str:
        """终端显示用（结果截断到60字）。"""
        lines = []
        self._fmt_node(root, lines, 0)
        return "\n".join(lines)

    def _tree_str_full(self, root: dict) -> str:
        """注入 LLM context 用（结果完整，不截断）。"""
        lines = []
        self._fmt_node_full(root, lines, 0)
        return "\n".join(lines)

    def _fmt_node(self, node: dict, lines: list, depth: int) -> None:
        icon   = {"pending": "○", "running": "▶", "done": "✓", "failed": "✗"}.get(node["status"], "?")
        indent = "  " * depth
        result = f" → {node['result'][:60]}" if node.get("result") else ""
        lines.append(f"{indent}[{icon}] {node['goal']}{result}")
        for st in node.get("subtasks", []):
            self._fmt_node(st, lines, depth + 1)

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

    def _print_decompose(self, goal: str, subtasks: list[str], depth: int) -> None:
        indent = "  " * depth
        print(f"\n{indent}{info('◈')} {T.decompose_line(goal)}")
        for i, st in enumerate(subtasks, 1):
            print(f"{indent}  {dim(str(i) + '.')} {st}")


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
