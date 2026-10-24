"""OCR for webtrawl: a thin adapter over the vendored engine in ``_vendor.cli_common.ocr``.

The engine is not implemented here on purpose. Reading text from a picture is the
same problem for every tool that needs it, so the chain, the model registry, the
cache and the health file all live in `_vendor/cli_common/ocr.py` and
`ocr_models.toml`. This module only maps the CLI's names onto that engine:
`-p gemini` means the Gemini provider in the registry, `-p vlm` means the
OpenAI-compatible one, and `-p auto` means the `fast` profile.

Nothing in this file may re-implement a provider. If a model changes, it changes
in the registry, for every caller at once.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from _vendor.cli_common import ocr as engine

OcrError = engine.OcrError
OcrResult = engine.OcrResult

# CLI name -> registry provider. `None` means "use the profile".
PROVIDER_ALIASES: dict[str, str | None] = {
    "auto": None,
    "gemini": "gemini-flash-lite",
    "gemini-deep": "gemini-flash",
    "vlm": "vlm-free",
    "mistral": "mistral-ocr",
    "tesseract": "tesseract",
}


def clean_text(text: str) -> str:
    """Trim trailing whitespace per line, never touching the line breaks."""
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def ocr_file(
    path: Path,
    *,
    provider: str = "auto",
    lang: str = "fra",
    timeout: float | None = None,
    no_cache: bool = False,
    fast: bool = True,
    structured: bool = True,
) -> OcrResult:
    """Read one picture through the shared engine.

    `provider="auto"` picks the `fast` profile (`deep` when the caller asks for
    the reasoning model). An explicit provider pins the chain to that one entry,
    which is what makes "I asked for Gemini" a promise the tool keeps.
    """
    alias = PROVIDER_ALIASES.get(provider, provider)
    if alias is None:
        return engine.read_image(
            path,
            profile="fast" if fast else "deep",
            lang=lang,
            deadline=timeout,
            no_cache=no_cache,
            structured=structured,
        )
    return engine.read_image(path, providers=[alias], lang=lang, deadline=timeout, no_cache=no_cache, structured=structured)


async def ocr_file_async(
    path: Path,
    *,
    provider: str = "auto",
    lang: str = "fra",
    timeout: float | None = None,
    no_cache: bool = False,
    fast: bool = True,
    structured: bool = True,
) -> OcrResult:
    """Same contract as `ocr_file`, without blocking the event loop.

    The CLI runs a batch of pages through one loop: a blocking call there would
    serialize every picture behind the previous one and make the concurrency flag
    a decoration.
    """
    alias = PROVIDER_ALIASES.get(provider, provider)
    if alias is None:
        return await engine.read_image_async(
            path,
            profile="fast" if fast else "deep",
            lang=lang,
            deadline=timeout,
            no_cache=no_cache,
            structured=structured,
        )
    return await engine.read_image_async(
        path, providers=[alias], lang=lang, deadline=timeout, no_cache=no_cache, structured=structured
    )


def available_providers() -> list[dict[str, Any]]:
    """Describe the registry for the `providers` command, key source included."""
    rows: list[dict[str, Any]] = []
    for entry in engine.available():
        rows.append(
            {
                "provider": str(entry["provider"]),
                "model": str(entry.get("model", "")),
                "ready": str(entry.get("ready", "no")),
                "source": str(entry.get("source", "none")),
                "note": f"{entry.get('quality', '')} | {entry.get('cost', '')}".strip(" |"),
            }
        )
    return rows
