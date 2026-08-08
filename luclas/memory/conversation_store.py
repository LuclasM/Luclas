"""
memory/conversation_store.py — 持久对话本体

一个 conversation_id 一条持续对话（消息渠道复用现有 session_id 格式，CLI 固定
用 "cli_local"），断线/进程重启都能从这里原样接续。messages 是活跃窗口内的
原始轮次；每条消息标一个 episode_id——指向当前正在累积的话题段
（current_episode_id），任务相关的轮次标 "__task__"（不参与话题分段，任务
完成时直接由 EpisodeStore.create_task_episode 落库，见 conversation_runner.py）。

一旦一段话题关闭（close_topic），它对应的消息就"已经安全落库"；活跃窗口
达到 50% 时按重要性和新鲜度渐进压缩——见 compress_if_over_threshold()。
"""

import datetime
import json
import threading
import uuid

from memory.database import get_conn
from memory.token_estimate import estimate_tokens
import memory.decay as decay

TASK_EPISODE_TAG = "__task__"
COMPRESS_TRIGGER_RATIO = 0.50
COMPRESS_TARGET_RATIO = 0.50
DEFAULT_CONTEXT_LENGTH = 8192

# append_message()/close_topic()/compress_if_over_threshold() are all read-
# modify-write on the same conversations.messages JSON blob (read the whole
# row, mutate in Python, write the whole row back) — SQLite's own writer
# serialization doesn't protect against that, since two threads can each
# successfully SELECT before either UPDATEs. This is a real, not
# theoretical, race in this codebase: a background dispatch_task result
# (api.py's _bg()/luclas.py's _make_cli_dispatch _run()) can land at any
# moment while a *different* conversation turn for the same conversation_id
# is still mid-flight (that's the entire point of background dispatch —
# the conversation stays responsive and keeps going while it runs), and
# without this lock the loser's whole append is silently dropped. One lock
# per conversation_id, mirroring tools/user_input.py's _lock_for_session.
_locks_guard = threading.Lock()
_conversation_locks: dict = {}


def _lock_for(conversation_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _conversation_locks.get(conversation_id)
        if lock is None:
            lock = threading.Lock()
            _conversation_locks[conversation_id] = lock
        return lock


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

    def append_message(self, conversation_id: str, role: str, content: str, episode_id: str = None) -> str:
        with _lock_for(conversation_id):
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conv = self.get_or_create(conversation_id)
            tag = episode_id or conv["current_episode_id"]
            messages = conv["messages"]
            message_id = uuid.uuid4().hex[:12]
            messages.append({"id": message_id, "role": role, "content": content,
                             "timestamp": now, "episode_id": tag})
            with get_conn() as conn:
                conn.execute("UPDATE conversations SET messages=?, updated_at=? WHERE id=?",
                             (json.dumps(messages, ensure_ascii=False), now, conversation_id))
            return message_id

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
        with _lock_for(conversation_id):
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

    def split_topic_before_messages(self, conversation_id: str, episode_store,
                                    message_ids: list[str], importance: int = 5) -> str | None:
        """Put the identified current turn at the start of a new topic.

        Topic classification happens after the assistant has answered, so both
        the triggering user message and its answer have already been appended.
        The old implementation archived those messages with the previous topic
        and opened an empty topic afterwards.  Retag the exact messages (ids are
        used because a background task result may have appended concurrently),
        archive only the preceding part, and leave this turn live in the new
        topic.
        """
        wanted = set(message_ids)
        with _lock_for(conversation_id):
            conv = self.get_or_create(conversation_id)
            old_id = conv["current_episode_id"]
            old_segment = [m for m in conv["messages"]
                           if m.get("episode_id") == old_id and m.get("id") not in wanted]
            if old_segment:
                content = "\n".join(f"{m['role']}: {m['content']}" for m in old_segment)
                episode_store.close_conversation_episode(
                    old_id, conversation_id, content, importance=importance)
            new_id = uuid.uuid4().hex[:12]
            moved = False
            for m in conv["messages"]:
                if m.get("id") in wanted:
                    m["episode_id"] = new_id
                    moved = True
            if not moved:
                return None
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with get_conn() as conn:
                conn.execute(
                    "UPDATE conversations SET messages=?, current_episode_id=?, updated_at=? WHERE id=?",
                    (json.dumps(conv["messages"], ensure_ascii=False), new_id, now, conversation_id),
                )
            return old_id if old_segment else None

    def compress_if_over_threshold(self, conversation_id: str, episode_store, llm,
                                   context_length: int = DEFAULT_CONTEXT_LENGTH) -> dict:
        """Immediately and gradually compress dialogue history under pressure.

        At 50% context usage, close the live segment (a storage rollover, not a
        semantic topic claim), rank closed episodes by importance + decayed
        freshness, and downgrade the least valuable ones one level per trigger:
        raw -> summary -> gist -> delete.  The condensed representation replaces
        raw turns in the live window, so it remains available to the next turn.
        """
        with _lock_for(conversation_id):
            conv = self.get_or_create(conversation_id)
            messages = conv["messages"]

            def tokens() -> int:
                return sum(estimate_tokens(m.get("content", "")) for m in messages)

            used = tokens()
            limit = context_length * COMPRESS_TRIGGER_RATIO
            if used < limit:
                return {"triggered": False, "before": used, "after": used, "processed": 0}

            # Strong rollover: make the currently-open segment eligible.  Its
            # high freshness normally keeps it behind older/lower-value topics.
            current_id = conv["current_episode_id"]
            current = [m for m in messages if m.get("episode_id") == current_id]
            if current:
                content = "\n".join(f"{m['role']}: {m['content']}" for m in current)
                episode_store.close_conversation_episode(current_id, conversation_id, content)
                new_id = uuid.uuid4().hex[:12]
                with get_conn() as conn:
                    conn.execute("UPDATE conversations SET current_episode_id=? WHERE id=?",
                                 (new_id, conversation_id))
                conv["current_episode_id"] = new_id

            episode_ids = []
            for m in messages:
                eid = m.get("episode_id")
                if eid and eid not in (conv["current_episode_id"], TASK_EPISODE_TAG) and eid not in episode_ids:
                    episode_ids.append(eid)

            now = datetime.datetime.now()
            candidates = []
            for eid in episode_ids:
                ep = episode_store.get(eid)
                if ep:
                    fresh = decay.compute_freshness(ep.get("freshness"), ep.get("created_at"), now)
                    candidates.append((decay.rank_key(ep.get("importance"), fresh), ep))
            candidates.sort(key=lambda item: item[0])

            processed = 0
            for _, ep in candidates:
                # At exactly 50%, still perform one step: "reached the ceiling"
                # includes equality.  Afterwards stop as soon as we are back at
                # or below the target.
                if processed and tokens() <= context_length * COMPRESS_TARGET_RATIO:
                    break
                eid = ep["id"]
                gran = ep.get("granularity") or "raw"
                target = decay.next_granularity(gran)
                indices = [i for i, m in enumerate(messages) if m.get("episode_id") == eid]
                if not indices:
                    continue
                if target is None:
                    messages[:] = [m for m in messages if m.get("episode_id") != eid]
                    episode_store.delete(eid)
                else:
                    try:
                        summary = llm.chat([{
                            "role": "user",
                            "content": (
                                "将下面这段对话历史压缩成更简短的版本。保留用户要求、已确认事实、"
                                "重要决定、任务状态、文件路径和未完成事项；去掉重复、寒暄和过程细节。"
                                f"目标颗粒度：{target}。只返回压缩后的内容。\n\n{ep['content']}"
                            ),
                        }], temperature=0.2)
                    except Exception:
                        # Context maintenance must never turn an otherwise
                        # successful user turn into a failed turn. Leave this
                        # episode untouched and try another candidate.
                        continue
                    episode_store.update_compressed(eid, summary, target)
                    first = indices[0]
                    replacement = {
                        "id": uuid.uuid4().hex[:12], "role": "system",
                        "content": f"[历史话题{target}，episode={eid}]\n{summary}",
                        "timestamp": messages[first].get("timestamp", ""),
                        "episode_id": eid, "granularity": target, "hidden": True,
                    }
                    messages[:] = [m for m in messages if m.get("episode_id") != eid]
                    messages.insert(min(first, len(messages)), replacement)
                processed += 1

            now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with get_conn() as conn:
                conn.execute("UPDATE conversations SET messages=?, updated_at=? WHERE id=?",
                             (json.dumps(messages, ensure_ascii=False), now_s, conversation_id))
            return {"triggered": True, "before": used, "after": tokens(), "processed": processed}

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
