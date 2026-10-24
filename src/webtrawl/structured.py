"""Structured-data extraction for scraped pages.

Trafilatura is built for prose: it keeps the densest <article> node and discards
everything else. Machine-readable facts (price, availability, sku, brand, rating,
identifier) live outside that node, so a successful prose extraction silently
loses them. This module reads the structured layer that pages publish explicitly
(JSON-LD, microdata, OpenGraph) and normalises it, so callers never depend on
fragile selectors or on a single locale's number format.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_LD_RE = re.compile(
    r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_META_RE = re.compile(r"<meta\s+([^>]+?)/?>", re.S | re.I)
_ITEMSCOPE_RE = re.compile(r"<([a-z0-9]+)\b[^>]*itemscope[^>]*>(.*?)</\1>", re.S | re.I)
_ITEMPROP_RE = re.compile(
    r'<([a-z0-9]+)\b[^>]*itemprop\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</\1>', re.S | re.I
)
_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*["\']([^"\']*)["\']')

SCHEMA_ORG = re.compile(r"^https?://schema\.org/(.+)$", re.I)

AVAILABILITY = {
    "instock": "in_stock",
    "in_stock": "in_stock",
    "limitedavailability": "in_stock",
    "preorder": "preorder",
    "backorder": "backorder",
    "outofstock": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "discontinued",
    "offline": "out_of_stock",
}



def strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def parse_money(value: Any) -> float | None:
    """Parse a money value written in any common French or international format.

    Handles ``82.51``, ``82,51 €``, ``82,51€``, ``€82.51``, ``99€95`` and the
    ``99€<sup>95</sup>`` markup that French storefronts emit (the decimals sit in a
    <sup> *after* the symbol, which no locale-agnostic amount regex matches), plus
    grouped thousands.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = strip_tags(str(value))
    if not text:
        return None
    text = text.replace("\u202f", " ").replace("\u00a0", " ").replace("&euro;", "€")

    def to_float(raw: str) -> float | None:
        cleaned = raw.replace(" ", "").replace(" ", "")
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    patterns = (
        r"(\d{1,3}(?:[.\s]\d{3})*)[,.](\d{2})\s*€",
        r"(\d{1,3}(?:[.\s]\d{3})*)[,.](\d{2})",
        r"€\s*(\d{1,3}(?:[.\s]\d{3})*)[,.](\d{2})",
        r"(\d+)\s*€\s*(\d{2})",
        r"€\s*(\d{1,3}(?:[.\s]\d{3})*)\b",
        r"(\d{1,3}(?:[.\s]\d{3})*)\s*€",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        whole = match.group(1)
        if len(match.groups()) == 1:
            value_parsed = to_float(whole)
        else:
            value_parsed = to_float(f"{whole}.{match.group(2)}")
        if value_parsed is not None and 0 < value_parsed < 1e7:
            return round(value_parsed, 2)
    return None


def _schema_type(node: Any) -> str:
    raw = node.get("@type") if isinstance(node, dict) else None
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    if not isinstance(raw, str):
        return ""
    match = SCHEMA_ORG.match(raw.strip())
    return (match.group(1) if match else raw.strip()).split("/")[-1]


def _walk(node: Any, out: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, out)
        return
    if not isinstance(node, dict):
        return
    if "@graph" in node:
        _walk(node["@graph"], out)
    if _schema_type(node):
        out.append(node)
    for key, value in node.items():
        if key in ("offers", "hasPart", "itemListElement", "mainEntity", "about", "subjectOf"):
            _walk(value, out)
    for value in node.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "@type" in value[0]:
            _walk(value, out)


def _loads_any(blob: str) -> list[Any]:
    """Parse a script body holding one JSON document or several concatenated ones."""
    text = strip_tags(blob)
    for candidate in (text, blob.strip()):
        try:
            return [json.loads(candidate)]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    out: list[Any] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n,[]":
            index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError):
            break
        out.append(value)
    return out


def extract_jsonld(html: str) -> list[dict]:
    """Return every JSON-LD node published in the page, including @graph members."""
    out: list[dict] = []
    for blob in _LD_RE.findall(html or ""):
        for value in _loads_any(blob):
            _walk(value, out)
    return out


def extract_meta(html: str) -> dict[str, str]:
    """Return OpenGraph and product meta tags as a flat mapping."""
    out: dict[str, str] = {}
    for attrs in _META_RE.findall(html or ""):
        pairs = dict(_ATTR_RE.findall(attrs))
        key = (pairs.get("property") or pairs.get("name") or pairs.get("itemprop") or "").lower()
        content = pairs.get("content", "").strip()
        if key and content and key not in out:
            out[key] = content
    return out


def extract_microdata(html: str) -> list[dict]:
    """Return itemscope blocks reduced to a flat property mapping."""
    out: list[dict] = []
    for _tag, body in _ITEMSCOPE_RE.findall(html or "")[:200]:
        props: dict[str, str] = {}
        for _ptag, pname, pvalue in _ITEMPROP_RE.findall(body):
            value = strip_tags(pvalue)[:300]
            if pname not in props and value:
                props[pname] = value
        if props:
            out.append(props)
    return out


def normalise_availability(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "unknown"
    match = SCHEMA_ORG.match(text)
    if match:
        text = match.group(1)
    slug = re.sub(r"[^a-z]", "", text.lower())
    return AVAILABILITY.get(slug, slug or "unknown")


def _node_offers(node: dict) -> list[dict]:
    offers = node.get("offers")
    if offers is None:
        return []
    return offers if isinstance(offers, list) else [offers]


_AMOUNT_CONTEXT_RE = re.compile(
    r'([^<>{}]{0,70}?)(\d{1,3}(?:[.\s]\d{3})*)\s*€\s*(?:<sup>)?(\d{2})', re.I | re.S
)
_AMOUNT_PLAIN_RE = re.compile(r"([^\n]{0,70}?\d{1,4}[.,]\d{2}\s*(?:€|&euro;|&#8364;))", re.I)


def extract_amounts(html: str, limit: int = 12) -> list[dict]:
    """Recover numeric amounts a page renders but never declares as structured data.

    Covers storefronts that publish no Product schema and print prices in markup no
    amount regex matches, such as ``99€<sup>95</sup>``. Results are advisory: they
    carry their surrounding text so a caller can tell a price from a basket total.
    """
    found: list[dict] = []
    seen: set[tuple[str, float]] = set()

    for match in _AMOUNT_CONTEXT_RE.finditer(html or ""):
        whole = re.sub(r"[.\s]", "", match.group(2))
        value = to_float_safe(f"{whole}.{match.group(3)}")
        if value is None:
            continue
        context = strip_tags(match.group(1))[-70:]
        key = (context, value)
        if key in seen:
            continue
        seen.add(key)
        found.append({"value": value, "context": context, "currency": "EUR", "format": "symbol-before-decimals"})

    for match in _AMOUNT_PLAIN_RE.finditer(html or ""):
        snippet = strip_tags(match.group(1))
        value = parse_money(snippet)
        if value is None:
            continue
        context = snippet[-70:]
        key = (context, value)
        if key in seen:
            continue
        seen.add(key)
        found.append({"value": value, "context": context, "currency": "EUR", "format": "trailing-symbol"})

    found.sort(key=lambda a: a["value"])
    return found[:limit]


def to_float_safe(raw: str) -> float | None:
    try:
        return round(float(raw), 2)
    except (ValueError, AttributeError):
        return None


def extract_facts(html: str) -> dict[str, Any]:
    """Extract machine-readable facts from a page: offers, identity, ratings.

    Generic by design: the caller gets every published offer plus the product or
    article identity, without hardcoding which fields a given vertical uses.
    """
    nodes = extract_jsonld(html)
    meta = extract_meta(html)
    microdata = extract_microdata(html)

    offers: list[dict] = []
    ratings: list[dict] = []
    identity: dict[str, Any] = {}

    for node in nodes:
        kind = _schema_type(node)
        if kind in ("Product", "SoftwareApplication", "Book", "Vehicle", "Offer", "AggregateOffer"):
            for key in ("name", "sku", "mpn", "gtin13", "gtin", "brand", "description", "category", "color", "size"):
                if node.get(key) and key not in identity:
                    value = node[key]
                    if isinstance(value, dict):
                        value = value.get("name")
                    identity[key] = strip_tags(str(value))[:300] if value else None
        if kind in ("AggregateRating", "Review"):
            value = node.get("ratingValue")
            rating = parse_money(value if not isinstance(value, str) else value.replace(",", "."))
            if rating is not None:
                ratings.append({"type": kind, "value": rating, "count": node.get("ratingCount") or node.get("reviewCount")})
        for offer in _node_offers(node):
            if not isinstance(offer, dict):
                continue
            amount = parse_money(
                offer.get("price")
                if offer.get("price") is not None
                else offer.get("lowPrice", offer.get("highPrice"))
            )
            seller = offer.get("seller")
            if isinstance(seller, dict):
                seller = seller.get("name")
            records = []
            if offer.get("priceSpecification"):
                specs = offer["priceSpecification"]
                records = specs if isinstance(specs, list) else [specs]
            for spec in records:
                spec_amount = parse_money(spec.get("price")) if isinstance(spec, dict) else None
                if spec_amount is not None:
                    offers.append(
                        {
                            "price": spec_amount,
                            "currency": spec.get("priceCurrency") or offer.get("priceCurrency"),
                            "availability": normalise_availability(spec.get("availability") or offer.get("availability")),
                            "condition": normalise_availability(offer.get("itemCondition")) or None,
                            "seller": seller,
                            "valid_until": offer.get("priceValidUntil"),
                            "url": offer.get("url"),
                            "source": "jsonld:priceSpecification",
                        }
                    )
            if amount is not None:
                offers.append(
                    {
                        "price": amount,
                        "currency": offer.get("priceCurrency"),
                        "availability": normalise_availability(offer.get("availability")),
                        "condition": normalise_availability(offer.get("itemCondition")) or None,
                        "seller": seller,
                        "valid_until": offer.get("priceValidUntil"),
                        "url": offer.get("url"),
                        "source": f"jsonld:{kind or 'Offer'}",
                    }
                )

    if not offers:
        for prop in microdata:
            amount = parse_money(prop.get("price"))
            if amount is not None:
                offers.append(
                    {
                        "price": amount,
                        "currency": prop.get("pricecurrency") or meta.get("product:price:currency") or meta.get("og:price:currency"),
                        "availability": normalise_availability(prop.get("availability")),
                        "condition": None,
                        "seller": None,
                        "valid_until": None,
                        "url": prop.get("url"),
                        "source": "microdata",
                    }
                )

    for key, value in meta.items():
        if key.endswith(":price:amount") or key == "price":
            amount = parse_money(value)
            if amount is not None:
                offers.append(
                    {"price": amount, "currency": None, "availability": "unknown", "condition": None,
                     "seller": meta.get("og:site_name"), "valid_until": None, "url": None, "source": f"meta:{key}"}
                )
        if key.endswith(":availability") and offers:
            for offer in offers:
                if offer["availability"] == "unknown":
                    offer["availability"] = normalise_availability(value)

    for slot, key in (("name", "og:title"), ("description", "og:description"), ("brand", "og:brand"),
                      ("sku", "product:retailer_item_id"), ("image", "og:image")):
        if not identity.get(slot) and meta.get(key):
            identity[slot] = strip_tags(meta[key])[:300]

    deduped: list[dict] = []
    seen: set[tuple] = set()
    for offer in offers:
        key = (offer["price"], offer["currency"], offer["availability"], offer["source"])
        if key not in seen:
            seen.add(key)
            deduped.append(offer)
    deduped.sort(key=lambda o: (o["availability"] != "in_stock", o["price"]))

    return {
        "offers": deduped,
        "identity": {k: v for k, v in identity.items() if v},
        "ratings": ratings,
        "amounts": extract_amounts(html) if len(deduped) < 2 else [],
        "jsonld_types": sorted({_schema_type(n) for n in nodes if _schema_type(n)}),
        "microdata_blocks": len(microdata),
    }
