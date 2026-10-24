"""Extraction caching and offset-based pagination/scrolling for webtrawl.

Allows AI agents to inspect long web pages in bounded chunks without re-fetching
over the network or re-printing previously seen characters.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path(
    os.environ.get(
        "WEBTRAWL_CACHE_DIR",
        str(Path.home() / ".cache" / "webtrawl" / "extract_cache"),
    )
)
DEFAULT_TTL = 3600  # 1 hour
# A failed extraction is retried far sooner than a good one: the page may recover,
# and the failure carries no content worth keeping for an hour.
FAILURE_TTL = 300  # 5 minutes
# Bump when the extraction gate gets stricter: a record written under an older
# gate may hold a wall that the current code would reject, so it must not be
# replayed as a success. Cache entries are disposable, invalidation is free.
CACHE_SCHEMA = 2


def _cache_path(url: str, cache_dir: Path | None = None) -> Path:
    base = cache_dir or DEFAULT_CACHE_DIR
    url_hash = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()
    return base / f"{url_hash}.json"


def _read_record(path: Path) -> dict[str, Any] | None:
    """Load one cache record, or None when it is absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _is_fresh(record: dict[str, Any] | None, ttl_seconds: int) -> bool:
    """Report whether a record exists, matches the cache schema, and is inside its TTL."""
    return (
        bool(record)
        and record.get("schema") == CACHE_SCHEMA
        and time.time() - record.get("timestamp", 0) <= ttl_seconds
    )


def _write_record(path: Path, record: dict[str, Any]) -> None:
    """Merge a partial record onto the URL's existing one and refresh its timestamp.

    Merging keeps the text, the failure and the raw HTML of one URL in a single
    record, so writing the HTML after a text extract cannot erase the text.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = _read_record(path) or {}
        merged.update(record)
        merged["schema"] = CACHE_SCHEMA
        merged["timestamp"] = time.time()
        path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # non-fatal if disk write fails


def get_cached_extract(
    url: str, ttl_seconds: int = DEFAULT_TTL, cache_dir: Path | None = None
) -> str | None:
    """Retrieve cached extracted text for a URL if present, fresh, and successful."""
    path = _cache_path(url, cache_dir)
    if not path.is_file():
        return None
    record = _read_record(path)
    if not _is_fresh(record, ttl_seconds) or not record:
        return None
    if record.get("error"):
        return None
    content = record.get("content")
    return content if content else None


def get_cached_failure(
    url: str, ttl_seconds: int = FAILURE_TTL, cache_dir: Path | None = None
) -> dict[str, Any] | None:
    """Retrieve a fresh cached failure for a URL as {"error", "suggestion"}, else None.

    Replaying a failure without touching the network is what stops a page that
    cannot be extracted from costing the full cascade on every retry.
    """
    path = _cache_path(url, cache_dir)
    if not path.is_file():
        return None
    record = _read_record(path)
    if not _is_fresh(record, ttl_seconds) or not record or not record.get("error"):
        return None
    return {"error": record["error"], "suggestion": record.get("suggestion")}


def get_cached_html(
    url: str, ttl_seconds: int = DEFAULT_TTL, cache_dir: Path | None = None
) -> str | None:
    """Retrieve the raw HTML cached for a URL if present and fresh."""
    path = _cache_path(url, cache_dir)
    if not path.is_file():
        return None
    record = _read_record(path)
    if not _is_fresh(record, ttl_seconds) or not record:
        return None
    html = record.get("html")
    return html if html else None


def set_cached_extract(
    url: str,
    content: str,
    cache_dir: Path | None = None,
    *,
    error: str | None = None,
    suggestion: str | None = None,
    html: str | None = None,
) -> None:
    """Save a URL's extraction outcome: its text, or a failure with its fix.

    ``error`` and ``suggestion`` are written unconditionally so a later success
    clears an earlier failure instead of merging on top of it.
    """
    record: dict[str, Any] = {
        "url": url.strip(),
        "content": content,
        "error": error,
        "suggestion": suggestion,
    }
    if html:
        record["html"] = html
    _write_record(_cache_path(url, cache_dir), record)


def set_cached_html(url: str, html: str, cache_dir: Path | None = None) -> None:
    """Attach raw fetched HTML to a URL's record, keeping its text or failure fields."""
    _write_record(_cache_path(url, cache_dir), {"url": url.strip(), "html": html})


def paginate_content(
    text: str,
    offset: int = 0,
    max_chars: int = 4000,
    url: str = "",
) -> dict[str, Any]:
    """Slice text from offset up to max_chars and compute pagination metadata."""
    total_chars = len(text)
    offset = max(0, offset)

    if offset >= total_chars and total_chars > 0:
        return {
            "content": "",
            "total_chars": total_chars,
            "offset": offset,
            "end_offset": total_chars,
            "chars_returned": 0,
            "remaining_chars": 0,
            "percent_read": 100.0,
            "has_more": False,
            "next_offset": None,
            "next_cmd": None,
            "is_beyond_end": True,
            "url": url,
        }

    if max_chars <= 0:
        slice_text = text[offset:]
        end_offset = total_chars
    else:
        slice_text = text[offset : offset + max_chars]
        end_offset = min(total_chars, offset + max_chars)

    chars_returned = len(slice_text)
    remaining_chars = max(0, total_chars - end_offset)
    percent_read = (
        round((end_offset / total_chars * 100), 1) if total_chars > 0 else 100.0
    )
    has_more = remaining_chars > 0
    next_offset = end_offset if has_more else None
    if max_chars == 4000 or max_chars <= 0:
        next_cmd = f'webtrawl extract "{url}" --offset {next_offset}' if has_more else None
    else:
        next_cmd = f'webtrawl extract "{url}" --offset {next_offset} --max-chars {max_chars}' if has_more else None

    return {
        "content": slice_text,
        "total_chars": total_chars,
        "offset": offset,
        "end_offset": end_offset,
        "chars_returned": chars_returned,
        "remaining_chars": remaining_chars,
        "percent_read": percent_read,
        "has_more": has_more,
        "next_offset": next_offset,
        "next_cmd": next_cmd,
        "is_beyond_end": False,
        "url": url,
    }


def format_extract_text(res: dict[str, Any]) -> str:
    """Format paginated slice as human/agent readable text with progress banners."""
    url = res.get("url", "")
    total = f"{res['total_chars']:,}"

    if res.get("is_beyond_end"):
        return (
            f"[URL: {url}]\n"
            f"[Offset {res['offset']} is beyond end of document (total: {total} chars). No more content.]\n"
        )

    # Complete document in single view
    if res["offset"] == 0 and not res["has_more"]:
        header = f"[URL: {url}]\n[Document length: {total} chars | 100% complete]\n\n"
        return header + res["content"]

    # Sliced chunk
    off_start = f"{res['offset']:,}"
    off_end = f"{res['end_offset']:,}"
    remaining = f"{res['remaining_chars']:,}"
    pct = f"{res['percent_read']}%"

    header = (
        f"[URL: {url}]\n"
        f"[Offset: {off_start}..{off_end} of {total} chars | {pct} read | {remaining} chars remaining]\n"
    )
    if res["has_more"] and res.get("next_cmd"):
        header += f"[Next chunk: {res['next_cmd']}]\n"
        header += f'[Or skip pagination: webtrawl extract "{url}" --max-chars 0 (whole document, one call)]\n'
    header += "\n"

    if res["has_more"]:
        footer = (
            f"\n\n[End of chunk: chars {off_start}..{off_end} of {total} | {remaining} chars remaining ({pct} read)]\n"
            f"[To read next chunk: {res['next_cmd']}]"
            f'\n[Or skip pagination: webtrawl extract "{url}" --max-chars 0 (whole document, one call)]'
        )
    else:
        footer = f"\n\n[End of document reached (total: {total} chars | 100% read)]"

    return header + res["content"] + footer

