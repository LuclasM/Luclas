"""
memory/conversation_store.py — 持久对话本体

一个 conversation_id 一条持续对话（消息渠道复用现有 session_id 格式，CLI 固定
用 "cli_local"），断线/进程重启都能从这里原样接续。messages 是活跃窗口内的
原始轮次；每条消息标一个 episode_id——指向当前正在累积的话题段
（current_episode_id），任务相关的轮次标 "__task__"（不参与话题分段，任务
完成时直接由 EpisodeStore.create_task_episode 落库，见 conversation_runner.py）。

一旦一段话题关闭（close_topic），它对应的消息就"已经安全落库"，可以在窗口
超过阈值时被驱逐出活跃窗口而不丢数据——见 evict_if_over_threshold()。
"""

import datetime
import json
import uuid

from memory.database import get_conn
from memory.token_estimate import estimate_tokens

TASK_EPISODE_TAG = "__task__"
COMPRESS_TRIGGER_RATIO = 0.70
COMPRESS_TARGET_RATIO = 0.50
DEFAULT_CONTEXT_LENGTH = 8192


class ConversationStore:
    def get_or_create(self, conversation_id: str) -> dict:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if row:
                return self._row(row)
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            episode_id = uuid.uuid4().hex[:12]
            conn.execute("""
                INSERT INTO conversations
                  (id, messages, current_episode_id, active_task_id, active_task_foreground, created_at, updated_at)
                VALUES (?, '[]', ?, '', 0, ?, ?)
            """, (conversation_id, episode_id, now, now))
            return {
                "id": conversation_id, "messages": [], "current_episode_id": episode_id,
                "active_task_id": "", "active_task_foreground": False,
                "created_at": now, "updated_at": now,
            }

    def append_message(self, conversation_id: str, role: str, content: str, episode_id: str = None) -> None:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conv = self.get_or_create(conversation_id)
        tag = episode_id or conv["current_episode_id"]
        messages = conv["messages"]
        messages.append({"role": role, "content": content, "timestamp": now, "episode_id": tag})
        with get_conn() as conn:
            conn.execute("UPDATE conversations SET messages=?, updated_at=? WHERE id=?",
                         (json.dumps(messages, ensure_ascii=False), now, conversation_id))

    def get_messages(self, conversation_id: str) -> list:
        return self.get_or_create(conversation_id)["messages"]

    def set_active_task(self, conversation_id: str, task_id: str, foreground: bool) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE conversations SET active_task_id=?, active_task_foreground=? WHERE id=?",
                         (task_id, 1 if foreground else 0, conversation_id))

    def clear_active_task(self, conversation_id: str) -> None:
        self.set_active_task(conversation_id, "", False)

    def close_topic(self, conversation_id: str, episode_store, importance: int = 5) -> str:
        """Persist the currently-open topic-segment as an episode and start a
        fresh one. Returns the closed episode's id, or None if the segment
        had no messages yet (nothing to close)."""
        conv = self.get_or_create(conversation_id)
        current_id = conv["current_episode_id"]
        segment = [m for m in conv["messages"] if m["episode_id"] == current_id]
        if not segment:
            return None
        content = "\n".join(f"{m['role']}: {m['content']}" for m in segment)
        episode_store.close_conversation_episode(current_id, conversation_id, content, importance=importance)
        new_episode_id = uuid.uuid4().hex[:12]
        with get_conn() as conn:
            conn.execute("UPDATE conversations SET current_episode_id=? WHERE id=?",
                         (new_episode_id, conversation_id))
        return current_id

    def evict_if_over_threshold(self, conversation_id: str, context_length: int = DEFAULT_CONTEXT_LENGTH) -> int:
        """Drop already-closed-episode turns from the live window once usage
        crosses COMPRESS_TRIGGER_RATIO, down to COMPRESS_TARGET_RATIO. Turns
        belonging to the still-open topic (current_episode_id) are never
        evicted this way — only turns whose episode is already safely in
        the episodes table. Returns how many messages were evicted."""
        conv = self.get_or_create(conversation_id)
        messages = conv["messages"]
        current_id = conv["current_episode_id"]

        def total_tokens(msgs):
            return sum(estimate_tokens(m["content"]) for m in msgs)

        used = total_tokens(messages)
        if used < context_length * COMPRESS_TRIGGER_RATIO:
            return 0

        target = context_length * COMPRESS_TARGET_RATIO
        evicted = 0
        i = 0
        while i < len(messages) and used > target:
            m = messages[i]
            if m["episode_id"] == current_id:
                i += 1
                continue
            used -= estimate_tokens(m["content"])
            evicted += 1
            del messages[i]
            # don't advance i — the list shifted left under us

        if evicted:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with get_conn() as conn:
                conn.execute("UPDATE conversations SET messages=?, updated_at=? WHERE id=?",
                             (json.dumps(messages, ensure_ascii=False), now, conversation_id))
        return evicted

    def _row(self, r) -> dict:
        return {
            "id": r["id"],
            "messages": json.loads(r["messages"] or "[]"),
            "current_episode_id": r["current_episode_id"],
            "active_task_id": r["active_task_id"],
            "active_task_foreground": bool(r["active_task_foreground"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
