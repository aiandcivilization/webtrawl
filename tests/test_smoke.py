"""Smoke and contract tests: the CLI answers, and the gates that carry the value hold.

These tests are deliberately network-free. Every assertion here protects a
behaviour the README claims, so a regression fails in CI instead of in an
agent's context window.
"""

from __future__ import annotations

from typer.testing import CliRunner

from webtrawl import structured
from webtrawl.cli import app
from webtrawl.extract_cache import paginate_content
from webtrawl.providers import detect_wall, extraction_reject_reason, parse_gnews_rss

runner = CliRunner()


def test_help_lists_commands() -> None:
    """`--help` exits 0 and every documented command appears."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.stdout
    for command in ("web", "news", "extract", "image", "ocr", "providers", "config"):
        assert command in result.stdout


def test_providers_reports_readiness_without_network() -> None:
    """`providers` is the key diagnostic: it must run with no key and no network."""
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0, result.stdout
    assert "brave" in result.stdout.lower()


def test_cookie_wall_is_classified_not_returned() -> None:
    """A consent panel is a wall, never a page. This is the 2026-10-24 regression."""
    panel = (
        "Nous utilisons des cookies et des technologies similaires. "
        "Vous pouvez accepter ou refuser les cookies non essentiels. "
        "Gérer mes préférences. Politique de confidentialité."
    )
    assert detect_wall(panel) == "cookie wall"
    assert extraction_reject_reason(panel, 0) == "cookie wall"


def test_paywall_outranks_cookie_wall() -> None:
    """A page can carry both; the paywall is the useful classification."""
    both = (
        "Abonnez-vous pour lire la suite. Cet article est réservé aux abonnés. "
        "Nous utilisons des cookies pour améliorer votre expérience. Accepter les cookies."
    )
    assert detect_wall(both) == "paywall"


def test_prose_passes_the_gate() -> None:
    """Real prose must not be rejected by the wall detector."""
    prose = (
        "The turbinate bones sit inside the nasal cavity and warm the inspired air. "
        "Chronic hypertrophy reduces airflow and produces a sensation of permanent congestion. "
        "Surgery is considered when medical treatment fails after three months."
    )
    assert detect_wall(prose) is None


def test_offset_pagination_does_not_repeat_text() -> None:
    """`extract --offset` must scroll a cached page, not re-print it."""
    text = "".join(f"line {i:03d} of the document\n" for i in range(200))
    first = paginate_content(text, offset=0, max_chars=100, url="https://example.org/x")
    second = paginate_content(text, offset=100, max_chars=100, url="https://example.org/x")
    assert first["content"] != second["content"]
    assert first["next_offset"] == 100
    assert second["next_cmd"] == 'webtrawl extract "https://example.org/x" --offset 200 --max-chars 100'
    assert first["total_chars"] == len(text)


def test_money_parser_keeps_decimals_after_the_symbol() -> None:
    """French storefronts print `99€95` in a <sup>; the decimals must survive."""
    assert structured.parse_money("99€<sup>95</sup>") == 99.95
    assert structured.parse_money("82.51") == 82.51


def test_jsonld_price_is_extracted() -> None:
    """Structured facts live outside the prose node; the layer must find them."""
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Widget",'
        '"offers":{"@type":"Offer","price":"82.51","priceCurrency":"EUR",'
        '"availability":"https://schema.org/InStock"}}'
        "</script></head><body><p>Widget</p></body></html>"
    )
    facts = structured.extract_facts(html)
    assert facts["offers"][0]["price"] == 82.51
    assert facts["offers"][0]["currency"] == "EUR"
    assert facts["offers"][0]["availability"] == "in_stock"


def test_google_news_rss_parser() -> None:
    """The keyless fallback is the reason the tool runs with zero API keys."""
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>A title - Source</title><link>https://example.org/a</link>
      <source>Source</source><pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate>
      <description>&lt;a href="x"&gt;A title&lt;/a&gt;</description></item>
    </channel></rss>"""
    rows = parse_gnews_rss(xml)
    assert rows[0]["title"] == "A title"
    assert rows[0]["url"] == "https://example.org/a"
    assert rows[0]["source"] == "Source"
