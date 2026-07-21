"""
memory/embedder.py — CPU embedding via sentence-transformers
"""
import os
import threading
import numpy as np
from config import EMBED_MODEL

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")         # suppress unauthenticated-token warning
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")

_model = None
_model_lock = threading.Lock()


def _load():
    global _model
    # Plain "if _model is None: load" is a check-then-set race: encode() is
    # called from many concurrent task threads (every memory write/search,
    # across every concurrent session in api.py), so two threads can both
    # see _model as None on the first call and both start loading the
    # SentenceTransformer model at once — wasteful (double memory/load time)
    # at best. Double-checked locking: cheap fast path once loaded, no
    # redundant loads on the cold-start race.
    if _model is None:
        with _model_lock:
            if _model is None:
                import warnings
                from sentence_transformers import SentenceTransformer
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    _model = SentenceTransformer(EMBED_MODEL, device="cpu")
    return _model


def encode(text: str) -> bytes:
    vec = _load().encode(text, normalize_embeddings=True, show_progress_bar=False)
    return vec.astype(np.float32).tobytes()


def encode_batch(texts: list) -> list:
    vecs = _load().encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.astype(np.float32).tobytes() for v in vecs]


def cosine(a: bytes, b: bytes) -> float:
    va = np.frombuffer(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    return float(np.dot(va, vb))  # 已归一化，点积 = 余弦相似度
