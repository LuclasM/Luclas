import hashlib
import os
import re
import datetime
from config import CORE_PATH, CORE_LOCAL_PATH, CORE_HIST
import i18n as T

# core.md sections are tagged with `<!-- segment: general|work -->` comments
# right before the heading where a section starts. A tag stays in effect for
# every following line until the next tag, so most headings don't need their
# own — only the ones where the segment actually changes. "general" =
# domain-agnostic method (identity, discipline, workflow); "work" = this
# deployment's business specifics (goals, tag taxonomy, health-check data).
# Lines before the first tag default to "general" (currently just the title).
_SEGMENT_TAG_RE = re.compile(r'^<!--\s*segment:\s*(general|work)\s*-->\s*$')

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

# path -> sha256 of the content as of the last load_core() call for that path.
# Lets core_update() notice if the file changed on disk (e.g. a hand edit)
# since it was last read into context, instead of silently clobbering it.
_last_loaded_hash: dict[str, str] = {}


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _atomic_write(path: str, content: str) -> None:
    """Write via temp file + os.replace so a crash mid-write can never leave
    a truncated/corrupt file at `path` — the rename is atomic."""
    d = os.path.dirname(path) or "."
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def core_update(new_content: str, reason: str) -> dict:
    os.makedirs(CORE_HIST, exist_ok=True)
    target = CORE_LOCAL_PATH if os.path.isfile(CORE_LOCAL_PATH) else CORE_PATH

    # 保存当前版本到快照，并检测自上次 load_core() 以来文件是否被外部改动过
    snap_path = None
    drift_detected = False
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            old = f.read()
        expected_hash = _last_loaded_hash.get(target)
        if expected_hash is not None and _hash(old) != expected_hash:
            drift_detected = True

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snap_path = os.path.join(CORE_HIST, f"{ts}.md")
        header = f"{T.core_update_reason_prefix()}{reason} -->\n\n"
        if drift_detected:
            header = (
                f"{T.core_update_reason_prefix()}{reason} -->\n"
                f"<!-- WARNING: this file was modified on disk after it was last loaded "
                f"(e.g. a manual edit) — this snapshot preserves that version -->\n\n"
            )
        _atomic_write(snap_path, header + old)

    # 写入新版本
    _atomic_write(target, new_content)
    _last_loaded_hash[target] = _hash(new_content)

    result = {"ok": True, "reason": reason, "snapshot": snap_path}
    if drift_detected:
        result["drift_detected"] = True
        result["warning"] = (
            "core.md changed on disk since it was last loaded (possibly a manual edit) — "
            "the pre-update version was preserved in the snapshot, but review it to make "
            "sure nothing was unintentionally overwritten."
        )
    return result


def _filter_segment(content: str, segment: str) -> str:
    current = "general"
    out = []
    for line in content.splitlines(keepends=True):
        m = _SEGMENT_TAG_RE.match(line.strip())
        if m:
            current = m.group(1)
            continue  # marker line itself never appears in filtered output
        if current == segment:
            out.append(line)
    return "".join(out)


def load_core(segment: str = "all") -> str:
    """优先加载本地业务定制（core.local.md，不入开源仓库），否则用默认 core.md。

    segment="all"（默认）返回磁盘上的原文，不做任何过滤——core_update() 的
    drift 检测和 /core 展示都依赖这份原始哈希对得上，不能悄悄改动内容。
    segment="general"/"work" 按 `<!-- segment: ... -->` 标记过滤，只用于挑选
    某个调用方实际需要的那部分（见 conversation_runner.py）。
    """
    path = CORE_LOCAL_PATH if os.path.isfile(CORE_LOCAL_PATH) else CORE_PATH
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    _last_loaded_hash[path] = _hash(content)
    if segment == "all":
        return content
    return _filter_segment(content, segment)


def list_core_snapshots() -> list[dict]:
    """List history snapshots newest-first, each with its update reason
    (the first line's comment, written by core_update()). Shared by the CLI
    (/core history) and the web API (GET /api/system/core/history) so
    there's exactly one implementation of "what's a valid snapshot name."""
    if not os.path.isdir(CORE_HIST):
        return []
    snaps = sorted(os.listdir(CORE_HIST), reverse=True)
    result = []
    for s in snaps:
        try:
            with open(os.path.join(CORE_HIST, s), encoding="utf-8") as f:
                first = f.readline().strip()
            reason = first.replace(T.core_update_reason_prefix(), "").replace(" -->", "")
        except Exception:
            reason = ""
        result.append({"name": s, "reason": reason})
    return result


def read_core_snapshot(name: str) -> str:
    """Read one history snapshot by exact filename. Only accepts a name
    returned by list_core_snapshots() — callers taking `name` from an HTTP
    request must not join client input into a path without this check."""
    valid_names = {s["name"] for s in list_core_snapshots()}
    if name not in valid_names:
        raise FileNotFoundError(name)
    with open(os.path.join(CORE_HIST, name), encoding="utf-8") as f:
        return f.read()
