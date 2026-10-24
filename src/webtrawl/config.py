"""Key/config resolution for webtrawl.

Resolution order for the Brave API key (first hit wins):
1. ``BRAVE_API_KEY`` environment variable
2. this tool's own config: ``~/.config/webtrawl/config.toml`` -> [keys] brave


Tavily, parallel and tinyfish follow the same shape.
Every provider key is settable with ``webtrawl config set <provider> KEY``.

The key is never logged; only its *source* is reported.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

TOOL_CONFIG_PATH = Path(
    os.environ.get("WEBTRAWL_CONFIG", str(Path.home() / ".config" / "webtrawl" / "config.toml"))
)


def load_tool_config(path: Path | None = None) -> dict:
    """Load this tool's TOML config; return {} when missing or invalid."""
    p = path or TOOL_CONFIG_PATH
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, tomllib.TOMLDecodeError):
        return {}


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, tomllib.TOMLDecodeError):
        return {}


def resolve_brave_key(env: dict | None = None) -> tuple[str | None, str]:
    """Return (api_key_or_None, source) with source in env|tool-config|none."""
    environment = os.environ if env is None else env
    key = (environment.get("BRAVE_API_KEY") or "").strip()
    if key:
        return key, "env"

    cfg = load_tool_config()
    key = ((cfg.get("keys") or {}).get("brave") or "")
    if isinstance(key, str) and key.strip():
        return key.strip(), "tool-config"

    return None, "none"


def resolve_tavily_key(env: dict | None = None) -> tuple[str | None, str]:
    """Return (api_key_or_None, source) with source in env|tool-config|none.

    Unlike brave, a missing key is NOT fatal: tavily accepts a keyless
    access-mode header (rate-capped). Callers decide whether to degrade.
    """
    environment = os.environ if env is None else env
    key = (environment.get("TAVILY_API_KEY") or "").strip()
    if key:
        return key, "env"
    cfg = load_tool_config()
    key = ((cfg.get("keys") or {}).get("tavily") or "")
    if isinstance(key, str) and key.strip():
        return key.strip(), "tool-config"
    return None, "none"


def resolve_parallel_key(env: dict | None = None) -> tuple[str | None, str]:
    """Return (api_key_or_None, source) with source in env|tool-config|none.

    Parallel has no keyless mode: a missing key is fatal for explicit use.
    """
    environment = os.environ if env is None else env
    key = (environment.get("PARALLEL_API_KEY") or "").strip()
    if key:
        return key, "env"
    cfg = load_tool_config()
    key = ((cfg.get("keys") or {}).get("parallel") or "")
    if isinstance(key, str) and key.strip():
        return key.strip(), "tool-config"
    return None, "none"


def resolve_tinyfish_key(env: dict | None = None) -> tuple[str | None, str]:
    """Return (api_key_or_None, source) with source in env|tool-config|none.

    TinyFish search is free at any wallet balance but has no keyless mode: a missing
    key is fatal for explicit use, like parallel.
    """
    environment = os.environ if env is None else env
    key = (environment.get("TINYFISH_API_KEY") or "").strip()
    if key:
        return key, "env"
    cfg = load_tool_config()
    key = ((cfg.get("keys") or {}).get("tinyfish") or "")
    if isinstance(key, str) and key.strip():
        return key.strip(), "tool-config"
    return None, "none"


def set_key(provider: str, value: str, path: Path | None = None) -> Path:
    """Persist provider key into this tool's config.toml with 0600 perms."""
    p = path or TOOL_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = _read_toml(p)
    keys = cfg.get("keys") or {}
    keys[provider] = value
    cfg["keys"] = keys
    lines = []
    for prov, val in keys.items():
        safe = val.replace('"', '\\"')
        lines.append(f'{prov} = "{safe}"')
    body = "[keys]\n" + "\n".join(lines) + "\n"
    p.write_text(body, encoding="utf-8")
    os.chmod(p, 0o600)
    return p
