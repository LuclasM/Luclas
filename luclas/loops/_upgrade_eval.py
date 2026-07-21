"""
loops/_upgrade_eval.py — P0-4 升级触发与评估引擎

任务完成后自动评估是否需要系统升级。
不阻断主流程，评估结果以建议形式输出。
"""

import json
import re
import datetime
import os
import threading

from config import DATA_DIR
import i18n as T

_UPGRADE_TRIGGER_FILE = os.path.join(DATA_DIR, "upgrade_trigger.json")

# api.py creates a fresh TaskRunner (and hence a fresh UpgradeEvaluator) per
# /chat call, and different sessions run concurrently on their own threads —
# without this lock, two tasks finishing close together would each load
# upgrade_trigger.json, mutate their own in-memory copy, and whichever saves
# last would silently overwrite the other's recorded task, undercounting
# consecutive failures. Scoped to this process only (not cross-process safe
# against e.g. the CLI and the API service touching the same file at once,
# which would need real file locking) — acceptable here since only one of
# those is normally running against a given data dir at a time.
_HISTORY_LOCK = threading.Lock()


class UpgradeEvaluator:
    """
    升级评估器。任务完成后调用 evaluate_after_task()，
    根据任务结果历史判断是否需要升级。
    """

    UPGRADE_THRESHOLD = 3  # 连续 N 个任务失败触发评估
    COOLDOWN_HOUR = 6  # 评估后冷却期（小时）

    def __init__(self, llm, task_memory, mem_store):
        self.llm = llm
        self.task_memory = task_memory
        self.mem_store = mem_store
        self._history = {}   # populated fresh, under _HISTORY_LOCK, in evaluate_after_task()

    def _load_history(self) -> dict:
        try:
            with open(_UPGRADE_TRIGGER_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {"recent_tasks": [], "last_eval": "", "cooldown_until": ""}

    def _save_history(self) -> None:
        try:
            with open(_UPGRADE_TRIGGER_FILE, 'w') as f:
                json.dump(self._history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def evaluate_after_task(self, goal: str, result: str) -> None:
        """任务完成后调用，评估是否需要升级。"""
        # The read-modify-write of the history file happens under the lock,
        # reloading fresh from disk rather than trusting self._history from
        # construction time — see _HISTORY_LOCK's comment for why (concurrent
        # sessions each own a separate UpgradeEvaluator instance). The actual
        # assessment call below (self.llm.chat(), a network round-trip) runs
        # *outside* the lock — every task's completion calls this method, so
        # holding the lock across a slow LLM call would stall every other
        # concurrently-finishing task's own bookkeeping behind one
        # assessment. The cooldown is recorded before releasing the lock
        # (rather than after the assessment finishes) so a second failure
        # streak that shows up while the first assessment is still running
        # doesn't also decide to kick off a redundant one.
        should_assess = False
        consecutive_failures = 0
        with _HISTORY_LOCK:
            self._history = self._load_history()

            # 检查冷却期
            cooldown = self._history.get("cooldown_until", "")
            if cooldown:
                try:
                    cooldown_dt = datetime.datetime.fromisoformat(cooldown)
                    if datetime.datetime.now() < cooldown_dt:
                        return  # 冷却期内，跳过
                except Exception:
                    pass

            status = "failed" if self._is_failed(result) else "done"

            # 记录本次任务
            self._history["recent_tasks"].append({
                "goal": goal[:100],
                "status": status,
                "time": datetime.datetime.now().isoformat(),
                "result_preview": result[:200],
            })

            # 只保留最近 10 个任务记录
            self._history["recent_tasks"] = self._history["recent_tasks"][-10:]

            # 检查连续失败
            recent = self._history["recent_tasks"]
            for t in reversed(recent):
                if t["status"] == "failed":
                    consecutive_failures += 1
                else:
                    break

            should_assess = consecutive_failures >= self.UPGRADE_THRESHOLD
            if should_assess:
                # 设置冷却期（评估开始前先设，避免并发的第二次失败串又触发一次评估）
                cooldown_dt = datetime.datetime.now() + datetime.timedelta(hours=self.COOLDOWN_HOUR)
                self._history["cooldown_until"] = cooldown_dt.isoformat()
                self._history["last_eval"] = datetime.datetime.now().isoformat()

            self._save_history()

        if should_assess:
            self._run_upgrade_assessment(consecutive_failures)

    def _is_failed(self, result: str) -> bool:
        return any(result.startswith(p) for p in T.failed_prefixes())

    def _run_upgrade_assessment(self, fail_count: int) -> None:
        """运行升级评估，输出建议。"""
        print(f"\n{'='*60}")
        print(f"  ⚠ {fail_count} consecutive task failures, triggering upgrade assessment...")
        print(f"{'='*60}")

        # 收集失败任务信息
        failed_tasks = [
            f"  - {t['goal']}: {t['result_preview'][:100]}"
            for t in self._history["recent_tasks"]
            if t["status"] == "failed"
        ]

        prompt = (
            f"The last {fail_count} consecutive tasks failed:\n"
            + "\n".join(failed_tasks) + "\n\n"
            f"Please analyze:\n"
            f"1. Do these failures share a common cause?\n"
            f"2. Is a system upgrade needed (core.md policy or Python code)?\n"
            f"3. If so, what are the specific upgrade recommendations?\n"
            f"\n"
            f"Return JSON:\n"
            f'{{"common_cause": "...", "upgrade_needed": true/false, '
            f'"recommendations": ["recommendation 1", "recommendation 2"]}}\n'
            f"Return only the JSON."
        )

        try:
            resp = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=600,
            )
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                if data.get("upgrade_needed"):
                    print(f"\n  🔧 Upgrade recommendation:")
                    print(f"  Common cause: {data.get('common_cause', 'unknown')}")
                    for i, rec in enumerate(data.get("recommendations", []), 1):
                        print(f"  {i}. {rec}")

                    # 将建议存入记忆
                    self.mem_store.write(
                        content=json.dumps(data, ensure_ascii=False, indent=2),
                        type="experience",
                        tags=["upgrade-assessment", "system-improvement"],
                        importance=8,
                        source="first_hand",   # derived from this agent's own task history
                        credibility=9,
                    )
                    print(f"\n  🧠 Upgrade recommendation saved to memory")
                else:
                    print(f"\n  ✅ Assessment result: no system upgrade needed")
                    print(f"  Analysis: {data.get('common_cause', 'no common cause')}")
            else:
                print(f"  (failed to parse assessment output)")
        except Exception as e:
            print(f"  (assessment execution failed: {e})")

        print(f"{'='*60}\n")
