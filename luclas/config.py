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
            v = v.strip()
            # setup.py never writes quoted values, but a hand-edited .env
            # following the common KEY="value" convention (python-dotenv and
            # most other .env tooling support it) would otherwise have the
            # quote characters taken as part of the literal value — e.g. a
            # WeCom token silently becoming '"abc123"' instead of 'abc123',
            # breaking signature verification with no error message anywhere.
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)


_load_dotenv()


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git"] + args, cwd=BASE_DIR, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return ""


_date         = _git(["log", "-1", "--format=%cd", "--date=short"]) or "unknown"

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

# How long ask_user() waits for a reply on messaging channels (push question,
# block on the session's supplement queue) before giving up.
ASK_USER_TIMEOUT_SECONDS = 600

MODELS_CONFIG_PATH = os.path.join(DATA_DIR, "models.json")

# systemd --user service running api.py, restarted via `luclas api restart` / `/api restart`
API_SERVICE_NAME = os.environ.get("LUC_API_SERVICE_NAME", "luclas-api.service")

EMBED_MODEL = os.environ.get(
    "LUC_EMBED_MODEL",
    "BAAI/bge-small-zh-v1.5" if LANG == "zh" else "paraphrase-multilingual-MiniLM-L12-v2",
)
