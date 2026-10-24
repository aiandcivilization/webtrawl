"""Image acquisition: resolve, download and assemble pictures, including walled ones.

Why this module exists (measured 2026-10-24, on a genealogy session that lost
half a day to it):

- A page can carry its only evidence as a picture: a scan, a register page, a
  certificate. The prose extractor returns nothing for it, so the agent reaches
  for `curl`, which answers 403, and concludes the CDN has a bot wall.
- The measured cause was neither a captcha nor a missing account: the CDN
  answers 200 as soon as the request carries the `Referer` of the page that
  embeds the picture. Every picture this module fetches carries it.
- The viewer caps its plain thumbnail at 1000 px (`thumbnail-1100-fill.jpg`
  answers 406). The native scan (3888x5184 here) is only served as a 256 px
  tile pyramid, so readable resolution means fetching the tiles that cover the
  region and stitching them: 336 tiles, 2.3 MB, 5 s over six threads.
- Launching the patched Chromium once per picture costs about 9 s each, so it is
  kept as the ESCALATION for a real wall, never the default transport.

The contract:

1. `resolve()` turns a page URL into the picture it documents, upgrading a
   viewer thumbnail to the largest size the server still accepts.
2. `fetch_batch()` downloads over plain TLS on one keep-alive async session with a
   per-URL retry, escalating the whole batch to ONE browser session only for
   what still refuses.
3. `assemble()` rebuilds a native-resolution page, or only the rectangle asked
   for, from the tile grid.
4. Every byte is cached on disk, so the second pass of an investigation costs
   nothing.

Output stays TOON (cli-tool-packaging Rule 1); diagnostics go to stderr.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from webtrawl import providers

CACHE_DIR = Path(
    os.environ.get(
        "WEBTRAWL_IMAGE_CACHE_DIR",
        str(Path.home() / ".cache" / "webtrawl" / "images"),
    )
)

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp")
ZOOMIFY_RE = re.compile(r"/viewer/zoomify/api/actes/([0-9a-f]{2})/([0-9a-f]{2})/(\d+)/?")
MAX_SIDE_RE = re.compile(r"maxside=\d+")
THUMB_SIZE_RE = re.compile(r"thumbnail-(\d+)-fill")
# The largest is tried first; the server answers 406 above its cap, so the
# refusal costs one request and tells us the ceiling.
THUMB_LADDER = (1000, 900, 600, 300)
TILE_SIZE_FALLBACK = 256
# Native resolution means one tile per 256 px of the scan. Beyond this the
# caller asked for the wrong thing (a whole book at native level).
MAX_TILES = 600
CACHE_MAX_BYTES = 2 * 1024**3
CACHE_PRUNE_TRIGGER_BYTES = 1024 * 1024
WORKERS = 6
RETRIES = 2
BACKOFF_SECONDS = 0.6
# A couple of transient tile failures are normal under a burst. A real wall loses
# EVERY request, so escalating on a partial loss only buys a 30 s stall for tiles
# that a second TLS pass would have delivered.
BROWSER_ESCALATION_RATIO = 1.0


@dataclass
class Picture:
    """One picture to obtain: where it comes from and what to keep of it."""

    url: str
    page_url: str | None = None
    viewer: str | None = None  # Zoomify base when the URL is a tile viewer
    region: tuple[int, int, int, int] | None = None
    full: bool = False
    name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def is_image_url(url: str) -> bool:
    """Report whether a URL is a picture by suffix, ignoring the query string."""
    return urlparse(url).path.lower().endswith(IMAGE_SUFFIXES)


# --- cache ------------------------------------------------------------------


def _cache_path(key: str, suffix: str = ".jpg") -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}{suffix}"


def _read_cache(key: str, suffix: str = ".jpg") -> bytes | None:
    path = _cache_path(key, suffix)
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def _write_cache(key: str, data: bytes, suffix: str = ".jpg") -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key, suffix).write_bytes(data)
    except OSError:
        pass  # a later cache miss is cheaper than a failed download now
    if len(data) > CACHE_PRUNE_TRIGGER_BYTES:
        _prune_cache()


def _prune_cache() -> None:
    """Keep the picture cache under its cap, dropping the oldest files first.

    A stitched register page is 0.6-2.5 MB, so a 200-page volume is a few
    hundred megabytes on its own. The cache is a convenience, never the only
    copy of an investigation, so ageing out is safe.
    """
    try:
        files = sorted(CACHE_DIR.glob("*"), key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in files)
    except OSError:
        return
    for path in files:
        if total <= CACHE_MAX_BYTES:
            break
        try:
            total -= path.stat().st_size
            path.unlink()
        except OSError:
            continue


def _suffix_for(content_type: str, url: str) -> str:
    """Pick a file suffix from the response media type, then from the URL."""
    ct = (content_type or "").lower()
    for suffix, marker in (
        (".jpg", "jpeg"),
        (".png", "png"),
        (".webp", "webp"),
        (".gif", "gif"),
        (".tif", "tiff"),
    ):
        if marker in ct:
            return suffix
    path = urlparse(url).path.lower()
    for suffix in IMAGE_SUFFIXES:
        if path.endswith(suffix):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


# --- resolution -------------------------------------------------------------


def _largest_variant(url: str) -> str:
    """Ask a resize service for its biggest copy instead of the listed thumbnail.

    Two shapes are handled because they are the two the archives use: a
    `thumbnail-NNN-fill` path segment and a `maxside=NNN` query parameter.
    """
    if THUMB_SIZE_RE.search(url):
        return THUMB_SIZE_RE.sub(f"thumbnail-{THUMB_LADDER[0]}-fill", url)
    if MAX_SIDE_RE.search(url):
        return MAX_SIDE_RE.sub(f"maxside={THUMB_LADDER[0]}", url)
    return url


def _name_for(page_url: str | None, url: str) -> str:
    """Build a stable, readable file name from the page the picture documents."""
    if page_url:
        parts = [p for p in urlparse(page_url).path.split("/") if p]
        stem = "-".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "image")
    else:
        stem = Path(urlparse(url).path).stem or "image"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem)[:80] or "image"


def zoomify_base(url: str) -> str | None:
    """Return the Zoomify API base a viewer URL belongs to, if any."""
    match = ZOOMIFY_RE.search(urlparse(url).path)
    if not match:
        return None
    parsed = urlparse(url)
    return (
        f"{parsed.scheme}://{parsed.netloc}"
        f"/viewer/zoomify/api/actes/{match.group(1)}/{match.group(2)}/{match.group(3)}"
    )


def _image_candidates(html: str) -> list[str]:
    """Collect picture URLs a page publishes, most evidential first.

    OpenGraph comes first because it is the picture the publisher itself chose
    to represent the document; `img` tags follow, minus icons and spacers.
    """
    found: list[str] = []
    for pattern in (
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    ):
        found.extend(re.findall(pattern, html, re.I))
    found.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I))
    clean: list[str] = []
    for url in found:
        if not url.startswith(("http://", "https://")):
            continue
        lowered = url.lower()
        if any(bad in lowered for bad in ("/logo", "sprite", "pixel.", "spacer", "icon-", "/img/1x1")):
            continue
        if url not in clean:
            clean.append(url)
    return clean


def page_variants(url: str, spec: str) -> list[str]:
    """Expand a page range against a viewer URL whose last path segment is a number.

    One register has hundreds of views, and fetching them one command at a time is
    the difference between a batch and an afternoon. The rule is deliberately
    narrow: only a trailing numeric segment is varied, so a URL that does not carry
    one is refused instead of silently returning the same page N times.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts or not parts[-1].isdigit():
        raise providers.BadInputError(
            f"--pages needs a URL ending in a view number, got {url!r}: "
            "for example https://host/archival-registers/view/33132/57"
        )
    numbers: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, _sep, high = chunk.partition("-")
            if not (low.isdigit() and high.isdigit()):
                raise providers.BadInputError(f"bad page range {chunk!r}: use 50-57 or 1,4,9")
            start, end = int(low), int(high)
            if end < start:
                raise providers.BadInputError(f"reversed page range {chunk!r}")
            if end - start > 400:
                raise providers.BadInputError(f"range {chunk!r} is above the 400-page guard")
            numbers.extend(range(start, end + 1))
        elif chunk.isdigit():
            numbers.append(int(chunk))
        else:
            raise providers.BadInputError(f"bad page value {chunk!r}")
    if not numbers:
        raise providers.BadInputError("--pages is empty")
    parsed = urlparse(url)
    prefix = "/" + "/".join(parts[:-1])
    return [parsed._replace(path=f"{prefix}/{number}").geturl() for number in numbers]


PLACEHOLDER_MARKERS = ("data:image/gif;base64,r0lgod", "data:image/svg", "/placeholder", "spacer.gif", "blank.gif", "lazy-load")


def page_pictures(html: str) -> tuple[list[str], int]:
    """Count the pictures a page publishes and the placeholders it ships instead.

    A scraping agent must be told, in the extraction output itself, that the
    evidence is a picture and that the page is full of placeholders. Otherwise it
    reads empty markdown, concludes the page is empty, and gives up on a document
    that is one `image` call away.
    """
    pictures = _image_candidates(html)
    placeholders = 0
    for tag in re.findall(r"<img[^>]*>", html, re.I):
        lowered = tag.lower()
        src = re.search(r'src=["\']([^"\']*)["\']', lowered)
        if not src or not src.group(1).strip() or any(marker in lowered for marker in PLACEHOLDER_MARKERS):
            placeholders += 1
    return pictures, placeholders


def resolve(target: str, *, referer: str | None = None, full: bool = False,
            region: tuple[int, int, int, int] | None = None) -> list[Picture]:
    """Turn one target into the picture it identifies.

    A target that is already a picture is used as-is. A page target is fetched
    once and its published picture is taken, upgraded to the largest variant the
    server accepts and marked as a Zoomify viewer when the URL is a tile
    endpoint, so `--full`/`--region` can assemble native resolution later.
    """
    if is_image_url(target) or ZOOMIFY_RE.search(urlparse(target).path):
        return [
            Picture(
                url=_largest_variant(target),
                page_url=referer,
                viewer=zoomify_base(target),
                full=full,
                region=region,
                name=_name_for(referer, target),
            )
        ]

    html, _tier = providers.fetch_html(target)
    if not html:
        raise providers.TransientError(f"could not read {target} to find its picture")
    for candidate in _image_candidates(html):
        base = zoomify_base(candidate)
        picture = Picture(
            url=_largest_variant(candidate),
            page_url=target,
            viewer=base,
            full=full,
            region=region,
            name=_name_for(target, candidate),
        )
        return [picture]  # the first candidate is the document's own picture
    raise providers.TransientError(
        f"{target} publishes no picture: the page may render it client-side. "
        f"Read it with `webtrawl extract {target} --brute`, then pass the picture URL."
    )


# --- transports -------------------------------------------------------------


# One keep-alive session per process. A tile pyramid is dozens of small pictures
# from a single host: a fresh TLS connection per tile paid the handshake fifty
# times, and a thread pool hid that cost instead of removing it.
MAX_INFLIGHT = 8

_SESSION: Any = None
_SESSION_LOOP: Any = None


def _session() -> Any:
    """Return the async HTTP session bound to the running loop."""
    global _SESSION, _SESSION_LOOP
    loop = asyncio.get_running_loop()
    if _SESSION is None or _SESSION_LOOP is not loop:
        from curl_cffi.requests import AsyncSession

        _SESSION = AsyncSession(max_clients=MAX_INFLIGHT, impersonate="chrome", timeout=25)
        _SESSION_LOOP = loop
    return _SESSION


async def fetch_many_async(
    urls: list[str],
    referer: str | None,
    *,
    sem: asyncio.Semaphore | None = None,
    attempts: int = RETRIES,
) -> dict[str, tuple[bytes, str]]:
    """Fetch pictures concurrently over one keep-alive session.

    `sem` is shared by the whole command, so a batch of pages cannot multiply the
    burst that the CDN starts challenging: the bound is per command, not per page.
    Cancellation is real here: an abandoned request is cancelled, not left
    running behind a thread that the interpreter then waits for at exit.
    """
    if not urls:
        return {}
    gate = sem or asyncio.Semaphore(MAX_INFLIGHT)
    session = _session()
    headers = {"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer

    async def one(url: str) -> tuple[str, tuple[bytes, str] | None]:
        for attempt in range(attempts + 1):
            try:
                async with gate:
                    resp = await session.get(url, headers=headers, allow_redirects=True)
            except Exception:
                resp = None
            if resp is not None:
                content_type = resp.headers.get("content-type") or ""
                if resp.status_code == 200 and content_type.lower().startswith("image"):
                    return url, (resp.content, content_type)
            if attempt < attempts:
                await asyncio.sleep(BACKOFF_SECONDS * (attempt + 1))
        return url, None

    answers = await asyncio.gather(*(one(url) for url in urls))
    return {url: got for url, got in answers if got is not None}


async def _fetch_descriptor(url: str, referer: str | None, *, attempts: int = 3) -> str | None:
    """Read a small XML descriptor over plain TLS, retrying a transient refusal.

    The descriptor is `application/xml`, so it cannot go through the picture
    path (which only accepts an `image/*` answer). A single 403 here would be
    read as "this viewer has no tiles", which is wrong and silent, so the retry
    is the point of this function.
    """
    session = _session()
    headers = {"Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    for attempt in range(attempts):
        try:
            resp = await session.get(url, headers=headers)
            if resp.status_code == 200 and resp.text.lstrip().startswith("<"):
                return resp.text
        except Exception:
            pass
        if attempt + 1 < attempts:
            await asyncio.sleep(BACKOFF_SECONDS * (attempt + 1))
    return None


_FETCH_JS = """
async ([urls, pool]) => {
  const out = {};
  const queue = urls.slice();
  const worker = async () => {
    while (queue.length) {
      const u = queue.shift();
      let last = null;
      for (let attempt = 0; attempt <= 2; attempt++) {
        try {
          const r = await fetch(u, {credentials: 'include'});
          const ct = r.headers.get('content-type') || '';
          const buf = await r.arrayBuffer();
          if (r.status === 200 && ct.startsWith('image')) {
            const b = new Uint8Array(buf);
            let s = '';
            for (let i = 0; i < b.length; i += 8192) s += String.fromCharCode.apply(null, b.subarray(i, i + 8192));
            out[u] = {status: 200, ct, bytes: b.length, b64: btoa(s)};
            last = null;
            break;
          }
          last = {status: r.status, ct, bytes: b.length, text: new TextDecoder().decode(buf).slice(0, 160)};
          if (![403, 429, 502, 503, 406].includes(r.status)) break;
        } catch (e) {
          last = {error: String(e)};
        }
        await new Promise(z => setTimeout(z, 1200 * (attempt + 1)));
      }
      if (last) out[u] = last;
    }
  };
  await Promise.all(Array.from({length: Math.min(pool, queue.length || 1)}, worker));
  return out;
}
"""


def _browser_fetch(urls: list[str], *, referer: str, timeout: float = 150.0) -> dict[str, tuple[bytes, str]]:
    """Escalate the whole batch to ONE browser session and fetch in-page.

    This is the last resort, for a site whose images are bound to a challenge
    cookie rather than to a referer. Launching it once for the batch is what
    keeps the cost at ~9 s total instead of ~9 s per picture.
    """
    if not urls:
        return {}
    from scrapling.fetchers import StealthyFetcher

    sink: list[dict[str, Any]] = []

    def action(page: Any) -> None:
        sink.append(page.evaluate(_FETCH_JS, [urls, 3]))

    StealthyFetcher.fetch(
        referer,
        headless=True,
        network_idle=True,
        solve_cloudflare=True,
        timeout=int(timeout * 1000),
        extra_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
        page_action=action,
        **providers._browser_kwargs(),
    )
    answers = sink[0] if sink else {}
    out: dict[str, tuple[bytes, str]] = {}
    for url, payload in answers.items():
        if payload.get("status") == 200 and payload.get("b64"):
            out[url] = (base64.b64decode(payload["b64"]), payload.get("ct") or "")
    return out


TILE_DEADLINE = 30.0


async def fetch_batch(
    urls: list[str], *, referer: str, sem: asyncio.Semaphore | None = None
) -> dict[str, tuple[bytes, str]]:
    """Fetch a batch over the plain TLS session, with a bounded cost.

    Three rules, each from a measured stall:

    - **A rate limit is not a transient failure.** When most of a batch is
      refused at once, retrying every URL with a backoff turns a 5-second wait
      into a multi-minute stall. Half-or-more missing is a wall: say so and stop.
    - **A partial loss is retried once**, on the same session, before anything
      expensive is considered.
    - **The whole batch is bounded**, so no combination of retries can outlive
      the deadline the caller believes it has.
    """
    try:
        async with asyncio.timeout(TILE_DEADLINE):
            got = await fetch_many_async(urls, referer, sem=sem, attempts=1)
            missing = [u for u in urls if u not in got]
            if missing and len(missing) >= len(urls):
                # Every URL refused: a wall, and one browser session is the only
                # thing that has ever cleared it.
                try:
                    got.update(await asyncio.to_thread(_browser_fetch, missing, referer=referer, timeout=30.0))
                except Exception:  # no browser installed, or the session died: keep what we have
                    pass
                return got
            if len(missing) > len(urls) / 2:
                raise providers.TransientError(
                    f"the CDN refused {len(missing)}/{len(urls)} tiles at once: that is a rate limit, "
                    "not a bad coordinate. Retry in about a minute."
                )
            if missing:
                got.update(await fetch_many_async(missing, referer, sem=sem, attempts=1))
            return got
    except TimeoutError as exc:
        raise providers.TransientError(
            f"the CDN held {len(urls)} tiles past the {TILE_DEADLINE:g}s deadline: retry in a minute."
        ) from exc


# --- Zoomify ----------------------------------------------------------------


@dataclass
class TileGrid:
    width: int
    height: int
    tile: int
    max_level: int

    @property
    def cols(self) -> int:
        return math.ceil(self.width / self.tile)

    @property
    def rows(self) -> int:
        return math.ceil(self.height / self.tile)


def _parse_properties(xml: str) -> tuple[int, int, int] | None:
    match = re.search(r"<IMAGE_PROPERTIES[^>]*>", xml)
    if not match:
        return None
    attrs = dict(re.findall(r'([A-Z]+)="(\d+)"', match.group(0)))
    try:
        return int(attrs["WIDTH"]), int(attrs["HEIGHT"]), int(attrs.get("TILESIZE", TILE_SIZE_FALLBACK))
    except (KeyError, ValueError):
        return None


def _level_dims(grid: TileGrid, level: int) -> tuple[int, int]:
    """Pixel size of one zoom level: each level halves the one above it."""
    factor = 2 ** max(0, grid.max_level - level)
    return math.ceil(grid.width / factor), math.ceil(grid.height / factor)


_GRID_CACHE: dict[str, TileGrid] = {}


async def tile_grid(viewer: str, *, referer: str) -> TileGrid:
    """Read the tile pyramid's real dimensions and find its deepest level.

    The deepest level is probed rather than derived: viewers disagree on the
    off-by-one, and reading a downscaled level by accident is invisible in the
    output, so one wasted request buys certainty.
    """
    if viewer in _GRID_CACHE:
        return _GRID_CACHE[viewer]
    xml = await _fetch_descriptor(f"{viewer}/ImageProperties.xml", referer) or ""
    parsed = _parse_properties(xml)
    if not parsed:
        # A refused descriptor is transient, and calling it "no tile set" sends
        # the caller to the wrong remedy: it drops --full or gives up, when the
        # right move is to wait a few seconds and retry.
        raise providers.TransientError(
            f"the CDN refused the tile descriptor of {viewer}: retry in a few seconds"
        )
    width, height, tile = parsed
    # Compute the deepest level instead of probing every level up to it: the
    # pyramid doubles the width at each step, so the depth is arithmetic. One
    # confirming request replaces eight probes, which is 1.6 s saved per page in
    # a batch. The neighbours are kept as a fallback because viewers disagree by
    # one at the boundary.
    computed = math.ceil(math.log2(max(width, height) / tile)) + 1
    max_level = 0
    for candidate in (computed, computed - 1, computed + 1):
        if candidate < 0:
            continue
        probe = await fetch_many_async([f"{viewer}/TileGroup0/{candidate}-0-0.jpg"], referer, attempts=0)
        if probe:
            max_level = candidate
            break
    if max_level == 0 and not await fetch_many_async([f"{viewer}/TileGroup0/0-0-0.jpg"], referer, attempts=0):
        raise providers.TransientError(f"no tile level answered for {viewer}: retry in a few seconds")
    grid = TileGrid(width=width, height=height, tile=tile, max_level=max_level)
    _GRID_CACHE[viewer] = grid
    return grid


def _tile_coords(
    grid: TileGrid,
    region: tuple[int, int, int, int] | None,
    level: int | None = None,
) -> tuple[list[tuple[int, int]], tuple[int, int, int, int]]:
    """List the (col, row) tiles covering the rectangle, clamped to the scan.

    A level below the deepest one halves the pixel count each step: for OCR the
    half-resolution level keeps the writing legible while fetching four times
    fewer tiles, which is the only lever that matters when the model's own
    latency dominates.
    """
    target = grid.max_level if level is None else max(0, min(level, grid.max_level))
    factor = 2 ** (grid.max_level - target)
    level_width, level_height = _level_dims(grid, target)
    x, y, w, h = region if region else (0, 0, grid.width, grid.height)
    x, y, w, h = x // factor, y // factor, w // factor, h // factor
    x = max(0, min(x, level_width - 1))
    y = max(0, min(y, level_height - 1))
    w = max(1, min(w, level_width - x))
    h = max(1, min(h, level_height - y))
    first_col, last_col = x // grid.tile, (x + w - 1) // grid.tile
    first_row, last_row = y // grid.tile, (y + h - 1) // grid.tile
    coords = [(c, r) for r in range(first_row, last_row + 1) for c in range(first_col, last_col + 1)]
    return coords, (x, y, w, h)


async def assemble(viewer: str, grid: TileGrid, *, referer: str,
                   region: tuple[int, int, int, int] | None = None,
                   level: int | None = None,
                   sem: asyncio.Semaphore | None = None) -> tuple[bytes, int, int]:
    """Stitch the native-resolution tiles covering `region` into one JPEG.

    The `TileGroupN` path segment is always 0: the CDN ignores it and derives
    the tile from `level-col-row` (measured: `TileGroup9/6-0-0.jpg` answers
    200), so computing the group would be dead code.
    """
    from PIL import Image

    target = grid.max_level if level is None else max(0, min(level, grid.max_level))
    coords, (x, y, w, h) = _tile_coords(grid, region, target)
    if len(coords) > MAX_TILES:
        raise providers.BadInputError(
            f"{len(coords)} tiles needed (cap {MAX_TILES}): ask for a --region, "
            "or take the 1000 px thumbnail instead of the native page"
        )
    urls = [f"{viewer}/TileGroup0/{target}-{col}-{row}.jpg" for col, row in coords]
    got = await fetch_batch(urls, referer=referer, sem=sem)
    missing = [u for u in urls if u not in got]
    if missing:
        raise providers.TransientError(
            f"{len(missing)}/{len(urls)} tiles did not come back (first: {missing[0]}). "
            "Retry: the burst limit clears in a few seconds, and a --region fetches fewer tiles."
        )

    canvas = Image.new("RGB", (w, h), "white")
    for url, (col, row) in zip(urls, coords, strict=True):
        data, _content_type = got[url]
        with Image.open(BytesIO(data)) as tile:
            canvas.paste(tile.convert("RGB"), (col * grid.tile - x, row * grid.tile - y))
    out = BytesIO()
    canvas.save(out, format="JPEG", quality=92)
    return out.getvalue(), w, h


# --- orchestration ----------------------------------------------------------


def image_size(data: bytes) -> tuple[int, int]:
    """Read a picture's pixel dimensions without decoding all its pixels."""
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            return image.size
    except Exception:
        return (0, 0)


def _finish(picture: Picture, data: bytes, out_dir: Path, *, transport: str, cached: bool) -> dict[str, Any]:
    """Write one picture to disk and describe it for the TOON row."""
    suffix = _suffix_for("", picture.url)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{picture.name}{suffix}"
    path.write_bytes(data)
    width, height = image_size(data)
    return {
        "name": picture.name,
        "path": str(path),
        "width": width,
        "height": height,
        "bytes": len(data),
        "transport": transport,
        "cached": "hit" if cached else "miss",
        "url": picture.url,
        "source": "tiles" if (picture.full and picture.viewer) else ("page" if picture.page_url else "direct"),
    }


async def run_async(
    targets: list[str],
    *,
    out_dir: Path,
    full: bool = False,
    region: tuple[int, int, int, int] | None = None,
    level: int | None = None,
    no_cache: bool = False,
    concurrency: int = MAX_INFLIGHT,
    sem: asyncio.Semaphore | None = None,
) -> list[dict[str, Any]]:
    """Resolve every target, download it, and return one row per picture.

    Everything that waits is concurrent, and one semaphore bounds the whole
    command: three pages must not open three times the connections a single page
    may open. Pages, grids and tiles overlap; only the pixel stitching is
    serial, because it is CPU work on bytes already in memory.
    """
    resolved = await asyncio.gather(
        *(asyncio.to_thread(resolve, target, full=full, region=region) for target in targets)
    )
    pictures: list[Picture] = [picture for group in resolved for picture in group]

    gate = sem or asyncio.Semaphore(concurrency)
    rows: list[dict[str, Any]] = []

    plain: list[Picture] = []
    tiles: list[Picture] = []
    for picture in pictures:
        if picture.full and picture.viewer:
            tiles.append(picture)
        else:
            if picture.full:
                from webtrawl.render import err

                err(f"note: {picture.url} exposes no tile pyramid; keeping the largest flat picture it serves")
            plain.append(picture)

    async def one_tile(picture: Picture) -> dict[str, Any]:
        viewer = picture.viewer or ""
        referer = picture.page_url or picture.url
        cache_key = f"full:{viewer}:{region}:{level}"
        if not no_cache:
            cached = _read_cache(cache_key)
            if cached is not None:
                return _finish(picture, cached, out_dir, transport="cache", cached=True)
        grid = await tile_grid(viewer, referer=referer)
        data, width, height = await assemble(
            viewer, grid, referer=referer, region=region, level=level, sem=gate
        )
        if not no_cache:
            _write_cache(cache_key, data)
        row = _finish(picture, data, out_dir, transport="tls", cached=False)
        used = grid.max_level if level is None else max(0, min(level, grid.max_level))
        row.update({"width": width, "height": height, "native": f"{grid.width}x{grid.height}", "level": used})
        return row

    if tiles:
        rows.extend(await asyncio.gather(*(one_tile(picture) for picture in tiles)))

    # Thumbnail path: one cache lookup, one bounded fetch, one optional escalation.
    todo: list[Picture] = []
    for picture in plain:
        cached = None if no_cache else _read_cache(picture.url)
        if cached is not None:
            rows.append(_finish(picture, cached, out_dir, transport="cache", cached=True))
        else:
            todo.append(picture)
    if todo:
        referer = next((p.page_url for p in todo if p.page_url), todo[0].url)
        got = await fetch_batch([p.url for p in todo], referer=referer, sem=gate)
        for picture in todo:
            result = got.get(picture.url)
            if result is None:
                rows.append({"name": picture.name, "url": picture.url, "status": 0, "error": "no transport answered"})
                continue
            data, content_type = result
            if not no_cache:
                _write_cache(picture.url, data, _suffix_for(content_type, picture.url))
            rows.append(_finish(picture, data, out_dir, transport="tls", cached=False))

    order = {picture.name: index for index, picture in enumerate(pictures)}
    rows.sort(key=lambda row: order.get(row.get("name", ""), len(pictures)))
    return rows


def run(
    targets: list[str],
    *,
    out_dir: Path,
    full: bool = False,
    region: tuple[int, int, int, int] | None = None,
    level: int | None = None,
    no_cache: bool = False,
) -> list[dict[str, Any]]:
    """Synchronous entry point for callers that are not already async."""
    return asyncio.run(
        run_async(
            targets,
            out_dir=out_dir,
            full=full,
            region=region,
            level=level,
            no_cache=no_cache,
        )
    )


def parse_region(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse ``X,Y,W,H`` in native pixels; None when the flag is absent."""
    if not value:
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise providers.BadInputError("--region expects X,Y,W,H in native pixels, for example 900,1200,600,400")
    x, y, w, h = (int(p) for p in parts)
    if w <= 0 or h <= 0:
        raise providers.BadInputError("--region width and height must be positive")
    return x, y, w, h
