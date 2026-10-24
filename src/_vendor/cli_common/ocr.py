"""Shared OCR engine (vendored): picture -> text with a hedged provider chain.

Why this lives in cli-common and not in one tool: reading text from a picture is
the same problem for the genealogy workflow (a register scan), the document
workflow (a photographed page) and any future tool. Two copies would drift, and
the second copy is always the one that forgets the `[illisible]` rule.

The engine answers one question: *what does this picture say, as fast as
possible?* Three mechanisms keep the answer fast, and each is measured:

1. **A profile is a fallback chain, not a preference list.** `fast` tries
   Gemini flash-lite; if it has not answered within `hedge_after` seconds the
   free OpenRouter model starts **in parallel** and the first correct answer is
   returned. A degraded provider therefore costs one hedge delay, not its whole
   timeout.
2. **A failing provider is skipped, not retried.** A provider that fails is put
   in a cooldown (default 5 min) in a small health file, so the next twenty
   command invocations do not each pay the same timeout.
3. **A transcription is cached by content, never by path.** Re-reading the same
   crop costs a file read.

The registry (`ocr_models.toml`) is data: a new model, provider or profile is an
edit there, not a code change. Keys resolve from the environment, then
`~/.config/webtrawl/ocr.toml`, then the legacy per-tool config of
webtrawl, so no existing setup breaks.

The output is never corrected. An unreadable word stays `[illisible]`: a
completed word would be a fabricated source, which is worse than a gap.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

# Condensed from the best public guidance, and from two findings that matter
# more than any wording:
#
# - DeepSeek-OCR's own samples ship two prompts; practitioners report that the
#   plain "Free OCR" prompt beats the markdown/grounding one for HANDWRITING
#   (markdown mode optimises layout, not letter shapes).
# - Prompt rewrites move handwriting accuracy far less than resolution does:
#   one measured comparison went 58% -> 83% by enlarging the crop, with prompt
#   changes alone changing nothing. Hence `--region` at native level, not a
#   cleverer sentence.
# - OpenAI's own vision cookbook: ask for high verbosity for literal
#   transcription, and request JSON when the caller needs structure.
#
# So: verbatim, no correction, uncertainty marked, line-addressable, and a
# structured form that maps straight onto TOON.
PLAIN_PROMPT = (
    "OCR. Transcris tout le texte visible, dans l'ordre de lecture, sans rien corriger, "
    "résumer ni traduire. Conserve les retours à la ligne, la ponctuation et l'orthographe "
    "d'origine. Pour un mot incertain, écris-le suivi de [?] ; pour un mot illisible, écris "
    "[illisible]. Ne rends que le texte."
)

STRUCTURED_PROMPT = (
    "OCR. Transcris tout le texte manuscrit ou imprimé, dans l'ordre de lecture, sans corriger, "
    "compléter, moderniser ni traduire. Réponds UNIQUEMENT par un objet JSON valide, sans texte "
    "autour et sans balise de code, de la forme : "
    '{"language":"fr","lines":[{"n":1,"text":"...","confidence":"high|medium|low"}],'
    '"entities":{"persons":[],"dates":[],"places":[]},"notes":""}. '
    "Règles : le champ text contient exactement les mots lus ; un mot incertain finit par [?] ; "
    "un mot illisible s'écrit [illisible] ; n'invente jamais un mot, un nom ou une date ; "
    "si la page est vide, lines vaut []."
)

SHARED_CONFIG = Path(
    os.environ.get("WEBTRAWL_OCR_CONFIG", str(Path.home() / ".config" / "webtrawl" / "ocr.toml"))
)
LEGACY_CONFIGS = (Path.home() / ".config" / "webtrawl" / "config.toml",)
CACHE_DIR = Path(
    os.environ.get("WEBTRAWL_OCR_CACHE", str(Path.home() / ".cache" / "webtrawl" / "ocr"))
)
HEALTH_FILE = CACHE_DIR / "health.json"
COOLDOWN_SECONDS = 60
MAX_INLINE_BYTES = 12 * 1024 * 1024
# A provider KIND maps to one credential name, so `openai` (any OpenAI-compatible
# host) shares the `vlm` key whatever host it points at.
KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "openai": "VLM_API_KEY",
    "vlm": "VLM_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}
KEY_NAME = {"gemini": "gemini", "openai": "vlm", "vlm": "vlm", "mistral": "mistral"}


class OcrError(Exception):
    """No provider in the chain produced text."""


@dataclass
class OcrResult:
    """One transcription plus the evidence of how it was produced."""

    text: str
    provider: str
    model: str
    seconds: float
    cached: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# --- registry ---------------------------------------------------------------


def _load_registry() -> dict[str, Any]:
    """Load the packaged model registry, then let the shared config override it."""
    try:
        raw = resources.files(__package__).joinpath("ocr_models.toml").read_text(encoding="utf-8")
        registry = tomllib.loads(raw)
    except (FileNotFoundError, tomllib.TOMLDecodeError, ModuleNotFoundError):
        registry = {}
    overrides = _read_toml(SHARED_CONFIG)
    if overrides.get("providers"):
        registry.setdefault("providers", {}).update(overrides["providers"])
    if overrides.get("profiles"):
        registry.setdefault("profiles", {}).update(overrides["profiles"])
    if overrides.get("defaults"):
        registry.setdefault("defaults", {}).update(overrides["defaults"])
    return registry


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, tomllib.TOMLDecodeError):
        return {}


def profiles() -> dict[str, list[str]]:
    """Return every profile name and its ordered provider chain."""
    registry = _load_registry()
    return {name: list(chain) for name, chain in (registry.get("profiles") or {}).items()}


def default_profile() -> str:
    """The profile used when the caller does not name one."""
    return str((_load_registry().get("defaults") or {}).get("profile") or "fast")


def default_lang() -> str:
    """The local engine's default language pack."""
    return str((_load_registry().get("defaults") or {}).get("lang") or "fra")


def _provider(name: str) -> dict[str, Any]:
    entry = (_load_registry().get("providers") or {}).get(name)
    if not entry:
        raise OcrError(f"unknown OCR provider {name!r}: add it to ocr_models.toml")
    return entry


# --- keys -------------------------------------------------------------------


def resolve_key(provider_kind: str) -> tuple[str | None, str]:
    """Resolve a key for a provider kind: env, shared config, then legacy configs."""
    name = KEY_NAME.get(provider_kind, provider_kind)
    env_var = KEY_ENV.get(provider_kind)
    if env_var:
        value = (os.environ.get(env_var) or "").strip()
        if value:
            return value, "env"
    shared = _read_toml(SHARED_CONFIG)
    key = ((shared.get("keys") or {}).get(name) or "")
    if isinstance(key, str) and key.strip():
        return key.strip(), str(SHARED_CONFIG)
    for path in LEGACY_CONFIGS:
        data = _read_toml(path)
        key = ((data.get("keys") or {}).get(name) or "")
        if isinstance(key, str) and key.strip():
            return key.strip(), str(path)
    return None, "none"


# --- cache and health -------------------------------------------------------


def _digest(data: bytes, provider: str, model: str, lang: str) -> str:
    return hashlib.sha256(data + f"|{provider}|{model}|{lang}".encode()).hexdigest()


def _cache_read(digest: str) -> str | None:
    try:
        path = CACHE_DIR / f"{digest}.md"
        return path.read_text(encoding="utf-8") if path.is_file() else None
    except OSError:
        return None


def _cache_write(digest: str, text: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{digest}.md").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _health() -> dict[str, float]:
    try:
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _mark_failed(name: str, reason: str = "") -> None:
    """Record a failure for one cooldown window.

    Deliberately short (60 s): a provider that fails once is usually a spike, and
    a long blacklist makes the next command silently worse. The reason is kept so
    `ocr health` can explain the skip instead of leaving it mysterious.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = _health()
        data[name] = time.time()
        HEALTH_FILE.write_text(json.dumps(data), encoding="utf-8")
        if reason:
            log = CACHE_DIR / "last_failures.log"
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {name}: {reason[:300]}\n")
    except OSError:
        pass


def _mark_ok(name: str) -> None:
    try:
        data = _health()
        if data.pop(name, None) is not None:
            HEALTH_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _in_cooldown(name: str) -> bool:
    stamp = _health().get(name)
    return bool(stamp) and (time.time() - float(stamp)) < COOLDOWN_SECONDS


def reset_health() -> None:
    """Forget every provider failure, so a fixed provider is tried again."""
    try:
        HEALTH_FILE.unlink()
    except OSError:
        pass


# --- providers --------------------------------------------------------------


def _mime_for(path: Path) -> str:
    return {
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".avif": "image/avif",
    }.get(path.suffix.lower(), "image/jpeg")


def _http_json(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        return json.load(urllib.request.urlopen(request, timeout=timeout))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:300]
        raise OcrError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise OcrError(f"request failed: {exc}") from exc


def _decode_structured(raw: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """Parse the model's JSON answer, tolerating a code fence around it.

    A model that wraps its JSON in ```json is not failing; a model that returns
    prose is. The difference decides whether the caller gets structured lines or
    the raw answer, and the caller is told which happened.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return None
    clean: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if isinstance(line, dict) and "text" in line:
            clean.append(
                {
                    "n": line.get("n", index),
                    "text": str(line.get("text", "")).strip(),
                    "confidence": line.get("confidence", ""),
                }
            )
        elif isinstance(line, str):
            clean.append({"n": index, "text": line.strip(), "confidence": ""})
    entities = payload.get("entities") if isinstance(payload.get("entities"), dict) else {}
    joined = "\n".join(line["text"] for line in clean if line["text"])
    return joined, clean, entities


def _prompt(structured: bool) -> str:
    """The instruction sent to a vision model."""
    return STRUCTURED_PROMPT if structured else PLAIN_PROMPT


_HTTP_SESSION: Any = None
_HTTP_LOOP: Any = None


def _http_session() -> Any:
    """Return one keep-alive async session for the whole process.

    A race starts up to four providers, and a batch starts several races: opening
    a fresh TLS connection each time paid the handshake per call. One session
    reuses the sockets and, more importantly, is cancellable -- an abandoned
    provider stops at the await instead of running on inside a thread that the
    interpreter then waits for at exit.
    """
    global _HTTP_SESSION, _HTTP_LOOP
    loop = asyncio.get_running_loop()
    if _HTTP_SESSION is None or _HTTP_LOOP is not loop:
        from curl_cffi.requests import AsyncSession

        _HTTP_SESSION = AsyncSession(max_clients=8, impersonate="chrome", timeout=90)
        _HTTP_LOOP = loop
    return _HTTP_SESSION


async def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """POST a JSON body and return the decoded answer.

    Falls back to the blocking client in a worker thread when `curl_cffi` is not
    installed, so a tool that only depends on cli-common still works.
    """
    try:
        session = _http_session()
    except ImportError:
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        return await asyncio.to_thread(_http_json, request, timeout)
    try:
        resp = await session.post(url, json=body, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - every transport failure is the same to the caller
        raise OcrError(f"request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise OcrError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise OcrError(f"answer was not JSON: {resp.text[:200]}") from exc


async def _run_gemini(entry: dict[str, Any], path: Path, key: str, timeout: float, *, structured: bool) -> OcrResult:
    body = {
        "contents": [
            {
                "parts": [
                    {"text": _prompt(structured)},
                    {"inline_data": {"mime_type": _mime_for(path), "data": base64.b64encode(path.read_bytes()).decode()}},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingLevel": entry.get("thinking", "MINIMAL")},
        },
    }
    started = time.monotonic()
    payload = await _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{entry['model']}:generateContent",
        body,
        {"x-goog-api-key": key, "Content-Type": "application/json"},
        timeout,
    )
    candidates = payload.get("candidates") or []
    if not candidates:
        raise OcrError(f"no candidate: {json.dumps(payload)[:200]}")
    text = "".join(part.get("text", "") for part in candidates[0]["content"]["parts"]).strip()
    finish = candidates[0].get("finishReason") or "STOP"
    extra: dict[str, Any] = {"finish": finish}
    if finish != "STOP":
        extra["note"] = f"answer cut by the model ({finish}): read a smaller region"
    return OcrResult(
        text=text,
        provider="gemini",
        model=str(entry["model"]),
        seconds=round(time.monotonic() - started, 1),
        extra=extra,
    )


async def _run_openai(entry: dict[str, Any], path: Path, key: str, timeout: float, *, structured: bool) -> OcrResult:
    body = {
        "model": entry["model"],
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _prompt(structured)},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{_mime_for(path)};base64,{base64.b64encode(path.read_bytes()).decode()}"},
                    },
                ],
            }
        ],
    }
    started = time.monotonic()
    payload = await _post_json(
        f"{str(entry.get('base_url', '')).rstrip('/')}/chat/completions",
        body,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout,
    )
    choices = payload.get("choices") or []
    if not choices:
        raise OcrError(f"no choice: {json.dumps(payload)[:200]}")
    content = choices[0]["message"]["content"]
    usage = payload.get("usage") or {}
    return OcrResult(
        text=(content if isinstance(content, str) else str(content)).strip(),
        provider="openai-compatible",
        model=str(entry["model"]),
        seconds=round(time.monotonic() - started, 1),
        extra={"cost_usd": usage.get("cost", 0), "tokens": usage.get("total_tokens")},
    )


async def _run_mistral(entry: dict[str, Any], path: Path, key: str, timeout: float, *, structured: bool) -> OcrResult:
    body = {
        "model": entry["model"],
        "document": {
            "type": "image_url",
            "image_url": f"data:{_mime_for(path)};base64,{base64.b64encode(path.read_bytes()).decode()}",
        },
    }
    started = time.monotonic()
    payload = await _post_json(
        "https://api.mistral.ai/v1/ocr",
        body,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout,
    )
    pages = payload.get("pages") or []
    text = "\n\n".join((page.get("markdown") or "").strip() for page in pages).strip()
    return OcrResult(
        text=text,
        provider="mistral",
        model=str(entry["model"]),
        seconds=round(time.monotonic() - started, 1),
        extra={"pages": len(pages), "usage": payload.get("usage_info")},
    )


def _run_tesseract(entry: dict[str, Any], path: Path, timeout: float, lang: str) -> OcrResult:
    binary = shutil.which("tesseract")
    if binary is None:
        raise OcrError("tesseract is not installed")
    started = time.monotonic()
    proc = subprocess.run(  # noqa: S603 - fixed binary, argument list, no shell
        [binary, str(path), "stdout", "-l", lang, "--psm", "6"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise OcrError(f"tesseract failed: {proc.stderr.strip()[:200]}")
    return OcrResult(
        text=proc.stdout.strip(),
        provider="tesseract",
        model=f"tesseract+{lang}",
        seconds=round(time.monotonic() - started, 2),
        extra={"note": "local engine: reliable on printed text, unreliable on handwriting"},
    )


async def _dispatch(entry: dict[str, Any], path: Path, *, lang: str, timeout: float, structured: bool = True) -> OcrResult:
    kind = entry.get("kind")
    if kind == "tesseract":
        return await asyncio.to_thread(_run_tesseract, entry, path, timeout, lang)
    key, _source = resolve_key(str(kind))
    if not key:
        raise OcrError(f"no key for {kind}: set {KEY_ENV.get(str(kind), str(kind).upper() + '_API_KEY')}")
    if kind == "gemini":
        return await _run_gemini(entry, path, key, timeout, structured=structured)
    if kind == "openai":
        return await _run_openai(entry, path, key, timeout, structured=structured)
    if kind == "mistral":
        return await _run_mistral(entry, path, key, timeout, structured=structured)
    raise OcrError(f"unknown provider kind {kind!r}")


# --- engine -----------------------------------------------------------------


async def read_image_async(
    path: str | Path,
    *,
    profile: str | None = None,
    lang: str | None = None,
    deadline: float | None = None,
    no_cache: bool = False,
    providers: list[str] | None = None,
    structured: bool = True,
) -> OcrResult:
    """Transcribe one picture with a hedged fallback chain.

    The chain starts one provider, hedges to the next after its `hedge_after`
    seconds, and returns the first non-empty answer. The overall wait is bounded
    by `deadline`, and every abandoned provider is a cancelled task -- there is
    no straggler to pay for after the answer is gone.
    """
    picture = Path(path)
    if not picture.is_file():
        raise OcrError(f"no such file: {picture}")
    if picture.stat().st_size > MAX_INLINE_BYTES:
        raise OcrError(f"{picture.name} is above the {MAX_INLINE_BYTES} inline limit: crop it first")

    registry = _load_registry()
    defaults = registry.get("defaults") or {}
    chosen_profile = profile or defaults.get("profile") or "fast"
    chain = providers or list((registry.get("profiles") or {}).get(chosen_profile) or [])
    if not chain:
        raise OcrError(f"profile {chosen_profile!r} is empty or unknown")
    language = lang or str(defaults.get("lang") or "fra")
    budget = float(deadline if deadline is not None else defaults.get("deadline_seconds", 20))

    data = picture.read_bytes()
    entries: list[tuple[str, dict[str, Any]]] = []
    floor: list[tuple[str, dict[str, Any]]] = []
    for name in chain:
        entry = _provider(name)
        model = str(entry.get("model", name))
        digest = _digest(data, name, model, language)
        if not no_cache:
            cached = _cache_read(digest)
            if cached is not None:
                return OcrResult(text=cached, provider=str(entry.get("kind")), model=model, seconds=0.0, cached=True)
        if _in_cooldown(name):
            continue
        (floor if entry.get("floor") else entries).append((name, entry))
    if not entries:
        entries, floor = floor, []
    if not entries:
        raise OcrError("every provider in the chain is in cooldown: run `ocr-reset-health`, or wait 5 minutes")

    started = time.monotonic()
    failures: list[str] = []
    tasks: dict[asyncio.Task[OcrResult], tuple[str, dict[str, Any]]] = {}
    queue = list(entries)
    next_hedge = 0.0
    try:
        while queue or tasks:
            elapsed = time.monotonic() - started
            remaining = budget - elapsed
            if remaining <= 0:
                failures.append(f"deadline of {budget:g}s reached")
                break
            if not tasks or (queue and elapsed >= next_hedge):
                name, entry = queue.pop(0)
                call_timeout = max(1.0, min(float(entry.get("timeout", 60)), remaining))
                task = asyncio.create_task(
                    _dispatch(entry, picture, lang=language, timeout=call_timeout, structured=structured)
                )
                tasks[task] = (name, entry)
                next_hedge = elapsed + float(entry.get("hedge_after", 2.0))
                continue
            done, _pending = await asyncio.wait(
                list(tasks), timeout=min(remaining, 1.0), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                name, entry = tasks.pop(task)
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    continue
                except Exception as exc:  # noqa: BLE001 - one provider failing is data, not a crash
                    failures.append(f"{name}: {exc}")
                    _mark_failed(name, str(exc))
                    continue
                if not result.text.strip():
                    failures.append(f"{name}: returned no text")
                    _mark_failed(name, "returned no text")
                    continue
                _mark_ok(name)
                model = str(entry.get("model", name))
                if structured:
                    decoded = _decode_structured(result.text)
                    if decoded is None:
                        result.extra["structured"] = "failed: model returned prose"
                    else:
                        text, lines, entities = decoded
                        result.text = text or result.text
                        result.extra["structured"] = "ok"
                        result.extra["lines"] = lines
                        result.extra["entities"] = entities
                _cache_write(_digest(data, name, model, language), result.text)
                result.extra["profile"] = chosen_profile
                result.extra["hedged"] = bool(tasks or queue)
                return result
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    for name, entry in floor:
        try:
            result = await _dispatch(entry, picture, lang=language, timeout=float(entry.get("timeout", 60)), structured=False)
        except Exception as exc:  # noqa: BLE001 - the floor must never raise past the caller
            failures.append(f"{name}: {exc}")
            continue
        if result.text.strip():
            result.extra["profile"] = chosen_profile
            result.extra["floor"] = True
            # The floor is not a substitute for a vision model on handwriting:
            # say so where the caller reads the result, not only in the docs.
            result.extra["note"] = (
                "FLOOR ENGINE: no vision provider answered; tesseract is reliable on "
                "printed text only, so treat this transcription as unreliable"
            )
            return result
        failures.append(f"{name}: returned no text")
    detail = "; ".join(failures) or "deadline reached before any provider answered"
    raise OcrError(detail)


def read_image(path: str | Path, **kwargs: Any) -> OcrResult:
    """Blocking wrapper, for callers that are not async."""
    return asyncio.run(read_image_async(path, **kwargs))


async def read_many_async(
    paths: list[str | Path],
    *,
    profile: str | None = None,
    lang: str | None = None,
    deadline: float | None = None,
    concurrency: int = 4,
    no_cache: bool = False,
    structured: bool = True,
) -> list[OcrResult | Exception]:
    """Transcribe several pictures concurrently, preserving the input order."""
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(path: str | Path) -> OcrResult | Exception:
        async with gate:
            try:
                return await read_image_async(
                    path,
                    profile=profile,
                    lang=lang,
                    deadline=deadline,
                    no_cache=no_cache,
                    structured=structured,
                )
            except Exception as exc:  # noqa: BLE001 - one bad picture must not sink the batch
                return exc

    return await asyncio.gather(*(one(path) for path in paths))


def read_many(paths: list[str | Path], **kwargs: Any) -> list[OcrResult | Exception]:
    """Blocking wrapper around the concurrent batch."""
    return asyncio.run(read_many_async(paths, **kwargs))


def available() -> list[dict[str, Any]]:
    """Describe every provider: kind, model, whether its key resolves, and cost."""
    rows: list[dict[str, Any]] = []
    for name, entry in (_load_registry().get("providers") or {}).items():
        kind = str(entry.get("kind"))
        if kind == "tesseract":
            ready, source = ("yes", "binary") if shutil.which("tesseract") else ("no", "missing")
        else:
            key, source = resolve_key(kind)
            ready = "yes" if key else "no"
        rows.append(
            {
                "provider": name,
                "kind": kind,
                "model": entry.get("model"),
                "ready": ready,
                "source": source,
                "cost": entry.get("cost", ""),
                "quality": entry.get("quality", ""),
                "cooldown": "yes" if _in_cooldown(name) else "no",
            }
        )
    return rows
