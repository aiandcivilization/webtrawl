"""Harvesting of embedded SSR client state from raw page HTML.

A client-rendered application page (prompt cards, dashboards, feeds) has no article
prose, so a text-density extractor such as trafilatura returns the navigation bar. The
real content is usually shipped inside the SSR hydration stream instead, as a JSON blob
or as a devalue-style JavaScript expression.

This module recognises the stream shapes of the common frameworks and decodes them with
:mod:`webtrawl.jsvalue`. Payloads that cannot be decoded safely are omitted
rather than approximated.
"""

from __future__ import annotations

import json
import re
from typing import Any

from webtrawl.jsvalue import JsParseError, _Parser, _resolve, _tokenize

# Global variables that carry SSR state, matched on the identifier or on the
# property of a `window.` assignment.
STATE_GLOBALS = frozenset(
    {
        "__remixContext",
        "__NUXT__",
        "__INITIAL_STATE__",
        "__APOLLO_STATE__",
        "__PRELOADED_STATE__",
        "__DATA__",
    }
)
SVELTEKIT_PREFIX = "__sveltekit_"
NEXT_DATA_ID = "__NEXT_DATA__"

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.I)

# --------------------------------------------------------------------------- #
# Harvesting
# --------------------------------------------------------------------------- #


def _script_bodies(raw_html: str) -> list[tuple[str, str]]:
    """Return (attributes, body) for every inline <script> element."""
    return [(attrs, body) for attrs, body in _SCRIPT_RE.findall(raw_html or "")]


def _parse_global_assignments(body: str) -> list[tuple[str, Any]]:
    """Find ``NAME = <object literal>`` assignments for known SSR state globals."""
    try:
        tokens = _tokenize(body)
    except JsParseError:
        return []
    found: list[tuple[str, Any]] = []
    for idx, token in enumerate(tokens):
        kind, value = token
        if kind != "name":
            continue
        after = tokens[idx + 1] if idx + 1 < len(tokens) else None
        if after is not None and after == ("punct", "."):
            prop = tokens[idx + 2] if idx + 2 < len(tokens) else None
            if prop is None or prop[0] != "name":
                continue
            name = prop[1]
            assign_idx = idx + 3
        else:
            name = value
            assign_idx = idx + 1
        if not (name in STATE_GLOBALS or name.startswith(SVELTEKIT_PREFIX)):
            continue
        assign = tokens[assign_idx] if assign_idx < len(tokens) else None
        if assign != ("punct", "="):
            continue
        try:
            parser = _Parser(tokens)
            parser.pos = assign_idx + 1
            decoded = _resolve(parser.parse_value(), parser.table, frozenset())
        except (JsParseError, RecursionError):
            continue
        if name.startswith(SVELTEKIT_PREFIX) and isinstance(decoded, dict):
            inner = decoded.get("data")
            if isinstance(inner, (dict, list)):
                decoded = inner
        if isinstance(decoded, (dict, list)):
            found.append((name, decoded))
    return found


def _parse_tanstack(body: str) -> Any | None:
    """Decode the ``$_TSR.router=($R=>...)(...)`` devalue stream of TanStack Start.

    The router value is the *body* of a one-parameter arrow whose parameter is the
    flattening table, so the body is parsed directly and resolved against the
    references it registers.
    """
    try:
        tokens = _tokenize(body)
    except JsParseError:
        return None
    for idx in range(len(tokens) - 3):
        if tokens[idx] != ("name", "$_TSR") or tokens[idx + 1] != ("punct", "."):
            continue
        if tokens[idx + 2] != ("name", "router") or tokens[idx + 3] != ("punct", "="):
            continue
        parser = _Parser(tokens)
        parser.pos = idx + 4
        if not parser.accept("punct", "("):
            continue
        if not parser.accept("name", "$R"):
            continue
        if not parser.accept("punct", "=>"):
            continue
        try:
            decoded = _resolve(parser.parse_value(), parser.table, frozenset())
        except (JsParseError, RecursionError):
            return None
        if isinstance(decoded, (dict, list)):
            return decoded
    return None


def extract_embedded_payloads(raw_html: str) -> list[dict]:
    """Harvest embedded SSR client state from a page's raw HTML.

    Recognises the Next.js ``__NEXT_DATA__`` JSON blob, the global assignment
    forms used by Remix, Nuxt, SvelteKit, Apollo and generic preloaded-state
    conventions, and the devalue-style ``$_TSR.router`` stream of TanStack Start.

    Args:
        raw_html: The full page HTML as fetched.

    Returns:
        A list of ``{"framework": <name>, "data": <decoded>}`` dicts. Payloads
        that cannot be decoded safely are omitted rather than approximated.
    """
    payloads: list[dict] = []
    seen: set[str] = set()
    for attrs, body in _script_bodies(raw_html):
        if not body.strip():
            continue
        id_match = _ID_RE.search(attrs)
        if id_match and id_match.group(1) == NEXT_DATA_ID:
            try:
                payloads.append({"framework": "nextjs", "data": json.loads(body)})
            except ValueError:
                pass
            seen.add("nextjs")
            continue
        for name, data in _parse_global_assignments(body):
            if name in seen:
                continue
            seen.add(name)
            payloads.append({"framework": name, "data": data})
        if not any(p["framework"].startswith("tanstack") for p in payloads):
            router = _parse_tanstack(body)
            if router is not None:
                seen.add("tanstack")
                payloads.append({"framework": "tanstack", "data": router})
    return payloads


def render_payloads(payloads: list[dict], url: str) -> str:
    """Render decoded SSR payloads as extractable document text.

    Used as the last tier of the prose cascade, so the banner states plainly that
    the text layer was unusable and what was returned instead.

    Args:
        payloads: Payloads as returned by :func:`extract_embedded_payloads`.
        url: The page the payloads were harvested from.

    Returns:
        Markdown-ish text carrying a provenance header and one JSON block per payload.
    """
    names = ", ".join(str(p.get("framework", "unknown")) for p in payloads)
    lines = [
        f"# prose layer unusable for {url}: the page is a client-rendered application shell.",
        f"# decoded {len(payloads)} embedded SSR payload(s) instead: {names}",
        "",
    ]
    for payload in payloads:
        lines.append(f"## {payload.get('framework', 'unknown')}")
        lines.append(json.dumps(payload.get("data"), ensure_ascii=False, indent=1))
        lines.append("")
    return "\n".join(lines).rstrip()
