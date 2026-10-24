"""webtrawl CLI — multi-provider web/news search + URL extraction.

All output is TOON. There is no --json flag: JSON is transport for machine input,
never an output (cli-tool-packaging Rule 1).
Semantic exit codes: 0 ok | 1 transient | 2 auth missing | 3 bad input | 4 rate limited.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from toon import encode as toon_encode

from _vendor.cli_common.toon_output import render_data_as_toon
from webtrawl import __version__, documents, hydration, images, providers
from webtrawl import ocr as ocr_engine
from webtrawl.config import (
    TOOL_CONFIG_PATH,
    load_tool_config,
    resolve_brave_key,
    resolve_parallel_key,
    resolve_tavily_key,
    resolve_tinyfish_key,
    set_key,
)
from webtrawl.extract_cache import (
    format_extract_text,
    get_cached_extract,
    get_cached_failure,
    get_cached_html,
    paginate_content,
    set_cached_extract,
    set_cached_html,
)
from webtrawl.render import err, fail, format_structured, print_payload, print_results

app = typer.Typer(
    help="Agent-native web search and page extraction. Discovery across Brave, Tavily, Parallel and TinyFish with a keyless Google News last resort; extraction through a four-tier cascade (Jina Reader, TLS fetch, patched Chromium, Internet Archive); TOON output by default.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Manage provider API keys (config file: ~/.config/webtrawl/config.toml)", no_args_is_help=True)
app.add_typer(config_app, name="config")

CountOpt = Annotated[int, typer.Option("--count", "-n", min=1, max=20, help="Number of results (1-20, default 10).")]
FreshnessOpt = Annotated[Optional[str], typer.Option("--freshness", "-f", help="Freshness window: day|week|month|year.")]
CountryOpt = Annotated[Optional[str], typer.Option("--country", help="2-letter country code boost (e.g. FR, US).")]
LangOpt = Annotated[Optional[str], typer.Option("--lang", help="Search language code (e.g. fr, en).")]
OffsetOpt = Annotated[int, typer.Option("--offset", min=0, help="Result pagination offset.")]
CompactOpt = Annotated[bool, typer.Option("--compact", help="Emit compact TOON table without snippet column.")]
FullOpt = Annotated[bool, typer.Option("--full", help="Include snippets column (default behavior; kept for backward compatibility).")]


def _brave_key_or_fail() -> str:
    key, source = resolve_brave_key()
    if not key:
        fail(
            "no Brave API key found",
            code=2,
            suggestion="export BRAVE_API_KEY=... | webtrawl config set brave KEY | run webtrawl providers",
        )
    return key


def _run_provider(func, *args, **kwargs):
    """Call a provider function and map typed errors to semantic exit codes."""
    try:
        return func(*args, **kwargs)
    except providers.BadInputError as exc:
        fail(str(exc), code=3, suggestion="fix the arguments")
    except providers.AuthMissingError as exc:
        fail(str(exc), code=2, suggestion="check your BRAVE_API_KEY / plan at api-dashboard.search.brave.com")
    except providers.RateLimitError as exc:
        fail(str(exc), code=4, suggestion="back off and retry later")
    except providers.IncompleteExtractionError as exc:
        fail(str(exc), code=1, suggestion=exc.suggestion)
    except providers.TransientError as exc:
        fail(str(exc), code=1, suggestion="retry might help")
    except documents.DocumentError as exc:
        fail(str(exc), code=1, suggestion="the message above names the fix; no curl needed")


def _auto_chain(candidates: list[tuple[str, object]]) -> tuple[list[dict], dict]:
    """Try search callables in order; return first success, map last failure."""
    last_error: Exception | None = None
    for name, thunk in candidates:
        try:
            rows, meta = thunk()
            return rows, meta
        except providers.ProviderError as exc:
            last_error = exc
            err(f"{name} unavailable ({exc}); trying next method")
    assert last_error is not None
    suggestions = {
        2: "configure keys: webtrawl config set <provider> KEY",
        4: "all methods exhausted; back off, or retry later with an explicit -m",
    }
    fail(str(last_error), code=last_error.exit_code, suggestion=suggestions.get(last_error.exit_code))


# Providers that refuse to run without a key, with the hint shown on exit 2.
WEB_KEY_REQUIRED = {
    "parallel": ("Parallel", "get one at platform.parallel.ai then: webtrawl config set parallel KEY"),
    "tinyfish": ("TinyFish", "free but keyed: get one at agent.tinyfish.ai/api-keys then: webtrawl config set tinyfish KEY"),
}


def _web_candidates(method: str, query: str, count: int, freshness: str | None, country: str | None, lang: str | None):
    """Ordered list of (name, thunk) honoring the requested method.

    Insertion order of `probes` IS the auto cascade: brave (keyed) > tavily
    (keyless ok) > parallel (keyed) > tinyfish (keyed last resort).
    """
    bkey, _ = resolve_brave_key()
    tkey, _ = resolve_tavily_key()
    pkey, _ = resolve_parallel_key()
    fkey, _ = resolve_tinyfish_key()
    keys = {"brave": bkey, "tavily": tkey, "parallel": pkey, "tinyfish": fkey}
    probes = {
        "brave": lambda: providers.brave_web(bkey or _brave_key_or_fail(), query, count=count, freshness=freshness, country=country, lang=lang),
        "tavily": lambda: providers.tavily_search(query, api_key=tkey, count=count, freshness=freshness),
        "parallel": lambda: providers.parallel_search(query, api_key=pkey, count=count, freshness=freshness),
        "tinyfish": lambda: providers.tinyfish_search(fkey, query, count=count, freshness=freshness, country=country, lang=lang),
    }
    if method in probes:
        if method in WEB_KEY_REQUIRED and keys[method] is None:
            label, hint = WEB_KEY_REQUIRED[method]
            fail(f"no {label} API key found", code=2, suggestion=hint)
        return [(method, probes[method])]
    return [(name, thunk) for name, thunk in probes.items() if name == "tavily" or keys[name]]

@app.command()
def web(
    query: Annotated[str, typer.Argument(help="Search query.")],
    method: Annotated[str, typer.Option("--method", "-m", help="Search method: brave | tavily | parallel | tinyfish | auto (first available in that order).")] = "auto",
    count: CountOpt = 10,
    freshness: FreshnessOpt = None,
    country: CountryOpt = None,
    lang: LangOpt = None,
    offset: OffsetOpt = 0,
    compact: CompactOpt = False,
    full: FullOpt = True,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass the on-disk search-result cache.")] = False,
):
    """STEP 1 (Discovery): Web search across Brave index, Tavily (keyless ok), Parallel, TinyFish.
    
    MANDATORY STEP 2: Follow up with 'webtrawl extract <url>' on candidate URLs to read actual content.
    """
    if method.lower() not in ("brave", "tavily", "parallel", "tinyfish", "auto"):

        fail(f"unknown method {method!r}", code=3, suggestion="use brave | tavily | parallel | tinyfish | auto")
    from webtrawl.search_cache import get_cached, set_cached

    cache_key = ("web", query, method.lower(), count, freshness, country, lang, offset)
    cached = None if no_cache else get_cached(cache_key)
    if cached is None:
        candidates = _web_candidates(method.lower(), query, count, freshness, country, lang)
        rows, meta = _auto_chain(candidates)
        if not no_cache:
            set_cached(cache_key, rows, meta)
    else:
        rows, meta = cached["rows"], cached["meta"]
    print_results(rows, full=full, compact=compact, meta={**meta, "query": query})


@app.command()
def news(
    query: Annotated[str, typer.Argument(help="News query.")],
    method: Annotated[str, typer.Option("--method", "-m", help="News method: brave | tavily | auto (auto = brave if key else tavily keyless; gnews is the last-resort fallback). Parallel and tinyfish have no news vertical in this CLI.")] = "auto",
    count: CountOpt = 10,
    freshness: FreshnessOpt = None,
    country: CountryOpt = None,
    lang: LangOpt = None,
    compact: CompactOpt = False,
    full: FullOpt = True,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass the on-disk search-result cache.")] = False,
):
    """News search: Brave and Tavily verticals, Google News RSS as keyless safety net."""
    m = method.lower()
    if m == "gnews":  # kept for backward compatibility
        m = "auto"
        err("-m gnews deprecated; gnews remains the auto last-resort fallback")
    if m not in ("brave", "tavily", "auto"):
        fail(f"unknown method {method!r}", code=3, suggestion="use brave | tavily | auto (parallel and tinyfish are web-only)")
    bkey, _ = resolve_brave_key()
    tkey, _ = resolve_tavily_key()
    if m == "brave":
        candidates = [("brave", lambda: providers.brave_news(_brave_key_or_fail(), query, count=count, freshness=freshness, country=country, lang=lang))]
    elif m == "tavily":
        candidates = [("tavily", lambda: providers.tavily_search(query, api_key=tkey, count=count, freshness=freshness, topic="news"))]
    else:
        candidates = []
        if bkey:
            candidates.append(("brave", lambda: providers.brave_news(bkey, query, count=count, freshness=freshness, country=country, lang=lang)))
        candidates.append(("tavily", lambda: providers.tavily_search(query, api_key=tkey, count=count, freshness=freshness, topic="news")))
        candidates.append(("gnews", lambda: providers.gnews_news(query, count=count, lang=lang, country=country)))
    from webtrawl.search_cache import get_cached, set_cached

    cache_key = ("news", query, method.lower(), count, freshness, country, lang)
    cached = None if no_cache else get_cached(cache_key)
    if cached is None:
        rows, meta = _auto_chain(candidates)
        if not no_cache:
            set_cached(cache_key, rows, meta)
    else:
        rows, meta = cached["rows"], cached["meta"]
    print_results(rows, full=full, compact=compact, meta={**meta, "query": query})


def _fetch_html_cached(url: str, *, no_cache: bool) -> tuple[str, str]:
    """Fetch page HTML, reusing the cached copy so --payloads/--raw-html do not re-pay.

    The HTML fetched during an extraction is kept in the same record as its outcome,
    so inspecting a page after a failed extract costs no transport. --no-cache
    forces a fresh fetch.
    """
    if not no_cache:
        cached = get_cached_html(url)
        if cached:
            return cached, "cache"
    html, tier = providers.fetch_html(url)
    if html and not no_cache:
        set_cached_html(url, html)
    return html, tier


def _emit_picture_hint(url: str, html: str, text: str = "") -> None:
    """Tell the agent whether the page's evidence is a picture it can read.

    Extraction returns prose. When that prose is empty because the page is a scan
    viewer, the agent needs the next command in the same output, not a hint in the
    documentation it will not read.
    """
    if html:
        pictures, placeholders = images.page_pictures(html)
    else:
        # A reader-tier extraction (Jina) returns markdown, not HTML: its image
        # links are the only evidence of a picture, and they cost nothing to count.
        raw = re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text or "")
        # A reader tier keeps the site's own chrome as images. Counting icons as
        # "pictures" would tell the agent the opposite of the truth.
        noise = ("icon", "logo", "avatar", "sprite", "favicon", "1x1", "pixel", "/bundles/", "/img/")
        # A document picture is a JPEG/WEBP; a UI asset is a GIF, an SVG or a PNG
        # under a bundle path. Counting the chrome would tell the agent the
        # opposite of the truth about a scan.
        pictures = [
            url
            for url in raw
            if url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".webp"))
            and not any(marker in url.lower() for marker in noise)
        ]
        placeholders = 0
    viewer_like = bool(re.search(r"/(view|viewer|archives?|registres?|document|image|ark:)/", url, re.I))
    if not pictures and not placeholders and viewer_like:
        # A reader tier strips OpenGraph, so a scan viewer looks picture-less. The
        # extra HTML fetch is paid only on a URL that is shaped like a document
        # viewer (and cached afterwards), never on an ordinary article.
        html, _tier = _fetch_html_cached(url, no_cache=False)
        if html:
            pictures, placeholders = images.page_pictures(html)
    if not pictures and not placeholders:
        return
    hint: dict[str, Any] = {
        "url": url,
        "pictures": len(pictures),
        "placeholders": placeholders,
    }
    if pictures:
        hint["examples"] = pictures[:3]
        hint["image_cmd"] = f"webtrawl image {url} -o images"
        hint["ocr_cmd"] = f"webtrawl ocr {url} --region X,Y,W,H -o acts"
    print(toon_encode(hint))


def _emit_extract_provenance(url: str, text: str, report: dict[str, Any]) -> None:
    """Print one TOON provenance line: transport tier, per-tier timings, char count.

    The paginated text block follows unchanged; this line only adds which tier
    answered, what every other tier returned, and how long each one took. An empty
    report means the text came from cache, so no tier ran.
    """
    provenance: dict[str, Any] = {
        "url": url,
        "transport": report.get("accepted_tier") or "cache",
        "cache": "miss" if report else "hit",
        "total_chars": len(text),
    }
    if report.get("tiers"):
        provenance["tiers"] = report["tiers"]
    if report.get("elapsed_seconds") is not None:
        provenance["elapsed_seconds"] = report["elapsed_seconds"]
    print(toon_encode(provenance))


@app.command()
def extract(
    url: Annotated[str, typer.Argument(help="http(s) URL to read into markdown.")],
    offset: Annotated[int, typer.Option("--offset", "-o", min=0, help="Start character offset for scrolling/pagination.")] = 0,
    max_chars: Annotated[int, typer.Option("--max-chars", "-m", min=0, help="Chunk length in characters (0 = unlimited / entire document).")] = 4000,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass local disk cache and force a fresh web fetch.")] = False,
    budget: Annotated[int, typer.Option("--budget", min=1, help="Wall-clock budget in seconds for the whole extraction cascade. A tier that cannot finish in the remaining time is skipped, so this bounds the worst case.")] = 20,
    brute: Annotated[bool, typer.Option("--brute", help="Brute mode: skip fast tiers, go straight to the real patched-Chromium tier (slow, for Vercel/Cloudflare walls). No cache.")] = False,
    wayback: Annotated[bool, typer.Option("--wayback", help="Read the newest Internet Archive snapshot instead of the live page. Also runs automatically when the live cascade hits a bot, cookie, login or pay wall.")] = False,
    structured: Annotated[bool, typer.Option("--structured", "-s", help="Return published structured facts (JSON-LD/microdata/OpenGraph: offers, price, availability, identity, ratings) instead of prose.")] = False,
    raw_html: Annotated[bool, typer.Option("--raw-html", help="Dump the full fetched HTML, unprocessed, so you can see what the page really ships. Reuses the HTML cached by an earlier extract; --no-cache forces a fresh fetch.")] = False,
    raw_html_out: Annotated[Optional[str], typer.Option("--raw-html-out", metavar="PATH", help="Write the full fetched HTML to PATH instead of stdout.")] = None,
    payloads: Annotated[bool, typer.Option("--payloads", help="Print the decoded embedded SSR payloads (Next.js, Remix, Nuxt, SvelteKit, Apollo, TanStack) as JSON. Reuses the HTML cached by an earlier extract; --no-cache forces a fresh fetch.")] = False,
):
    """Extract page markdown through a tiered cascade (Jina Reader, TLS fetch, patched Chromium).

    The first tier whose text clears the completeness gate wins, so a usable Jina
    result never pays for the browser tiers. A tier that cannot start inside the
    remaining --budget is skipped, and the per-tier timings are printed as TOON
    provenance above the content. A document that carries substantial prose is kept
    even when a navigation header sits on top of it; the failure message reports
    what each tier returned and how long it took.

    Document URLs (.pdf, .docx, .xlsx, .csv, ...) are detected automatically and take
    the document path instead: the file is downloaded and converted to markdown by
    MarkItDown, then paginated and cached exactly like a page. Same command, no local
    tooling, no curl.

    Consent banners are dismissed inside their vendor container before extraction.
    A cookie or maintenance wall is rejected and the next tier runs; a paywall or
    login wall is returned as partial content with a visible banner, because its
    masked metadata is still evidence. When every live tier is blocked, the newest
    Internet Archive snapshot is tried automatically.

    Use --payloads or --raw-html to inspect a page that resists extraction.
    --brute forces the browser transport from the start (patched Chromium via
    patchright, challenge solver on, 60 s), for pages known to be bot-walled.
    --wayback reads the archive directly, skipping the live page entirely.
    """
    clean_url = url.strip()

    if images.is_image_url(clean_url):
        fail(
            f"{clean_url} is a picture, not a page: the prose extractor has no text to return",
            code=3,
            suggestion=f"webtrawl image {clean_url} -o images",
        )

    if documents.document_kind(clean_url) is not None:
        text = None if no_cache else get_cached_extract(clean_url)
        if text is None:
            text, doc_meta = _run_provider(documents.extract_document, clean_url)
            if text:
                set_cached_extract(clean_url, text)
            provenance = {
                "url": clean_url,
                "type": doc_meta.get("type"),
                "transport": doc_meta.get("transport"),
                "downloaded_bytes": doc_meta.get("downloaded_bytes"),
            }
            print(toon_encode(provenance))
        res = paginate_content(text, offset=offset, max_chars=max_chars, url=clean_url)
        print(format_extract_text(res))
        return

    if wayback:
        archived = _run_provider(providers.wayback_extract, clean_url, 30.0)
        if not archived:
            fail(
                f"no usable Internet Archive snapshot for {clean_url}",
                code=1,
                suggestion="try the live page, or pick another source from the search results",
            )
        print(toon_encode({"url": clean_url, "transport": "wayback-machine", "total_chars": len(archived)}))
        res = paginate_content(archived, offset=offset, max_chars=max_chars, url=clean_url)
        print(format_extract_text(res))
        return

    if brute:
        # Brute mode: no cache by design (bot walls are per-session), longer
        # timeout for challenge solving + full page JS. Same paginate tail.
        text = _run_provider(providers.brute_extract, clean_url, 60.0)
        res = paginate_content(text or "", offset=offset, max_chars=max_chars, url=clean_url)
        print(format_extract_text(res))
        return

    if raw_html or raw_html_out:
        html, tier = _fetch_html_cached(clean_url, no_cache=no_cache)
        if not html:
            fail(f"could not fetch any HTML from {clean_url} (transport: {tier})", code=1, suggestion="retry, or try another URL")
        if raw_html_out:
            Path(raw_html_out).write_text(html, encoding="utf-8")
            print_payload({"status": "success", "url": clean_url, "transport": tier, "html_bytes": len(html), "path": raw_html_out})
        else:
            sys.stdout.write(html)
        return

    if payloads:
        html, tier = _fetch_html_cached(clean_url, no_cache=no_cache)
        if not html:
            fail(f"could not fetch any HTML from {clean_url} (transport: {tier})", code=1, suggestion="retry, or try another URL")
        found = hydration.extract_embedded_payloads(html)
        if not found:
            fail(
                f"no embedded SSR payload decoded from {clean_url} (transport: {tier}, {len(html)} bytes). "
                "The page is not a server-rendered application shell.",
                code=1,
                suggestion=f"inspect the raw page with: webtrawl extract {clean_url} --raw-html",
            )
        print(
            toon_encode(
                {
                    "status": "success",
                    "url": clean_url,
                    "transport": tier,
                    "html_bytes": len(html),
                    "payloads": found,
                }
            )
        )
        return

    if structured:
        facts = _run_provider(providers.structured_extract, clean_url)
        print(format_structured(facts))
        return

    report: dict[str, Any] = {}
    text: str | None = None
    if not no_cache:
        text = get_cached_extract(clean_url)
        if text is None:
            cached_failure = get_cached_failure(clean_url)
            if cached_failure is not None:
                fail(cached_failure["error"], code=1, suggestion=cached_failure.get("suggestion"))

    if text is None:
        def _extract() -> str:
            """Run the cascade, caching a failed extraction before re-raising it."""
            try:
                return providers.jina_extract(clean_url, budget=budget, report=report)
            except providers.IncompleteExtractionError as exc:
                if not no_cache:
                    set_cached_extract(
                        clean_url,
                        "",
                        error=str(exc),
                        suggestion=exc.suggestion,
                        html=report.get("html"),
                    )
                raise

        text = _run_provider(_extract)
        if text and not providers.is_bot_blocked(text) and not text.startswith("⚠️ [EXTRACTION BLOCKED"):
            if not no_cache:
                set_cached_extract(clean_url, text, html=report.get("html"))

    if not text or providers.is_bot_blocked(text) or text.startswith("⚠️ [EXTRACTION BLOCKED"):
        facts = None
        seen_html = report.get("html") or get_cached_html(clean_url)
        if seen_html:
            try:
                from webtrawl import structured as structured_mod

                parsed = structured_mod.extract_facts(seen_html)
                if parsed and (parsed.get("offers") or parsed.get("identity")):
                    parsed["transport"] = report.get("accepted_tier") or "cached-html"
                    parsed["url"] = clean_url
                    parsed["html_bytes"] = len(seen_html)
                    facts = parsed
            except Exception:
                facts = None
        elif not report.get("blocked"):
            try:
                facts = providers.structured_extract(clean_url, timeout=min(15.0, float(budget)))
            except Exception:
                facts = None
        if facts and (facts.get("offers") or facts.get("identity")):
            notice = (
                f"# prose layer unusable for {clean_url} — returning published structured data instead\n"
                f"# transport: {facts.get('transport')} | jsonld: {', '.join(facts.get('jsonld_types') or []) or 'none'}\n\n"
            )
            print(notice + format_structured(facts))
            return

    _emit_extract_provenance(clean_url, text, report)
    _emit_picture_hint(clean_url, report.get("html") or get_cached_html(clean_url) or "", text or "")
    res = paginate_content(text or "", offset=offset, max_chars=max_chars, url=clean_url)
    print(format_extract_text(res))


@app.command()
def image(
    target: Annotated[list[str], typer.Argument(help="Picture URL(s), or page URL(s) whose picture you want. A page yields the scan it documents.")],
    out: Annotated[str, typer.Option("--out", "-o", help="Directory that receives the files.")] = "images",
    full: Annotated[bool, typer.Option("--full", help="Assemble the page at native resolution from its Zoomify tiles instead of the thumbnail the viewer caps at 1000 px. Costs one browser session for the grid plus one for the tiles; cached afterwards.")] = False,
    region: Annotated[Optional[str], typer.Option("--region", help="Native-pixel rectangle X,Y,W,H to extract. Implies --full and downloads only the tiles that cover it, which is how a readable act is cut out of a 3888 px page.")] = None,
    pages: Annotated[Optional[str], typer.Option("--pages", help="Batch: vary the trailing view number of the URL, for example 50-57 or 1,4,9,12. Fetched through one transport.")] = None,
    level: Annotated[Optional[int], typer.Option("--level", min=0, help="Tile level to assemble, 0 = overview. Omit for native. One level down halves the pixels and quarters the tile count, which is enough for OCR.")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass the on-disk picture cache and re-download.")] = False,
):
    """Download pictures, including the ones behind a bot wall, and cut a readable region out of a scan.

    A page usually publishes the picture it documents as OpenGraph. When the
    publisher runs a Zoomify viewer, that URL is a 1000 px-capped thumbnail and
    the native scan only exists as 256 px tiles: `--full` stitches the page,
    `--region` stitches only the rectangle you ask for, at native resolution.

    Transport is chosen for you: plain TLS first, then ONE patched-Chromium
    session for the whole batch. That matters because the CDN binds a picture to
    the browser session cookie (a bare curl answers 403) and because a burst of
    tile requests trips the challenge, so the in-page pool is bounded and
    retried.

    Files are cached under ~/.cache/webtrawl/images, so re-reading an
    investigation costs nothing. The row carries width/height: pass them to
    `image-editing-cli crop` when you only need one act of the page.
    """
    try:
        targets = list(target)
        if pages:
            if len(targets) != 1:
                fail("--pages expands exactly one URL", code=3, suggestion="pass a single page URL with --pages 50-57")
            targets = images.page_variants(targets[0], pages)
        pictures_region = images.parse_region(region)
        rows = images.run(
            targets,
            out_dir=Path(out),
            full=full or pictures_region is not None,
            region=pictures_region,
            level=level,
            no_cache=no_cache,
        )
    except providers.BadInputError as exc:
        fail(str(exc), code=3, suggestion="fix the arguments")
    except providers.IncompleteExtractionError as exc:
        fail(str(exc), code=1, suggestion=exc.suggestion)
    except providers.TransientError as exc:
        fail(str(exc), code=1, suggestion="retry in a few seconds; a true wall clears with the next browser session")

    ok = [r for r in rows if r.get("bytes")]
    if not ok:
        first = rows[0] if rows else {}
        fail(
            f"no picture downloaded (status {first.get('status', '?')}): {first.get('error', 'no answer')}",
            code=1,
            suggestion="retry, or read the page with: webtrawl extract <page> --raw-html",
        )
    print(toon_encode({"count": len(ok), "dir": str(Path(out)), "images": rows}))
    print("\n# 💡 [NEXT STEPS FOR AI AGENTS]")
    print("# Read the file with the `read` tool: it is an image, not a text extract.")
    print("# If the writing is too small, cut the exact act out at native resolution:")
    print("#   webtrawl image <page-url> --region X,Y,W,H -o crops")
    print("# The region is in native pixels (0,0 = top-left of the scan), so a row of")
    print("# acts is one `--region` per act, not a local crop of a 3888 px page.")


@app.command()
def ocr(
    target: Annotated[list[str], typer.Argument(help="Local picture, picture URL, page URL, or a mix. A page URL is downloaded first, so one command goes from page to text.")],
    out: Annotated[str, typer.Option("--out", "-o", help="Directory for the downloaded picture and its .md transcription.")] = "ocr",
    region: Annotated[Optional[str], typer.Option("--region", help="Native-pixel rectangle X,Y,W,H to cut out of a zoomable scan before reading it. This is how one act is isolated from a 12-fiche page.")] = None,
    provider: Annotated[str, typer.Option("--provider", "-p", help="auto | gemini | vlm | mistral | tesseract. auto = a configured vision model first (handwriting), then the local engine (printed text).")] = "auto",
    lang: Annotated[str, typer.Option("--lang", help="Tesseract language pack, for example fra or eng. Ignored by the vision providers.")] = "fra",
    deep: Annotated[bool, typer.Option("--deep", help="Use the larger model with its reasoning pass. Measured on a register crop: the same three lines, 6-11 s instead of 1-5 s. Enable it only for a page the fast pass leaves half-read.")] = False,
    level: Annotated[Optional[int], typer.Option("--level", min=0, help="Tile level of the crop when the target is a zoomable scan. One level down is legible for OCR and fetches 4x fewer tiles.")] = None,
    pages: Annotated[Optional[str], typer.Option("--pages", help="Batch: vary the trailing view number of the page URL, for example 50-57. Downloaded and read concurrently.")] = None,
    concurrency: Annotated[int, typer.Option("--concurrency", "-c", min=1, max=16, help="How many pictures to read at once. The model's latency dominates, so this is the only lever that makes a volume of pages fast.")] = 4,
    timeout: Annotated[Optional[float], typer.Option("--timeout", min=1, help="Overall budget in seconds for one picture. When it expires, the providers still running are cancelled and the local engine answers with a loud warning.")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Re-download and re-read instead of using the picture and transcription caches.")] = False,
):
    """Read the text of a picture: local engine for print, vision model for handwriting.

    Measured on an 18th-century parish register: `tesseract` needs 0.6 s and
    returns two meaningless lines, a vision model needs 7 s and returns the act.
    So `auto` prefers a configured vision model and falls back to the local
    engine; an explicitly requested provider that cannot run is an ERROR, never a
    silent downgrade, because a caller who asked for Gemini must not receive
    tesseract's guess.

    The transcription is written next to the picture (`<name>.md`) and printed.
    Model output is never corrected: an unreadable word stays `[illisible]`.
    """
    if provider not in ("auto", "gemini", "vlm", "mistral", "tesseract"):
        fail(f"unknown provider {provider!r}", code=3, suggestion="use auto | gemini | vlm | mistral | tesseract")

    out_dir = Path(out)
    expanded: list[str] = []
    for item in target:
        if pages:
            try:
                expanded.extend(images.page_variants(item, pages))
            except providers.BadInputError as exc:
                fail(str(exc), code=3, suggestion="pass a page URL ending in its view number")
        else:
            expanded.append(item)

    async def prepare(item: str) -> tuple[Path, str]:
        """Download one target and return the picture to read."""
        local = Path(item)
        if local.is_file():
            return local, "file"
        region_box = images.parse_region(region)
        downloaded = await images.run_async(
            [item],
            out_dir=out_dir,
            full=region_box is not None,
            region=region_box,
            level=level,
            no_cache=no_cache,
            sem=transport_gate,
        )
        if not downloaded or not downloaded[0].get("path"):
            raise providers.TransientError(f"could not download a picture from {item}")
        return Path(downloaded[0]["path"]), "page" if downloaded[0].get("source") == "page" else "url"

    async def flow() -> list[tuple[Path, str, ocr_engine.OcrResult | None, str]]:
        """Download and read every target, overlapping the two phases.

        One semaphore bounds the connections for the whole batch: a page and its
        tiles come over a single keep-alive session, so ten pages do not open ten
        times the sockets one page may open. A picture is read as soon as it
        lands, so the model's latency hides behind the next download instead of
        queueing after it.
        """
        reads = asyncio.Semaphore(max(1, concurrency))

        async def one(item: str) -> tuple[Path, str, ocr_engine.OcrResult | None, str]:
            path, source = await prepare(item)
            async with reads:
                try:
                    result = await ocr_engine.ocr_file_async(
                        path, provider=provider, lang=lang, no_cache=no_cache, fast=not deep, timeout=timeout
                    )
                except ocr_engine.OcrError as exc:
                    return path, source, None, str(exc)
            return path, source, result, ocr_engine.clean_text(result.text)

        return list(await asyncio.gather(*(one(item) for item in expanded)))

    transport_gate = asyncio.Semaphore(images.MAX_INFLIGHT)
    try:
        outcome = asyncio.run(flow())
    except providers.BadInputError as exc:
        fail(str(exc), code=3, suggestion="fix the arguments")
    except providers.TransientError as exc:
        fail(str(exc), code=1, suggestion="retry, or check the page with: webtrawl extract <page> --raw-html")

    rows: list[dict[str, Any]] = []
    for path, source, result, payload in outcome:
        if result is None:
            fail(f"{path.name}: {payload}", code=1, suggestion="webtrawl providers shows which OCR provider is ready")
        text = payload
        text_path = path.with_suffix(".md")
        text_path.write_text(text + "\n", encoding="utf-8")
        row = {
            "name": path.name,
            "source": source,
            "picture": str(path),
            "text": str(text_path),
            "provider": result.provider,
            "model": result.model,
            "seconds": result.seconds,
            "chars": len(text),
            "cached": "hit" if result.cached else "miss",
        }
        if result.extra.get("note"):
            row["note"] = result.extra["note"]
        rows.append(row)
        print(toon_encode(row))
        lines_ = (result.extra or {}).get("lines") or []
        if lines_:
            print(render_data_as_toon(["n", "confidence", "text"], lines_))
        entities = (result.extra or {}).get("entities") or {}
        if any(entities.values()):
            print("# entities")
            for kind, values in entities.items():
                if values:
                    print(f"#   {kind}: {values}")
        print(f"\n----- {path.name} -----")
        print(text or "(no text found)")
        print()

    if not rows:
        fail("nothing to read", code=3, suggestion="pass at least one picture, URL or page")
    print("# 💡 [NEXT STEPS FOR AI AGENTS]")
    print("# The transcription is verbatim model output, not a corrected edition:")
    print("# [illisible] marks a word the model could not read, and stays in the record.")
    print("# Cross-check any date or surname against the picture before citing it.")


@app.command("providers")
def providers_list():
    """List search methods and infrastructure providers with key status."""
    bkey, bsource = resolve_brave_key()
    tkey, tsource = resolve_tavily_key()
    pkey, psource = resolve_parallel_key()
    fkey, fsource = resolve_tinyfish_key()
    rows = [
        {"provider": "brave", "kind": "method", "modes": "web,news", "configured": f"yes ({bsource})" if bkey else "no", "note": "independent index; free tier now needs a card for NEW keys"},
        {"provider": "tavily", "kind": "method", "modes": "web,news", "configured": f"yes ({tsource})" if tkey else "keyless ok", "note": "1k credits/mo free no card; keyless mode works with no account; $0.008/credit after"},
        {"provider": "parallel", "kind": "method", "modes": "web", "configured": f"yes ({psource})" if pkey else "no", "note": "~5k req/mo free credits; get key at platform.parallel.ai; $1/1k fast mode"},
        {"provider": "tinyfish", "kind": "method", "modes": "web", "configured": f"yes ({fsource})" if fkey else "no", "note": "free at any wallet balance incl $0, no card, but a key is required (agent.tinyfish.ai/api-keys); 30 req/min"},
        {"provider": "gnews", "kind": "infra", "modes": "news-fallback", "configured": "always", "note": "Google News RSS, keyless last resort"},
        {"provider": "jina", "kind": "infra", "modes": "extract", "configured": "always", "note": "r.jina.ai reader, keyless ~20 RPM"},
        {"provider": "wayback", "kind": "infra", "modes": "extract-fallback", "configured": "always", "note": "Internet Archive CDX + raw snapshot; automatic when the live cascade hits a wall"},
    ]
    for entry in ocr_engine.available_providers():
        rows.append(
            {
                "provider": entry["provider"],
                "kind": "ocr",
                "modes": "image",
                "configured": entry["ready"] if entry["source"] != "none" else "no",
                "note": entry["note"] + (" | key: " + entry["source"] if entry["source"] in ("env", "tool-config") else ""),
            }
        )
    from _vendor.cli_common.toon_output import render_data_as_toon
    print(render_data_as_toon(["provider", "kind", "modes", "configured", "note"], rows))


@app.command("agent-info")
def agent_info():
    """Self-describing manifest for AI agents (commands, params, exit codes)."""
    manifest = {
        "tool": "webtrawl",
        "version": __version__,
        "output": "TOON only (snippets included; --compact omits them). No --json flag and no JSON on stdout: JSON is transport for machine input, never an output.",
        "documents": "Document URLs (.pdf/.docx/.xlsx/.csv/...) are handled by `extract` itself: it downloads the file and converts it to markdown with MarkItDown. `web` and `news` tag every row with its `type` (web|pdf|docx|...) and put the ready command in `extract_cmd`. Never fetch a document URL with curl.",
        "commands": [
            {"name": "web", "args": "QUERY [-m brave|tavily|parallel|tinyfish|auto] [-n 1-20] [-f day|week|month|year] [--country CC] [--lang xx] [--offset N] [--compact] [--full]", "needs_key": "optional (auto picks what is configured; tavily works keyless)"},
            {"name": "news", "args": "QUERY [-m brave|tavily|auto] [-n] [-f] [--country] [--lang] [--compact] [--full]", "needs_key": "optional"},
            {"name": "extract", "args": "URL [--offset N] [--max-chars N] [--no-cache] [--budget SECONDS] [--brute] [--wayback] [--structured|-s]", "needs_key": "none", "note": "URL is a page OR a document. Cookie banners are dismissed, walls are reported by type, and the Internet Archive is tried automatically when the live cascade is blocked. A picture URL is refused here: use `image`."},
            {"name": "image", "args": "URL... [-o DIR] [--full] [--region X,Y,W,H] [--no-cache]", "needs_key": "none", "note": "Downloads pictures, including CDN URLs that answer 403 to curl (one browser session for the whole batch, cached on disk). --full stitches a Zoomify page at native resolution; --region stitches only that rectangle, which is how a readable act is extracted from a 3888 px scan."},
            {"name": "ocr", "args": "TARGET... [-o DIR] [--region X,Y,W,H] [-p auto|gemini|vlm|mistral|tesseract] [--lang fra] [--deep]", "needs_key": "optional (tesseract is local and free; vlm uses a free OpenRouter model)", "note": "Reads a picture. Accepts a local file, a picture URL or a page URL (downloaded first). auto prefers a configured vision model, because tesseract returned two meaningless lines on an 18th-century register where the vision model returned the act."},
            {"name": "providers", "args": "", "needs_key": "none"},
            {"name": "config check|show|set", "args": "[provider KEY]", "needs_key": "none"},
            {"name": "agent-info", "args": "", "needs_key": "none"},
        ],
        "exit_codes": {"0": "ok", "1": "transient (retry)", "2": "auth missing", "3": "bad input", "4": "rate limited"},
    }
    from toon import encode as toon_encode
    print(toon_encode(manifest))


@config_app.command("check")
def config_check():
    """Which providers have a resolvable key, and from where."""
    key, source = resolve_brave_key()
    tkey, tsource = resolve_tavily_key()
    pkey, psource = resolve_parallel_key()
    fkey, fsource = resolve_tinyfish_key()
    from _vendor.cli_common.toon_output import render_data_as_toon
    rows = [
        {"provider": "brave", "configured": "yes" if key else "no", "source": source},
        {"provider": "tavily", "configured": "yes" if tkey else "keyless ok", "source": tsource},
        {"provider": "parallel", "configured": "yes" if pkey else "no", "source": psource},
        {"provider": "tinyfish", "configured": "yes" if fkey else "no", "source": fsource},
        {"provider": "jina", "configured": "yes", "source": "keyless"},
        {"provider": "gnews", "configured": "yes", "source": "keyless"},
    ]
    print(render_data_as_toon(["provider", "configured", "source"], rows))


@config_app.command("show")
def config_show():
    """Show config file path and masked keys."""
    cfg = load_tool_config()
    keys = cfg.get("keys") or {}
    masked = {k: (v[:4] + "..." + v[-2:] if len(v) > 8 else "***") for k, v in keys.items()}
    print_payload({"config_path": str(TOOL_CONFIG_PATH), "keys": masked})


@config_app.command("set")
def config_set(
    provider: Annotated[
        str,
        typer.Argument(help="Provider name: brave | tavily | parallel | tinyfish | gemini | vlm | mistral."),
    ],
    value: Annotated[str, typer.Argument(help="API key ('-' to read from stdin).")],
):
    """Persist an API key (file created with 0600 permissions)."""
    val = value
    if val == "-":
        import sys
        val = sys.stdin.readline().strip()
    if not val:
        fail("empty key", code=3)
    path = set_key(provider.lower(), val)
    print_payload({"status": "success", "provider": provider, "config_path": str(path)})


if __name__ == "__main__":
    app()
