"""Search providers: Brave API (keyed) + Google News RSS and Jina Reader (keyless).

Exit-code semantics (semantic, stable, documented in docs/exit-codes.md):
0 ok | 1 transient error | 2 auth missing | 3 bad input | 4 rate limited.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from webtrawl import __version__

_HTML_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_RE.sub(" ", text or "").replace("  ", " ").strip()


BRAVE_WEB_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"
GNEWS_RSS_URL = "https://news.google.com/rss/search"
JINA_READER_URL = "https://r.jina.ai"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
PARALLEL_SEARCH_URL = "https://api.parallel.ai/v1/search"
TINYFISH_SEARCH_URL = "https://api.search.tinyfish.ai"

# --freshness day|week|month|year -> tinyfish recency_minutes (API caps at 5256000 = 10y)
TINYFISH_FRESHNESS_MINUTES = {"day": 1440, "week": 10080, "month": 43200, "year": 525600}

# --freshness day|week|month|year -> parallel advanced_settings.after_date (ISO date cutoff)
PARALLEL_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

FRESHNESS_MAP = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class ProviderError(Exception):
    exit_code = 1


class AuthMissingError(ProviderError):
    exit_code = 2


class BadInputError(ProviderError):
    exit_code = 3


class RateLimitError(ProviderError):
    exit_code = 4


class TransientError(ProviderError):
    exit_code = 1


class IncompleteExtractionError(TransientError):
    """No tier produced a usable document.

    Carries the per-tier evidence: what each tier returned, in how long, and why
    the completeness gate rejected it. The message therefore reports the real
    cause instead of blaming the page for the gate's own verdict. ``budget_exhausted``
    separates a wall-clock stop, where a higher --budget is the fix, from a page
    whose tiers all returned unusable text.
    """

    def __init__(
        self,
        url: str,
        tiers: list[dict[str, Any]],
        *,
        budget_seconds: float,
        elapsed_seconds: float,
        budget_exhausted: bool,
    ) -> None:
        self.url = url
        self.tiers = tiers
        self.budget_seconds = budget_seconds
        self.elapsed_seconds = elapsed_seconds
        self.budget_exhausted = budget_exhausted
        super().__init__(self._build_message())

    @staticmethod
    def _describe(tier: dict[str, Any]) -> str:
        """Render one tier's contribution as a single report line."""
        if tier["status"] == "skipped":
            return f"{tier['tier']}: skipped ({tier.get('reason', 'no budget left')})"
        line = f"{tier['tier']}: {tier['chars']} chars in {tier['seconds']}s"
        if tier.get("raw_bytes"):
            line += f" from {tier['raw_bytes']} raw bytes"
        if tier.get("reason"):
            line += f" -> {tier['status']}: {tier['reason']}"
        return line

    def _build_message(self) -> str:
        report = "; ".join(self._describe(tier) for tier in self.tiers) or "no tier ran"
        produced = any(tier["chars"] > 0 for tier in self.tiers)
        if self.budget_exhausted:
            head = (
                f"extraction of {self.url} exhausted its {self.budget_seconds:.0f}s budget "
                f"after {self.elapsed_seconds:.1f}s"
            )
        elif produced:
            head = f"every tier returned text that failed the completeness gate for {self.url}"
        else:
            head = f"no tier returned any usable text for {self.url}"
        return f"{head} (tiers: {report})."

    @property
    def suggestion(self) -> str:
        """The real fix for this failure, keyed on its actual cause."""
        if self.budget_exhausted:
            return "raise --budget (e.g. --budget 60) and retry: the cascade ran out of time, not content"
        walls = [tier.get("reason") for tier in self.tiers if tier.get("status") in ("rejected", "partial")]
        if any(wall in ("cookie wall", "paywall", "login wall") for wall in walls):
            return f"the page is walled; read the archived copy with: webtrawl extract {self.url} --wayback"
        if any(tier["chars"] > 0 for tier in self.tiers):
            return f"tiers returned text the gate rejected; inspect the page with: webtrawl extract {self.url} --raw-html"
        return f"no tier produced text; the page is likely a client-rendered shell: webtrawl extract {self.url} --payloads"


def map_freshness(freshness: str) -> str:
    """day|week|month|year -> Brave freshness codes pd|pw|pm|py."""
    f = FRESHNESS_MAP.get(freshness)
    if f is None:
        raise BadInputError(f"invalid freshness {freshness!r}; use day|week|month|year")
    return f


def clamp_count(count: int, lo: int = 1, hi: int = 20) -> int:
    return max(lo, min(hi, count))


def _brave_get(url: str, api_key: str, params: dict[str, Any], timeout: float = 15.0) -> dict:
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
        "User-Agent": USER_AGENT,
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    except requests.RequestException as exc:  # network/DNS/timeout
        raise TransientError(f"network error: {exc}") from exc
    if resp.status_code == 429:
        raise RateLimitError("brave rate limited (HTTP 429): switch method, e.g. -m tavily (keyless ok) or -m parallel")
    if resp.status_code in (401, 403):
        raise AuthMissingError(f"brave rejected the key (HTTP {resp.status_code})")
    if resp.status_code >= 500:
        raise TransientError(f"brave server error (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise TransientError(f"brave unexpected HTTP {resp.status_code}")
    return resp.json()


def parse_brave_web(payload: dict) -> list[dict]:
    """Flatten a Brave web response into normalized result rows.

    Brave shape: {"type":"search","web":{"results":[{title,url,description,age,...}]}}
    """
    rows: list[dict] = []
    for item in (payload.get("web") or {}).get("results") or []:
        meta = item.get("meta_url") or {}
        rows.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": _strip_html(item.get("description")),
                "date": item.get("age") or "",
                "source": meta.get("hostname") or meta.get("site_name") or "",
            }
        )
    return rows


def parse_brave_news(payload: dict) -> list[dict]:
    """Flatten a Brave NEWS response.

    NOTE: unlike the web vertical, the news endpoint returns results at TOP LEVEL:
    {"type":"news","query":{...},"results":[{type:"news_result",...}]}
    (This is the exact parsing bug found in paperfoot/search-cli v0.9.0.)
    """
    items = payload.get("results")
    if items is None:  # defensive: also accept wrapped forms
        items = (payload.get("news") or {}).get("results") or []
    rows: list[dict] = []
    for item in items or []:
        rows.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": _strip_html(item.get("description")),
                "date": item.get("age") or "",
                "source": (item.get("meta_url") or {}).get("hostname") or "",
            }
        )
    return rows


def brave_web(api_key: str, query: str, *, count: int = 10, freshness: str | None = None,
              country: str | None = None, lang: str | None = None, offset: int = 0) -> tuple[list[dict], dict]:
    params: dict[str, Any] = {"q": query, "count": clamp_count(count)}
    if offset:
        params["offset"] = offset
    if freshness:
        params["freshness"] = map_freshness(freshness)
    if country:
        params["country"] = country.upper()
    if lang:
        params["search_lang"] = lang.lower()
    payload = _brave_get(BRAVE_WEB_URL, api_key, params)
    return parse_brave_web(payload), {"provider": "brave", "vertical": "web"}


def brave_news(api_key: str, query: str, *, count: int = 10, freshness: str | None = None,
               country: str | None = None, lang: str | None = None) -> tuple[list[dict], dict]:
    params: dict[str, Any] = {"q": query, "count": clamp_count(count)}
    if freshness:
        params["freshness"] = map_freshness(freshness)
    if country:
        params["country"] = country.upper()
    if lang:
        params["search_lang"] = lang.lower()
    payload = _brave_get(BRAVE_NEWS_URL, api_key, params)
    return parse_brave_news(payload), {"provider": "brave", "vertical": "news"}


_TAG_RE = re.compile(r"<[^>]+>")


def parse_gnews_rss(xml_text: str) -> list[dict]:
    """Parse Google News RSS into normalized rows (keyless news fallback)."""
    root = ET.fromstring(xml_text)
    rows: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        src = item.findtext("source") or ""
        desc = _TAG_RE.sub(" ", item.findtext("description") or "").strip()
        # Google News titles look like "Headline - Publisher"; split off the tail.
        if len(title) > 3 and " - " in title[-60:]:
            head, _, tail = title.rpartition(" - ")
            if head and tail:
                title, src = head.strip(), (item.findtext("source") or tail).strip()
        rows.append({"title": title, "url": link, "snippet": desc[:200], "date": pub, "source": src})
    return rows


def gnews_news(query: str, *, count: int = 10, lang: str | None = None, country: str | None = None) -> tuple[list[dict], dict]:
    hl = (lang or "en").lower()
    gl = (country or "US").upper()
    params = {"q": query, "hl": f"{hl}-{gl}", "gl": gl, "ceid": f"{gl}:{hl}"}
    try:
        resp = requests.get(GNEWS_RSS_URL, params=params, timeout=10.0, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        raise TransientError(f"network error: {exc}") from exc
    if resp.status_code == 429:
        raise RateLimitError("google news rate limited (HTTP 429)")
    if resp.status_code != 200:
        raise TransientError(f"google news unexpected HTTP {resp.status_code}")
    rows = parse_gnews_rss(resp.text)[: clamp_count(count)]
    return rows, {"provider": "gnews", "vertical": "news"}


BOT_BLOCK_PATTERNS = [
    re.compile(r"are you a robot", re.I),
    re.compile(r"please confirm you are a human", re.I),
    re.compile(r"just a moment\.\.\.", re.I),
    re.compile(r"verify you are human", re.I),
    re.compile(r"enable javascript and cookies to continue", re.I),
    re.compile(r"challenge-platform", re.I),
    re.compile(r"v[ée]rifie que vous n.[êe]tes pas un bot", re.I),
    re.compile(r"se prot[ée]ger contre les bots malveillants", re.I),
    # Vercel Bot Manager serves a 200 shell titled "Vercel Security Checkpoint"
    # with a short reference id (e.g. "iad1::1234-AbC"). Without this pattern the
    # checkpoint text was returned as if it were clean content (242 chars).
    re.compile(r"vercel security checkpoint", re.I),
    re.compile(r"security checkpoint\s*\|", re.I),
]


def is_bot_blocked(text: str) -> bool:
    if not text:
        return True
    lower = text.lower()
    has_bot_keyword = any(
        k in lower
        for k in [
            "robot",
            "bot",
            "captcha",
            "just a moment",
            "challenge",
            "turnstile",
            # Vercel marker: without it, "Vercel Security Checkpoint" pages
            # slipped through detection because they contain none of the
            # Cloudflare keywords above.
            "vercel",
            "security checkpoint",
        ]
    )
    if not has_bot_keyword:
        return False
    return any(p.search(text) for p in BOT_BLOCK_PATTERNS)


def detect_wall(text: str) -> str | None:
    """Return the wall type that dominates ``text``, or None for real content.

    A consent banner, a paywall, or a maintenance notice can carry enough prose to
    clear the completeness gate; the cascade then returns the wall as if it were
    the page. Two distinct signature phrases, or one phrase heading a short
    document, identify the wall. A single phrase inside a long article is a
    mention, not a wall.

    Args:
        text: Extracted markdown from any tier.

    Returns:
        One of ``cookie wall``, ``paywall``, ``login wall``, ``maintenance page``,
        or None when the text is document content.
    """
    if not text:
        return None
    clean = text.strip()
    if len(clean) > WALL_MAX_CHARS:
        return None
    head = clean[:WALL_SINGLE_SIGNATURE_HEAD_CHARS]
    # A page can carry both a consent banner and a paywall. The paywall is the
    # useful classification: it says real metadata sits behind the mask, whereas
    # "cookie wall" only says retry with a cleaner session.
    for kind in ("paywall", "login wall", "cookie wall", "maintenance page"):
        patterns = WALL_SIGNATURES[kind]
        hits = sum(1 for pattern in patterns if pattern.search(clean))
        if hits >= 2:
            return kind
        if (
            hits == 1
            and len(clean) <= WALL_SINGLE_SIGNATURE_MAX_CHARS
            and any(pattern.search(head) for pattern in patterns)
        ):
            return kind
    return None


# A consent banner, a paywall, or a maintenance notice is not document content
# even when it is long enough to clear the completeness gate. Phrases are grouped
# by wall type: two distinct phrases mean the page *is* the wall, while a single
# phrase inside a long article is only a mention and must not trigger rejection.
WALL_SIGNATURES: dict[str, tuple[re.Pattern[str], ...]] = {
    "cookie wall": (
        re.compile(r"cookies? management panel", re.I),
        re.compile(r"preference for all services", re.I),
        re.compile(r"this site uses cookies", re.I),
        re.compile(r"apis? are used to load scripts", re.I),
        re.compile(r"service can install \d+ cookies?", re.I),
        re.compile(r"cookie requiring your consent", re.I),
        re.compile(r"g[ée]rer (?:mes|les) (?:cookies|choix)", re.I),
        re.compile(r"nous utilisons des cookies", re.I),
        re.compile(r"en poursuivant votre navigation", re.I),
        re.compile(r"tout accepter", re.I),
        re.compile(r"continuer sans accepter", re.I),
    ),
    "paywall": (
        re.compile(r"reserved for premium members", re.I),
        re.compile(r"r[ée]serv[ée]e?s? aux (?:membres )?premium", re.I),
        re.compile(r"become (?:a )?premium", re.I),
        re.compile(r"already premium", re.I),
        re.compile(r"abonnez-vous", re.I),
        re.compile(r"r[ée]serv[ée] aux abonn[ée]s", re.I),
        re.compile(r"subscribe to (?:continue|read)", re.I),
        re.compile(r"this (?:article|content|page) is (?:for|reserved)", re.I),
    ),
    "login wall": (
        re.compile(r"sign in to (?:continue|view|read)", re.I),
        re.compile(r"log ?in to (?:continue|view|read)", re.I),
        re.compile(r"connectez-vous pour (?:continuer|voir|lire|acc[ée]der)", re.I),
    ),
    "maintenance page": (
        re.compile(r"site en maintenance", re.I),
        re.compile(r"maintenance en cours", re.I),
        re.compile(r"temporarily unavailable (?:for|due to) maintenance", re.I),
        re.compile(r"503 service (?:temporarily )?unavailable", re.I),
        re.compile(r"502 bad gateway", re.I),
    ),
}

# A wall dominates a short document. Past this size the same phrases are more
# likely to be quoted by real content than to be the page itself. Consent panels
# grow with the number of services they list: FranceArchives ships a 6,074-char
# tarteaucitron panel, just above the first 6,000-char ceiling.
WALL_MAX_CHARS = 12000
# A single signature only counts when it heads a genuinely short document.
WALL_SINGLE_SIGNATURE_MAX_CHARS = 2500
WALL_SINGLE_SIGNATURE_HEAD_CHARS = 300

NAV_DROP_PATTERNS = [
    re.compile(r"^\s*\[Jump to content\].*$", re.I),
    re.compile(r"^\s*move to sidebar (hide|show).*$", re.I),
    re.compile(r"^\s*Toggle the table of contents.*$", re.I),
    re.compile(r"^\s*-\s*\[[ x]\]\s*(Main menu|Appearance|Personal tools|Tools|Navigation)\s*$", re.I),
]

JINA_REMOVE_SELECTORS = (
    "header, nav, footer, aside, .sidebar, #sidebar, .nav, .menu, "
    ".mw-jump-link, .vector-menu, #mw-navigation, #toc, .table-of-contents, "
    ".navigation, .advertisement, .ads"
)


def sanitize_markdown(text: str) -> str:
    """Strip JavaScript blobs, tracking scripts, unrendered templates, and residual navigation boilerplate."""
    if not text:
        return ""
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        line_str = line.strip()
        # Drop unrendered Angular/Mustache/Vue template expressions ({{ ... }}) and bare list dashes left behind
        if "{{" in line_str and "}}" in line_str:
            continue
        if line_str in ("-", "*", "- -", "* *"):
            continue
        # Drop minified JS, Adobe reactor promise scripts, and tracking blocks
        if "promise:n.resolve" in line_str or "core/src/lib/actions" in line_str or "var n=a(" in line_str:
            continue
        if "_satellite" in line_str or "adobe" in line_str and "reactor" in line_str:
            continue
        if len(line_str) > 300 and ("function(" in line_str or "webpack" in line_str or "window." in line_str):
            continue
        if any(p.match(line_str) for p in NAV_DROP_PATTERNS):
            continue
        cleaned_lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()


CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "enable javascript and cookies",
    "vérification de sécurité",
    "vérifiez que vous êtes humain",
    "vérifie que vous n’êtes pas un bot",
    "vérifie que vous n'êtes pas un bot",
    "se protéger contre les bots malveillants",
    "accès refusé",
)

# A prose extraction below this many characters is never a document.
MIN_TEXT_LENGTH = 100
# Extracted text must reach this share of the HTML actually fetched, otherwise the
# payload is a client-rendered shell whose data is not in the text layer.
EXTRACTION_COMPLETENESS_FLOOR = 0.02
# A block of short label lines is navigation chrome, not article prose.
NAV_CHROME_MIN_LINES = 8
NAV_CHROME_MAX_PROSE_SHARE = 0.5
# A line counts as prose when it is long in characters OR made of several words.
# The character test is what makes the gate language-aware: a Chinese sentence of
# 20 characters is a single whitespace token, so a word count alone classifies a
# real forum post as navigation chrome and discards the whole document.
PROSE_LINE_MIN_CHARS = 30
PROSE_LINE_MIN_WORDS = 6
# Wall-clock budget for the whole extraction cascade. A tier that cannot finish
# inside the remaining time is skipped instead of starting a fetch that overruns it.
MIN_TIER_SECONDS = 1.5
DEFAULT_EXTRACT_BUDGET = 20.0
# Scrapling hard-codes its Cloudflare solve-attempt cap in a private module global
# and exposes no parameter. Normal mode allows one attempt (~10 s, not ~40 s);
# brute mode never sets it and keeps the library default.
CLOUDFLARE_SOLVE_ATTEMPTS_NORMAL = 1


def _is_prose_line(line: str) -> bool:
    """Report whether one line reads as prose rather than a short navigation label."""
    return len(line) >= PROSE_LINE_MIN_CHARS or len(line.split()) >= PROSE_LINE_MIN_WORDS


def strip_nav_chrome(text: str) -> str:
    """Drop leading and trailing runs of navigation labels, keeping the article body.

    Navigation headers and footers are runs of non-prose lines at the document
    edges. Judging the stripped remainder lets a document that carries substantial
    prose pass the completeness gate instead of being discarded for its chrome.
    """
    lines = text.splitlines()
    start = 0
    while start < len(lines) and not _is_prose_line(lines[start].strip()):
        start += 1
    end = len(lines)
    while end > start and not _is_prose_line(lines[end - 1].strip()):
        end -= 1
    return "\n".join(lines[start:end]).strip()


def _looks_like_nav_chrome(text: str) -> bool:
    """Report whether text is a list of short labels rather than prose."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < NAV_CHROME_MIN_LINES:
        return False
    prose_chars = sum(len(line) for line in lines if _is_prose_line(line))
    return prose_chars < NAV_CHROME_MAX_PROSE_SHARE * len(text)


def extraction_reject_reason(
    text: str, raw_html_bytes: int, floor: float = EXTRACTION_COMPLETENESS_FLOOR
) -> str | None:
    """Return why ``text`` fails the completeness gate, or None when it passes.

    Args:
        text: The extracted text, as produced by any tier.
        raw_html_bytes: Byte size of the HTML the text came from; 0 disables the
            size comparison only.
        floor: Minimum share of ``raw_html_bytes`` the text must reach.

    Returns:
        A short reason when the extraction is unusable, else None.
    """
    clean = sanitize_markdown(text or "")
    if len(clean) <= MIN_TEXT_LENGTH:
        return f"too short ({len(clean)} chars <= {MIN_TEXT_LENGTH})"
    if is_bot_blocked(clean):
        return "bot challenge page"
    wall = detect_wall(clean)
    if wall in ("cookie wall", "maintenance page"):
        return wall
    if raw_html_bytes > 0 and len(clean) < floor * raw_html_bytes:
        return f"below density floor ({len(clean)} < {floor:.0%} of {raw_html_bytes} raw bytes)"
    body = strip_nav_chrome(clean)
    if len(body) <= MIN_TEXT_LENGTH:
        return "navigation chrome only"
    if _looks_like_nav_chrome(body):
        return "mostly navigation chrome"
    return None


def extraction_is_complete(text: str, raw_html_bytes: int, floor: float = EXTRACTION_COMPLETENESS_FLOOR) -> bool:
    """Report whether an extraction plausibly carries the page's content.

    The text must be a real document, not a bot wall, not a vanishingly small
    sample of the fetched HTML, and not a block of navigation labels. Navigation
    is stripped from the document edges before the prose test, so a page is never
    rejected for a navigation header sitting on top of a real article.
    """
    return extraction_reject_reason(text, raw_html_bytes, floor) is None


def html_is_challenge(html: str) -> bool:
    """Detect an anti-bot interstitial or an empty shell, whatever its wording."""
    if not html or len(html) < 1200:
        return True
    head = html[:6000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


# Consent managers used by the sites that most often wall agents. The page action
# only touches nodes inside these containers. Clicking the first visible button by
# text is unsafe: on a Geneanet tree the accept regex matched "ok" inside "Family
# Book" and navigated away from the person. Vendor-scoped selectors are the fix.
CONSENT_DISMISS_JS = """
(() => {
  const containers = ['#tarteaucitronRoot', '#didomi-host', '#onetrust-consent-sdk',
    '#axeptio_overlay', '.cky-consent-container', '#cookiebot', '#CybotCookiebotDialog'];
  const buttons = ['#tarteaucitronAllDenied2', '#tarteaucitronAllDenied',
    '#tarteaucitronCloseAlert', '#didomi-notice-disagree-button',
    '#onetrust-reject-all-handler', '.cky-btn-reject',
    '#tarteaucitronPersonalize2', '.tarteaucitronDeny', '.tarteaucitronAllow',
    '#didomi-notice-agree-button', '#onetrust-accept-btn-handler', '.cky-btn-accept'];
  for (const container of containers) {
    const root = document.querySelector(container);
    if (!root) continue;
    let clicked = false;
    for (const selector of buttons) {
      const button = root.querySelector(selector);
      if (button) { button.click(); clicked = true; break; }
    }
    // The consent widget stays in the DOM after its own handler runs (Geneanet
    // keeps the panel mounted), and trafilatura then reads the panel as the page.
    // Remove the container after the handler has had a tick to run.
    setTimeout(() => { try { root.remove(); } catch (e) {} }, 50);
    return clicked ? 'clicked+removed' : 'removed';
  }
  return null;
})()
"""

# Optional persistent Chromium profile: a Cloudflare clearance earned by one
# extraction is reused by the next. Off by default because two concurrent runs
# cannot share a Chromium profile; enable it with the env var when serialising
# work against a single challenged host.
BROWSER_PROFILE_ENV = "WEBTRAWL_BROWSER_PROFILE"

WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_RAW_URL = "https://web.archive.org/web/{timestamp}id_/{url}"
WAYBACK_TIMEOUT_SECONDS = 15.0
WAYBACK_MAX_SNAPSHOTS = 3


def dismiss_cookie_banner(page: Any) -> None:
    """Close a consent banner inside its known container, never a page button."""
    try:
        page.evaluate(CONSENT_DISMISS_JS)
        # The removal is scheduled 50 ms after the click so the vendor handler
        # runs first; wait past it before trafilatura reads the DOM.
        page.wait_for_timeout(400)
    except Exception:
        pass


def _browser_kwargs() -> dict[str, Any]:
    """Return the optional persistent-profile argument for the browser tier."""
    profile = os.environ.get(BROWSER_PROFILE_ENV, "").strip()
    return {"user_data_dir": profile} if profile else {}


def _wayback_snapshot_urls(url: str, limit: int = WAYBACK_MAX_SNAPSHOTS) -> list[str]:
    """Return the newest Wayback snapshot URLs for ``url``, newest first.

    The CDX API returns snapshots oldest first; a negative ``limit`` selects the
    last N instead of the first N, which is what the fallback needs.
    """
    params: list[tuple[str, str]] = [
        ("url", url),
        ("output", "json"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"),
        ("collapse", "digest"),
        ("limit", str(-limit)),
        ("fl", "timestamp"),
    ]
    try:
        resp = requests.get(
            WAYBACK_CDX_URL,
            params=params,
            headers={"User-Agent": f"webtrawl-cli/{__version__}"},
            timeout=WAYBACK_TIMEOUT_SECONDS,
        )
        rows = resp.json() if resp.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return []
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    timestamps = [row[0] for row in rows[1:] if row and row[0]]
    return [
        WAYBACK_RAW_URL.format(timestamp=timestamp, url=url) for timestamp in reversed(timestamps)
    ]


def _normalize_extract_url(url: str) -> str:
    """Rewrite known client-canvas URLs to their server-rendered HTML equivalents.

    GeneWeb links on ``gw.geneanet.org`` often carry ``type=tree``, which serves
    an empty JavaScript canvas (`gw_v5_graphtree`). Stripping ``type=tree`` makes
    GeneWeb return the full server-rendered HTML person sheet (`fiche`).
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host.endswith("geneanet.org") and parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            filtered = [(k, v) for k, v in pairs if not (k == "type" and v == "tree")]
            if len(filtered) != len(pairs):
                return urlunparse(parsed._replace(query=urlencode(filtered)))
    except Exception:
        pass
    return url


def _load_stored_cookies(url: str) -> dict[str, str]:
    """Load any reverse-cli cookies saved for the target host or its parent domain."""
    try:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return {}
        cookie_dir = Path.home() / ".cache" / "reverse-cli" / "cookies"
        if not cookie_dir.is_dir():
            return {}
        candidates = [host]
        parts = host.split(".")
        if len(parts) > 2:
            candidates.append(".".join(parts[-2:]))
        for candidate in candidates:
            path = cookie_dir / f"{candidate}.json"
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                cookies = payload.get("cookies")
                if isinstance(cookies, dict):
                    return {str(k): str(v) for k, v in cookies.items() if v is not None}
    except Exception:
        pass
    return {}


_SEMANTIC_MAIN_SELECTORS = (
    "#perso",
    "#fiche-content",
    "article",
    "main",
    "[role='main']",
    "#main-content",
    ".main-content",
    "#content",
)

_SEMANTIC_NOISE_SELECTORS = (
    "script, style, noscript, svg, iframe, nav, header, footer, aside, "
    ".sidebar, #sidebar, .nav, .menu, .breadcrumb, .breadcrumbs, "
    "#tarteaucitronRoot, #didomi-host, #onetrust-consent-sdk, #axeptio_overlay, "
    ".cky-consent-container, #cookiebot, #CybotCookiebotDialog, .cookie-banner"
)


def extract_semantic_main(html: str) -> str:
    """Extract structured text from semantic main containers when Trafilatura drops lists/cards."""
    if not html or html_is_challenge(html):
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for node in soup.select(_SEMANTIC_NOISE_SELECTORS):
            node.decompose()
        target = None
        for selector in _SEMANTIC_MAIN_SELECTORS:
            found = soup.select_one(selector)
            if found and len(found.get_text(" ", strip=True)) >= MIN_TEXT_LENGTH:
                target = found
                break
        if target is None:
            return ""
        raw_lines = target.get_text("\n", strip=True).splitlines()
        cleaned: list[str] = []
        for raw in raw_lines:
            line = re.sub(r"\s+", " ", raw).strip()
            if not line or "{{" in line or "}}" in line:
                continue
            if cleaned and cleaned[-1] == line:
                continue
            cleaned.append(line)
        return sanitize_markdown("\n".join(cleaned))
    except Exception:
        return ""


def _extract_html_markdown(html: str) -> tuple[str, bool]:
    """Extract markdown with Trafilatura, falling back to semantic DOM containers for structured pages.

    Trafilatura is tuned for article prose and discards ``<ul>/<li>/<div>`` trees
    (such as genealogy fiches on Geneanet/Geneastar or directory cards), keeping
    only footer paragraphs. Returns ``(markdown, used_semantic_dom)`` so the
    caller can relax the raw-HTML byte density gate when a compact semantic
    container (`#perso`, `#fiche-content`, `article`, `main`) was extracted from
    a large template shell.
    """
    if not html:
        return "", False
    import trafilatura

    traf_raw = (
        trafilatura.extract(
            html,
            output_format="markdown",
            favor_recall=True,
            include_tables=True,
            include_links=True,
        )
        or ""
    )
    traf_clean = sanitize_markdown(traf_raw)
    sem_clean = extract_semantic_main(html)
    if not sem_clean:
        return traf_clean, False
    raw_bytes = len(html.encode("utf-8", errors="ignore"))
    traf_ok = (
        bool(traf_clean)
        and extraction_reject_reason(traf_clean, raw_bytes) is None
        and detect_wall(traf_clean) is None
    )
    sem_ok = (
        len(sem_clean) > MIN_TEXT_LENGTH
        and not is_bot_blocked(sem_clean)
        and detect_wall(sem_clean) not in ("cookie wall", "maintenance page")
    )
    if sem_ok and not traf_ok:
        if traf_clean and traf_clean[:80] not in sem_clean:
            return f"{sem_clean}\n\n{traf_clean}", True
        return sem_clean, True
    if sem_ok and len(sem_clean) > len(traf_clean):
        return sem_clean, True
    return traf_clean, False


TLS_IMPERSONATE_PROFILES = ("chrome", "tor145", "chrome116")


def _silence_scrapling_loggers() -> None:
    """Suppress Scrapling INFO logs so CLI stdout/stderr remain clean TOON."""
    for name in ("scrapling", "scrapling.fetchers", "scrapling.engines", "scrapling.core"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False


def _tls_fetch_html(url: str, timeout_seconds: float) -> str:
    """Fetch HTML via TLS fingerprint impersonation, rotating JA3/JA4 profiles on challenge."""
    _silence_scrapling_loggers()
    sec = max(1, int(timeout_seconds))
    cookies = _load_stored_cookies(url)
    last_html = ""
    try:
        from curl_cffi import requests as curl_requests

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.google.com/",
        }
        for profile in TLS_IMPERSONATE_PROFILES:
            try:
                resp = curl_requests.get(
                    url,
                    impersonate=profile,
                    headers=headers,
                    cookies=cookies or None,
                    timeout=sec,
                    allow_redirects=True,
                )
                html = resp.text or ""
                if html:
                    last_html = html
                if resp.status_code in (200, 404) and html and not html_is_challenge(html):
                    return html
            except Exception:
                continue
    except Exception:
        pass

    try:
        from scrapling import Fetcher

        _silence_scrapling_loggers()
        page = Fetcher.get(url, impersonate="chrome", timeout=sec, retries=1)
        _silence_scrapling_loggers()
        html = getattr(page, "html_content", None) or (
            page.body.decode("utf-8", errors="ignore") if getattr(page, "body", None) else ""
        )
        if html:
            return html
    except Exception:
        pass
    return last_html


def wayback_extract(url: str, timeout: float = 20.0) -> str | None:
    """Return the newest usable Internet Archive snapshot of ``url`` as markdown.

    A datacenter IP is refused by managed challenges that an archive crawler
    passed long ago, and the snapshot often predates a paywall. The density floor
    is disabled here: archived HTML carries the archive toolbar and a large inline
    payload, so text share of raw bytes says nothing about completeness.
    """
    if not url.startswith(("http://", "https://")):
        return None
    url = _normalize_extract_url(url)
    for snapshot in _wayback_snapshot_urls(url):
        try:
            resp = requests.get(
                snapshot,
                headers={"User-Agent": f"webtrawl-cli/{__version__}"},
                timeout=timeout,
            )
        except requests.RequestException:
            continue
        if resp.status_code != 200 or not resp.text:
            continue
        clean, used_semantic = _extract_html_markdown(resp.text)
        reason = extraction_reject_reason(clean, 0)
        if used_semantic and reason in ("navigation chrome only", "mostly navigation chrome"):
            if len(clean) > MIN_TEXT_LENGTH and not is_bot_blocked(clean):
                reason = None
        if clean and reason is None:
            wall = detect_wall(clean)
            if wall in ("paywall", "login wall"):
                return f"⚠️ [{wall.upper()} — PARTIAL CONTENT]\n{clean}"
            return clean
    return None


def fetch_html(url: str, timeout: float = 45.0, force_browser: bool = False) -> tuple[str, str]:
    """Fetch raw page HTML through a two-stage transport cascade.

    Stage A spoofs a browser TLS fingerprint (fast, no browser, rotating JA3/JA4
    profiles when Cloudflare challenges Chrome 131+). Stage B launches a real
    patched Chromium with the Cloudflare solver, which is the only tier that
    clears interactive challenges. force_browser skips Stage A, for pages that answer
    200 with a client-rendered shell. Returns (html, tier_used).
    """
    if not url.startswith(("http://", "https://")):
        raise BadInputError("fetch expects an http(s) URL")
    url = _normalize_extract_url(url)
    logging.getLogger("scrapling").setLevel(logging.ERROR)

    if not force_browser:
        html = _tls_fetch_html(url, timeout)
        if html and not html_is_challenge(html):
            return html, "scrapling-tls"

    try:
        from scrapling.fetchers import StealthyFetcher

        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            solve_cloudflare=True,
            timeout=int(timeout * 1000),
            extra_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
            page_action=dismiss_cookie_banner,
            **_browser_kwargs(),
        )
        html = getattr(page, "html_content", None) or ""
        if html and not html_is_challenge(html):
            return html, "stealthy-browser"
        return html, "stealthy-browser:blocked"
    except Exception:
        return "", "failed"


def structured_extract(url: str, timeout: float = 45.0) -> dict[str, Any]:
    """Fetch a page and return the machine-readable facts it publishes.

    Independent of the prose pipeline: structured data survives when text-density
    extraction would drop it.
    """
    from webtrawl import structured

    html, tier = fetch_html(url, timeout=timeout)
    facts = structured.extract_facts(html) if html else {}
    if not facts.get("offers") and not facts.get("identity") and tier != "stealthy-browser":
        html, tier = fetch_html(url, timeout=timeout, force_browser=True)
        facts = structured.extract_facts(html) if html else {}

    facts["transport"] = tier
    facts["url"] = url
    facts["html_bytes"] = len(html)
    if not facts.get("offers") and not facts.get("identity"):
        raise TransientError(
            f"{url} published no structured data via {tier} "
            "(target likely renders its content client-side or blocks datacenter traffic)"
        )
    return facts


def _cap_cloudflare_solver_attempts(attempts: int) -> None:
    """Cap scrapling's Cloudflare solve-attempt loop for this process.

    scrapling hard-codes the cap in the private module global
    ``__CF_MAX_SOLVE_ATTEMPTS__`` and exposes no parameter, so patching it is the
    only lever. Normal mode allows one attempt; brute mode never calls this and
    keeps the library default.
    """
    try:
        from scrapling.engines._browsers import _stealth

        setattr(_stealth, "__CF_MAX_SOLVE_ATTEMPTS__", attempts)
    except Exception:
        pass


def jina_extract(
    url: str,
    timeout: float = 30.0,
    budget: float | None = None,
    report: dict[str, Any] | None = None,
) -> str:
    """Fetch a URL through the tiered extraction cascade under a wall-clock budget.

    Tiers run cheapest-first (Jina Reader, TLS fingerprint fetch, real patched
    Chromium, Internet Archive) and the first whose text clears the completeness
    gate wins, so a usable Tier 1 result never pays for the browser tiers. A
    consent wall is rejected and the next tier runs; a paywall is kept as partial
    content and only the archive can replace it. Each tier gets the smaller of
    ``timeout`` and the remaining budget; a tier that cannot start inside the
    remaining time is skipped. ``report``, when given, receives the per-tier
    timings on every exit path, including the failure one.
    """
    from webtrawl import hydration

    if not url.startswith(("http://", "https://")):
        raise BadInputError("extract expects an http(s) URL")
    url = _normalize_extract_url(url)
    logging.getLogger("scrapling").setLevel(logging.ERROR)

    total_budget = float(budget) if budget is not None else float(timeout)
    started_at = time.monotonic()
    deadline = started_at + total_budget
    tiers: list[dict[str, Any]] = []
    html_seen: list[str] = []
    # A paywall or login wall still carries a little real metadata (a date, a
    # place, masked field labels). Keep the best such document, but let a later
    # tier replace it: the archive often holds the pre-paywall page.
    partial: tuple[str, str, str] | None = None

    def remaining() -> float:
        return deadline - time.monotonic()

    def record(
        tier: str, started: float, text: str, raw_html_bytes: int, status: str, reason: str | None = None
    ) -> None:
        """Append one tier outcome to the cascade report."""
        entry: dict[str, Any] = {
            "tier": tier,
            "seconds": round(time.monotonic() - started, 2),
            "chars": len(text),
            "raw_bytes": raw_html_bytes,
            "status": status,
        }
        if reason:
            entry["reason"] = reason
        tiers.append(entry)

    def fill_report(accepted_tier: str | None, *, blocked: bool = False) -> None:
        """Publish the cascade report to the caller, on success and on failure."""
        if report is None:
            return
        report.update(
            {
                "tiers": tiers,
                "accepted_tier": accepted_tier,
                "elapsed_seconds": round(time.monotonic() - started_at, 2),
                "budget_seconds": total_budget,
                "blocked": blocked,
            }
        )
        if html_seen and html_seen[-1]:
            report["html"] = html_seen[-1]

    def accept(
        tier: str,
        started: float,
        text: str,
        raw_html_bytes: int,
        gate_bytes: int | None = None,
        *,
        semantic_container: bool = False,
    ) -> str | None:
        """Return the text when it clears the completeness gate, else record why not.

        ``gate_bytes`` overrides the byte count used by the density floor while the
        report keeps the real ``raw_html_bytes``. The browser tier passes 0: it has
        already executed the JavaScript, so a small text over a large bundle-heavy
        HTML is a rendered page, not a client-side shell. When ``semantic_container``
        is True, the text comes from a verified main content container (`#perso`,
        `#fiche-content`, `article`, `main`) whose lines may be short structured
        records (genealogy trees, tables, directory cards) rather than long prose.
        """
        nonlocal partial
        clean = sanitize_markdown(text or "")
        if not clean:
            record(tier, started, clean, raw_html_bytes, "empty")
            return None
        reason = extraction_reject_reason(clean, raw_html_bytes if gate_bytes is None else gate_bytes)
        if semantic_container and reason in ("navigation chrome only", "mostly navigation chrome"):
            if len(clean) > MIN_TEXT_LENGTH and not is_bot_blocked(clean):
                reason = None
        if reason is None:
            wall = detect_wall(clean)
            if wall in ("paywall", "login wall"):
                if partial is None or len(clean) > len(partial[2]):
                    partial = (tier, wall, clean)
                record(tier, started, clean, raw_html_bytes, "partial", wall)
                return None
            record(tier, started, clean, raw_html_bytes, "accepted")
            return clean
        record(tier, started, clean, raw_html_bytes, "rejected", reason)
        return None

    # Tier 1: Jina Reader (keyless) with automated boilerplate and navigation stripping.
    jina_text = ""
    started = time.monotonic()
    if remaining() >= MIN_TIER_SECONDS:
        try:
            resp = requests.get(
                f"{JINA_READER_URL}/{url}",
                headers={
                    "User-Agent": f"webtrawl-cli/{__version__}",
                    "Accept": "text/plain",
                    "X-Remove-Selector": JINA_REMOVE_SELECTORS,
                },
                timeout=max(1.0, min(timeout, remaining())),
            )
            if resp.status_code == 200:
                jina_text = resp.text
        except requests.RequestException:
            pass  # Fall through to Tier 2
        clean_jina = accept("jina-reader", started, jina_text, 0)
        if clean_jina is not None:
            fill_report("jina-reader")
            return clean_jina
    else:
        record("jina-reader", started, "", 0, "skipped", "not enough budget left")

    # Tier 2: TLS fingerprint impersonation (rotating JA3/JA4 profiles) + Trafilatura/semantic DOM extraction.
    # A paywall found at Tier 1 is served server-side; a browser cannot lift it, so
    # jump straight to the archive instead of paying 15 s for the same masked page.
    started = time.monotonic()
    if partial is None and remaining() >= MIN_TIER_SECONDS:
        try:
            html_src = _tls_fetch_html(url, max(1.0, min(timeout, remaining())))
            if html_src:
                html_seen.append(html_src)
            extracted_md, used_semantic = _extract_html_markdown(html_src) if html_src else ("", False)
            raw_bytes = len(html_src.encode("utf-8", errors="ignore")) if html_src else 0
            clean_scrapling = accept(
                "scrapling-tls+trafilatura",
                started,
                extracted_md,
                raw_bytes,
                gate_bytes=0 if used_semantic else None,
                semantic_container=used_semantic,
            )
            if clean_scrapling is not None:
                fill_report("scrapling-tls+trafilatura")
                return clean_scrapling
        except Exception as exc:
            record("scrapling-tls+trafilatura", started, "", 0, "error", str(exc)[:120])
    elif partial is not None:
        record("scrapling-tls+trafilatura", started, "", 0, "skipped", "paywall already found")
    else:
        record("scrapling-tls+trafilatura", started, "", 0, "skipped", "not enough budget left")

    # Tier 2b: real patched Chromium, one Cloudflare solve attempt, + Trafilatura/semantic DOM.
    started = time.monotonic()
    if partial is None and remaining() >= MIN_TIER_SECONDS:
        _cap_cloudflare_solver_attempts(CLOUDFLARE_SOLVE_ATTEMPTS_NORMAL)
        try:
            from scrapling.fetchers import StealthyFetcher

            page = StealthyFetcher.fetch(
                url,
                headless=True,
                network_idle=True,
                solve_cloudflare=True,
                timeout=int(max(1.0, min(timeout, remaining())) * 1000),
                extra_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
                page_action=dismiss_cookie_banner,
                **_browser_kwargs(),
            )
            html_src = getattr(page, "html_content", None) or ""
            if html_src:
                html_seen.append(html_src)
            extracted_md, used_semantic = _extract_html_markdown(html_src) if html_src else ("", False)
            clean_browser = accept(
                "stealthy-chromium+trafilatura",
                started,
                extracted_md,
                len(html_src.encode("utf-8", errors="ignore")) if html_src else 0,
                gate_bytes=0,
                semantic_container=used_semantic,
            )
            if clean_browser is not None:
                fill_report("stealthy-chromium+trafilatura")
                return clean_browser
        except Exception as exc:
            record("stealthy-chromium+trafilatura", started, "", 0, "error", str(exc)[:120])
    elif partial is not None:
        record("stealthy-chromium+trafilatura", started, "", 0, "skipped", "paywall already found")
    else:
        record("stealthy-chromium+trafilatura", started, "", 0, "skipped", "not enough budget left")

    # Tier 3: Internet Archive snapshot. A managed challenge that refuses this
    # datacenter IP was often solved by the archive crawler, and the snapshot may
    # predate the wall. Cheaper than giving up, and it needs no browser.
    started = time.monotonic()
    if remaining() >= MIN_TIER_SECONDS:
        archived = wayback_extract(url, timeout=max(5.0, min(20.0, remaining())))
        if archived is not None:
            record("wayback-machine", started, archived, 0, "accepted")
            fill_report("wayback-machine")
            return archived
        record("wayback-machine", started, "", 0, "miss")
    else:
        record("wayback-machine", started, "", 0, "skipped", "not enough budget left")

    # A paywall or login wall is not the document, but its masked metadata is
    # still the best evidence available. Return it explicitly labelled.
    if partial is not None:
        tier, wall, text = partial
        fill_report(tier)
        if report is not None:
            report["wall"] = wall
        return f"⚠️ [{wall.upper()} — PARTIAL CONTENT]\n{text}"

    # If Tier 1 returned content but it was a captcha/turnstile page, report the block.
    if is_bot_blocked(jina_text):
        domain = urlparse(url).hostname or url
        fill_report(None, blocked=True)
        return (
            f"⚠️ [EXTRACTION BLOCKED BY ANTI-BOT / CAPTCHA]\n"
            f"Target site ({domain}) is protected by Cloudflare Turnstile / PerimeterX robot verification.\n"
            f"Suggestion: Select an alternate open-access URL from search results (e.g. PubMed Central PMC, arXiv, Wikipedia, or open blog mirrors)."
        )

    for raw_html in html_seen:
        payloads = hydration.extract_embedded_payloads(raw_html)
        if payloads:
            fill_report("embedded-payloads")
            return hydration.render_payloads(payloads, url)

    budget_exhausted = any(tier["status"] == "skipped" for tier in tiers)
    fill_report(None)
    raise IncompleteExtractionError(
        url,
        tiers,
        budget_seconds=total_budget,
        elapsed_seconds=time.monotonic() - started_at,
        budget_exhausted=budget_exhausted,
    )


def brute_extract(url: str, timeout: float = 60.0) -> str:
    """Brute-force extraction for bot-walled pages (Vercel / Cloudflare).

    Skips the fast tiers (Jina proxy, TLS-only fetch) and goes straight to the
    real patched-Chromium tier (Scrapling StealthyFetcher, patchright engine,
    solve_cloudflare=True) + Trafilatura. Same (url, timeout) signature as
    jina_extract so the CLI stays symmetric: default path vs --brute path.

    Technology choice (why this stack, auditable):
    - Scrapling StealthyFetcher drives a real patched Chromium via patchright:
      it executes the page JavaScript and lets the challenge solver run, which
      TLS fingerprint spoofing alone (Fetcher.get) cannot do on Vercel Bot
      Manager / Cloudflare managed challenges.
    - Camoufox (patched Firefox, C++-level spoofing) is the documented upgrade
      trigger, not a new dependency here: same bypass class, heavier binary,
      kept out to stay minimal until StealthyFetcher provably fails.
    - No residential proxy by design (operator request): datacenter IP remains
      the known residual block vector; documented in TECH.md, not code-fixed.
    """
    if not url.startswith(("http://", "https://")):
        raise BadInputError("extract expects an http(s) URL")
    url = _normalize_extract_url(url)
    logging.getLogger("scrapling").setLevel(logging.ERROR)

    try:
        from scrapling.fetchers import StealthyFetcher

        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            solve_cloudflare=True,
            timeout=int(timeout * 1000),
            extra_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
            page_action=dismiss_cookie_banner,
            **_browser_kwargs(),
        )
        html_src = getattr(page, "html_content", None) or ""
        if html_src:
            clean, _ = _extract_html_markdown(html_src)
            wall = detect_wall(clean)
            if wall in ("paywall", "login wall"):
                return f"⚠️ [{wall.upper()} — PARTIAL CONTENT]\n{clean}"
            if clean and not is_bot_blocked(clean) and wall != "cookie wall" and len(clean) > 100:
                return clean
    except Exception as exc:
        raise TransientError(f"brute extraction failed for {url}: {exc}") from exc

    archived = wayback_extract(url, timeout=max(10.0, timeout))
    if archived is not None:
        return archived

    raise TransientError(f"brute extraction yielded nothing usable for {url}")



def parse_tavily(payload: dict) -> list[dict]:
    """Flatten a Tavily /search response into normalized rows."""
    rows: list[dict] = []
    for item in payload.get("results") or []:
        url = item.get("url") or ""
        host = urlparse(url).hostname or ""
        pub = item.get("published_date") or ""
        rows.append(
            {
                "title": item.get("title") or "",
                "url": url,
                "snippet": _strip_html(item.get("content")),
                "date": str(pub),
                "source": host,
            }
        )
    return rows


def tavily_search(
    query: str,
    *,
    api_key: str | None = None,
    count: int = 10,
    freshness: str | None = None,
    topic: str = "general",
) -> tuple[list[dict], dict]:
    """Search via Tavily (keyed Bearer auth, or keyless access-mode header).

    Keyless mode needs no account; it is rate-capped (cap unpublished), so
    keyless 4xx responses map to RateLimitError. time_range uses the same
    day|week|month|year vocabulary as our --freshness flag.
    """
    if topic not in ("general", "news"):
        raise BadInputError(f"invalid topic {topic!r}; use general|news")
    body: dict[str, Any] = {
        "query": query,
        "max_results": clamp_count(count),
        "topic": topic,
    }
    if freshness:
        # NOTE: unlike Brave (pd/pw/pm/py), Tavily wants the literal word.
        if freshness not in FRESHNESS_MAP:
            raise BadInputError(f"invalid freshness {freshness!r}; use day|week|month|year")
        body["time_range"] = freshness
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"
    try:
        resp = requests.post(TAVILY_SEARCH_URL, json=body, headers=headers, timeout=20.0)
    except requests.RequestException as exc:
        raise TransientError(f"network error: {exc}") from exc
    if resp.status_code == 429:
        raise RateLimitError("tavily rate limited (HTTP 429): switch method, e.g. -m brave or -m parallel")
    if resp.status_code in (401, 403):
        raise AuthMissingError(f"tavily rejected credentials (HTTP {resp.status_code})")
    if resp.status_code == 400 and not api_key:
        # keyless pool exhausted / gated: natural-language error bodies
        raise RateLimitError(f"tavily keyless refused: {resp.text[:200]}")
    if resp.status_code >= 500:
        raise TransientError(f"tavily server error (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise TransientError(f"tavily unexpected HTTP {resp.status_code}")
    payload = resp.json()
    vertical = "news" if topic == "news" else "web"
    return parse_tavily(payload), {"provider": "tavily", "vertical": vertical, "mode": "keyed" if api_key else "keyless"}


def parse_parallel(payload: dict) -> list[dict]:
    """Flatten a Parallel /v1/search response into normalized rows."""
    rows: list[dict] = []
    for item in payload.get("results") or []:
        url = item.get("url") or ""
        excerpts = item.get("excerpts") or []
        # Parallel excerpts are near-full page chunks: truncate hard for TOON rows
        snippet = _strip_html(str(excerpts[0]))[:300] if excerpts else ""
        rows.append(
            {
                "title": item.get("title") or "",
                "url": url,
                "snippet": snippet,
                "date": str(item.get("publish_date") or ""),
                "source": urlparse(url).hostname or "",
            }
        )
    return rows


def parallel_search(
    query: str,
    *,
    api_key: str,
    count: int = 10,
    freshness: str | None = None,
    mode: str = "fast",
) -> tuple[list[dict], dict]:
    """Search via Parallel Web Systems (keyed only, x-api-key header).

    mode: turbo (~250ms) | fast (~700ms) | basic | advanced (~3s).
    Freshness maps to an ISO after_date cutoff (no native news vertical).
    """
    if mode not in ("turbo", "fast", "basic", "advanced"):
        raise BadInputError(f"invalid mode {mode!r}; use turbo|fast|basic|advanced")
    # NOTE: root-caused live: the API REQUIRES search_queries (list); the
    # survey's "objective" field alone yields HTTP 422.
    body: dict[str, Any] = {"search_queries": [query], "mode": mode}
    advanced: dict[str, Any] = {"max_results": clamp_count(count)}
    if freshness:
        days = PARALLEL_FRESHNESS_DAYS.get(freshness)
        if days is None:
            raise BadInputError(f"invalid freshness {freshness!r}; use day|week|month|year")
        import datetime as _dt
        cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).date().isoformat()
        advanced["after_date"] = cutoff
    body["advanced_settings"] = advanced
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "User-Agent": USER_AGENT,
    }
    try:
        resp = requests.post(PARALLEL_SEARCH_URL, json=body, headers=headers, timeout=30.0)
    except requests.RequestException as exc:
        raise TransientError(f"network error: {exc}") from exc
    if resp.status_code == 402:
        raise RateLimitError(
            "parallel CREDITS EXHAUSTED (HTTP 402): switch to another method, e.g. "
            "webtrawl ... -m tavily (keyless ok) or -m brave"
        )
    if resp.status_code == 429:
        raise RateLimitError(
            f"parallel rate limited / quota reached (HTTP 429, body: {resp.text[:150]}): "
            "switch to another method, e.g. -m tavily or -m brave"
        )
    if resp.status_code in (401, 403):
        raise AuthMissingError(f"parallel rejected the key (HTTP {resp.status_code})")
    if resp.status_code >= 500:
        raise TransientError(f"parallel server error (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise TransientError(f"parallel unexpected HTTP {resp.status_code}")
    return parse_parallel(resp.json()), {"provider": "parallel", "vertical": "web"}


def _tinyfish_items(payload: Any) -> list:
    """Locate the result list in a TinyFish payload, tolerating container variants.

    Documented container is a top-level "results" list. "web"/"data" wrappers and a
    bare top-level list are accepted so a schema variant cannot crash the CLI.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    items = payload.get("results")
    if isinstance(items, list):
        return items
    for container in ("web", "data"):
        inner = payload.get(container)
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict) and isinstance(inner.get("results"), list):
            return inner["results"]
    return []


def parse_tinyfish(payload: Any) -> list[dict]:
    """Flatten a TinyFish search response into normalized rows.

    Documented shape: {"query","results":[{position, site_name, title, snippet, url,
    date?}],"total_results","page"}. Snippet fields are aliased for the same reason.
    """
    rows: list[dict] = []
    for item in _tinyfish_items(payload):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        snippet = item.get("snippet") or item.get("description") or item.get("content") or ""
        rows.append(
            {
                "title": str(item.get("title") or ""),
                "url": url,
                # tinyfish snippets are already short; the cap keeps TOON rows compact
                "snippet": _strip_html(str(snippet))[:300],
                "date": str(item.get("date") or ""),
                "source": str(item.get("site_name") or urlparse(url).hostname or ""),
            }
        )
    return rows


def tinyfish_search(api_key: str, query: str, *, count: int = 10, freshness: str | None = None,
                    country: str | None = None, lang: str | None = None) -> tuple[list[dict], dict]:
    """Search via the TinyFish Search API (keyed only, X-API-Key header).

    Free at any wallet balance but NOT keyless: an account plus a key from
    agent.tinyfish.ai are required. 30 requests/min per key. The API exposes no
    result-count parameter, so one page is sliced to `count` client-side.
    """
    params: dict[str, Any] = {"query": query}
    if freshness:
        minutes = TINYFISH_FRESHNESS_MINUTES.get(freshness)
        if minutes is None:
            raise BadInputError(f"invalid freshness {freshness!r}; use day|week|month|year")
        params["recency_minutes"] = minutes
    if country:
        params["location"] = country.upper()
    if lang:
        params["language"] = lang.lower()
    headers = {
        "Accept": "application/json",
        "X-API-Key": api_key,
        "User-Agent": USER_AGENT,
    }
    try:
        resp = requests.get(TINYFISH_SEARCH_URL, headers=headers, params=params, timeout=20.0)
    except requests.RequestException as exc:
        raise TransientError(f"network error: {exc}") from exc
    if resp.status_code == 429:
        raise RateLimitError("tinyfish rate limited (HTTP 429, 30 req/min): switch method, e.g. -m tavily (keyless ok) or -m brave")
    if resp.status_code in (401, 403, 404):
        raise AuthMissingError(f"tinyfish rejected the key or has no Search API access (HTTP {resp.status_code})")
    if resp.status_code == 402:
        raise AuthMissingError("tinyfish account lacks Search API access (HTTP 402)")
    if resp.status_code == 400:
        raise BadInputError(f"tinyfish refused the request (HTTP 400): {resp.text[:200]}")
    if resp.status_code >= 500:
        raise TransientError(f"tinyfish server error (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise TransientError(f"tinyfish unexpected HTTP {resp.status_code}")
    return parse_tinyfish(resp.json())[: clamp_count(count)], {"provider": "tinyfish", "vertical": "web"}
