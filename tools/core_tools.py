import os
import datetime
from config import CORE_PATH, CORE_LOCAL_PATH, CORE_HIST
import i18n as T

CORE_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "core_update",
        "description": (
            "Update the core policy file (core.md). "
            "Call this when you discover a better working method, learning method, or memory strategy. "
            "The previous version is automatically saved to a history snapshot before updating."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "new_content": {"type": "string", "description": "The full new policy content"},
                "reason":      {"type": "string", "description": "Reason for the update"},
            },
            "required": ["new_content", "reason"],
        },
    },
}


def core_update(new_content: str, reason: str) -> dict:
    os.makedirs(CORE_HIST, exist_ok=True)
    target = CORE_LOCAL_PATH if os.path.isfile(CORE_LOCAL_PATH) else CORE_PATH

    # 保存当前版本到快照
    snap_path = None
    if os.path.isfile(target):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_path = os.path.join(CORE_HIST, f"{ts}.md")
        with open(target, encoding="utf-8") as f:
            old = f.read()
        with open(snap_path, "w", encoding="utf-8") as f:
            f.write(f"{T.core_update_reason_prefix()}{reason} -->\n\n")
            f.write(old)

    # 写入新版本
    with open(target, "w", encoding="utf-8") as f:
        f.write(new_content)

    return {"ok": True, "reason": reason, "snapshot": snap_path}


def load_core() -> str:
    """优先加载本地业务定制（core.local.md，不入开源仓库），否则用默认 core.md。"""
    path = CORE_LOCAL_PATH if os.path.isfile(CORE_LOCAL_PATH) else CORE_PATH
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()
