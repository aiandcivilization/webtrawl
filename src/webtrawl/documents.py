"""Document pipeline: download and convert PDF/Office files to Markdown.

Search indexes return a lot of primary-source documents — Senate records,
Cour des comptes notes, INSEE publications, parliamentary reports. The prose
extractor cannot read them: `r.jina.ai` returns an empty or garbled text layer
for a PDF, and the completeness gate then fails the command with a bare
"no text layer" error. The agent that was told to "just extract the URL" is
left holding a discovery list whose best sources are unreadable, and the
tempting workaround is a raw `curl` plus a local PDF parser — a bypass that
leaves the tool, not the workflow, in charge.

This module closes the gap inside the tool: `webtrawl extract URL` on a
document URL downloads the file once, converts it with MarkItDown, and hands the
markdown to the same pagination and cache pipeline every web page already uses.
`web` and `news` mark the row with its document type so the caller knows which
of the results are files and not pages.

Detection is extension-based on purpose. Content sniffing would cost one extra
round trip on every single extract just to maybe catch the rare document served
from an extensionless URL, which is a bad trade for a step almost nobody needs.
"""

from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from webtrawl.providers import USER_AGENT

# MarkItDown is the single converter for every format below. It is already this
# monorepo's document dependency (book-downloading-cli) and the mandated path for
# PDF in the media-extraction skill, so a second format-specific extractor here
# would only create a second truth for the same job.
DOC_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "docx",
    ".ppt": "ppt",
    ".pptx": "pptx",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".xlsb": "xlsb",
    ".odt": "odt",
    ".ods": "ods",
    ".odp": "odp",
    ".rtf": "rtf",
    ".epub": "epub",
    ".djvu": "djvu",
    ".csv": "csv",
}

# Conversion is local CPU work on a possibly scanned file, not time spent
# waiting on the network, so it gets its own budget. Same reasoning and value
# as book_downloading_tool.book_logic.CONVERT_TIMEOUT_SECONDS.
CONVERT_TIMEOUT_SECONDS = 180
DOWNLOAD_TIMEOUT_SECONDS = 90.0
MAX_BYTES = 200 * 1024 * 1024
CHUNK_BYTES = 65536
SNIFF_BYTES = 512

# First bytes of every format MarkItDown reads, so a file can be told apart from
# a page of bot-wall HTML that merely carries a .pdf name.
DOCUMENT_MAGIC: tuple[bytes, ...] = (
    b"%PDF-",  # pdf
    b"PK\x03\x04",  # docx / xlsx / pptx / odt / ods / odp / epub (zip container)
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # legacy doc / xls / ppt (OLE2)
    b"{\\rtf",  # rtf
)
# Formats that ARE plain text, so a `text/*` content type is the expected answer
# for them and must never be read as a sign of a page disguised as a document.
TEXT_NATIVE_KINDS = frozenset({"csv"})
HTML_LEAD = (b"<!doctype", b"<html", b"<?xml", b"<HTML", b"<!DOCTYPE")


class DocumentError(Exception):
    """The URL looked like a document but could not be downloaded or converted."""


def _sniff(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(SNIFF_BYTES)


def sniff_error(path: Path, content_type: str, kind: str) -> str | None:
    """Return why the payload is not a document, or None when it looks fine.

    The declared Content-Type cannot be trusted on its own: a bot wall or a login
    redirect happily answers 200 `application/pdf` with an HTML challenge page.
    Trusting it would feed MarkItDown a wall of navigation chrome, and MarkItDown
    would return that chrome as if it were the report body. The bytes are the
    only evidence that survives a lying header.
    """
    head = _sniff(path)
    stripped = head.lstrip()[:16].lower()
    if any(head.startswith(marker) for marker in HTML_LEAD) or stripped.startswith((b"<!doctype", b"<html")):
        return (
            f"the {content_type} response is actually an HTML page (bot wall, login redirect, "
            f"or a moved file), not a document: first bytes are {head[:16]!r}. "
            "Extract the landing page with webtrawl extract to find the direct file link."
        )
    if content_type.startswith("text/") and kind not in TEXT_NATIVE_KINDS and not head.startswith(DOCUMENT_MAGIC):
        return (
            f"the response declares {content_type} and carries no document signature "
            f"(first bytes {head[:16]!r})"
        )
    return None


def document_kind(url: str) -> str | None:
    """Return the document kind for a URL, or None when the URL is a web page.

    Only the path suffix is inspected. A `.pdf` sitting in a query string is a
    link target on an HTML page, not that page's own type, so it must not flip
    the caller out of the prose pipeline.
    """
    if not url.startswith(("http://", "https://")):
        return None
    suffix = Path(urlparse(url).path).suffix.lower()
    return DOC_EXTENSIONS.get(suffix)


def download(url: str, *, timeout: float = DOWNLOAD_TIMEOUT_SECONDS) -> tuple[Path, str, str]:
    """Fetch a document to local disk. Returns (path, content_type, kind).

    Raises DocumentError when the server answers with an HTML page, whether it
    admits it in the Content-Type or not.
    """
    kind = document_kind(url) or ""
    suffix = Path(urlparse(url).path).suffix.lower() or ".bin"
    handle, raw_path = tempfile.mkstemp(prefix="webtrawl-doc-", suffix=suffix)
    os.close(handle)
    path = Path(raw_path)

    try:
        with requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type in ("", "text/html", "application/xhtml+xml"):
                raise DocumentError(
                    f"{url} answered Content-Type '{content_type or 'unset'}' instead of a file: "
                    "the document URL serves an HTML page (bot wall, login redirect, or a moved file). "
                    "Open the landing page with webtrawl extract to find the direct file link."
                )
            written = 0
            with path.open("wb") as sink:
                for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise DocumentError(
                            f"{url} exceeds the {MAX_BYTES // (1024 * 1024)} MB download cap; "
                            "raise MAX_BYTES in documents.py if this document really matters"
                        )
                    sink.write(chunk)
    except DocumentError:
        path.unlink(missing_ok=True)
        raise
    except requests.RequestException as exc:
        path.unlink(missing_ok=True)
        raise DocumentError(f"could not download {url}: {exc}") from exc

    if path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise DocumentError(f"{url} downloaded to an empty file")

    problem = sniff_error(path, content_type, kind)
    if problem:
        path.unlink(missing_ok=True)
        raise DocumentError(f"{url} is not a usable document: {problem}")
    return path, content_type, kind


def _run_with_timeout(work: Any, timeout: float) -> Any:
    """Run a callable off-thread and give up after `timeout` seconds.

    `shutdown(wait=False)` is deliberate: a plain `with` block would block on the
    stuck thread and turn the timeout into a hang, which is the exact failure
    this guard exists to prevent.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(work)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            future.cancel()
            raise DocumentError(
                f"conversion did not finish within {timeout:.0f}s (large or scanned file?)"
            ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def to_markdown(path: Path, *, timeout: float = CONVERT_TIMEOUT_SECONDS) -> str:
    """Convert a downloaded document to Markdown with MarkItDown."""
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise DocumentError(
            "markitdown is missing from this tool's environment; "
            "reinstall with: uv tool install --force webtrawl-tool"
        ) from exc

    result = _run_with_timeout(lambda: MarkItDown().convert(str(path)), timeout)
    text = (result.text_content or "").strip()
    if not text:
        raise DocumentError(
            f"MarkItDown found no text layer in {path.name}: scanned image PDF, or an encrypted file. "
            "OCR the file first, or use a text-native source for the same document."
        )
    return text


def extract_document(url: str, *, timeout: float = DOWNLOAD_TIMEOUT_SECONDS) -> tuple[str, dict[str, Any]]:
    """Download and convert a document URL. Returns (markdown, meta)."""
    kind = document_kind(url)
    if kind is None:
        raise DocumentError(f"{url} is not a document URL; nothing to download")

    path, content_type, kind = download(url, timeout=timeout)
    size = path.stat().st_size
    try:
        text = to_markdown(path)
    finally:
        path.unlink(missing_ok=True)

    meta = {
        "url": url,
        "type": kind,
        "content_type": content_type,
        "transport": "markitdown",
        "downloaded_bytes": size,
    }
    return text, meta
