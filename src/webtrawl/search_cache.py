"""On-disk cache for internet search results (web and news)."""
from __future__ import annotations

import hashlib
import os
import pickle
import time
from pathlib import Path
from typing import Any, Optional, Tuple

DEFAULT_TTL = 300  # 5 minutes
DEFAULT_DIR = Path.home() / ".cache" / "webtrawl" / "search_cache"


def _path(key_tuple: Tuple[Any, ...], cache_dir: Path | None = None) -> Path:
    base = cache_dir or DEFAULT_DIR
    base.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(repr(key_tuple).encode("utf-8")).hexdigest()
    return base / f"{key}.pkl"


def get_cached(key_tuple: Tuple[Any, ...], cache_dir: Path | None = None, ttl: int = DEFAULT_TTL) -> Optional[dict]:
    path = _path(key_tuple, cache_dir)
    try:
        if not path.exists():
            return None
        if os.path.getmtime(path) + ttl < time.time():
            return None
        with open(path, "rb") as fh:
            data = pickle.load(fh)
        if not isinstance(data, dict) or "rows" not in data:
            return None
        return data
    except Exception:
        return None


def set_cached(key_tuple: Tuple[Any, ...], rows: Any, meta: Any = None, cache_dir: Path | None = None) -> None:
    path = _path(key_tuple, cache_dir)
    try:
        with open(path, "wb") as fh:
            pickle.dump({"rows": rows, "meta": meta}, fh, protocol=4)
    except Exception:
        pass
