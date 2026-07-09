import os
import subprocess


CODE_DIR = os.path.dirname(os.path.abspath(__file__))   # luclas/
BASE_DIR = os.path.dirname(CODE_DIR)                     # Luclas/ (repo root, data root)


def _load_dotenv() -> None:
    """Minimal .env loader (no external dependency)."""
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git"] + args, cwd=BASE_DIR, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return ""


_VERSION_BASE = "1.0"
_build        = _git(["rev-list", "--count", "HEAD"]) or "0"
_date         = _git(["log", "-1", "--format=%cd", "--date=short"]) or "unknown"

VERSION      = f"{_VERSION_BASE}.{_build}"
VERSION_DATE = _date
DATA_DIR     = os.path.join(BASE_DIR, "data")
DB_PATH      = os.path.join(DATA_DIR, "luclas.db")
CORE_PATH        = os.path.join(DATA_DIR, "core.md")
CORE_LOCAL_PATH  = os.path.join(DATA_DIR, "core.local.md")
CORE_HIST        = os.path.join(DATA_DIR, "core_history")
REFLECT_PATH     = os.path.join(DATA_DIR, "reflect.md")
RAW_DIR      = os.path.join(DATA_DIR, "raw")
SESSION_DIR  = os.path.join(DATA_DIR, "sessions")

LANG = os.environ.get("LUC_LANG", "en")

LLM_BASE_URL = os.environ.get("LUC_LLM_BASE_URL", "")
LLM_MODEL    = os.environ.get("LUC_LLM_MODEL", "")
LLM_API_KEY  = os.environ.get("LUC_LLM_API_KEY", "")

AGENT_MAX_ITERATIONS = 100
AGENT_STALL_WINDOW   = 5
AGENT_MAX_ERRORS     = 5

MODELS_CONFIG_PATH = os.path.join(DATA_DIR, "models.json")

EMBED_MODEL = os.environ.get(
    "LUC_EMBED_MODEL",
    "BAAI/bge-small-zh-v1.5" if LANG == "zh" else "paraphrase-multilingual-MiniLM-L12-v2",
)
