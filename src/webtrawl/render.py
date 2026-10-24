"""Output rendering: TOON only.

Contract per cli-tool-packaging Rule 1: every command prints TOON. No `--json`
flag and no JSON on stdout at all — JSON is transport for machine *input*, never
an output, because a caller that deserialises results with Python is the defect
that rule exists to prevent.

Rich is gone for the same rule. `Console.print` soft-wraps every line to the
console width (80 when stdout is a pipe), which splits quoted values mid-string
and made this tool unable to read back its own TOON ("Unterminated string:
missing closing quote"). The data channel uses the builtin print, which never
wraps; stderr diagnostics use plain writes for the same reason.
"""

from __future__ import annotations

import sys
from typing import Any, NoReturn

from toon import encode as toon_encode

from _vendor.cli_common.toon_output import render_data_as_toon
from webtrawl import documents


def err(message: str) -> None:
    """Write one diagnostic line to stderr. Never wraps, never touches stdout."""
    print(message, file=sys.stderr)

RESULT_COLUMNS = ["n", "title", "url", "type", "date", "source", "snippet", "extract_cmd"]
COMPACT_COLUMNS = ["n", "title", "url", "type", "date", "source", "extract_cmd"]
FULL_COLUMNS = RESULT_COLUMNS
SNIPPET_MAX = 350



def truncate(text: str, limit: int = SNIPPET_MAX) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def numbered(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, start=1):
        row = dict(r)
        row["n"] = i
        out.append(row)
    return out


def extract_cmd_for(url: str, kind: str) -> str:
    """The exact command that turns this row into readable content.

    Same command for pages and documents, because `extract` detects the
    document type itself. The suffix on a document row is what stops an agent
    from reaching for curl or a local PDF parser: the tool already knows how to
    fetch and convert it.
    """
    cmd = f"webtrawl extract {url} --max-chars 3000"
    if kind == "web":
        return cmd
    return f"{cmd} # {kind}: the tool downloads the file and converts it to markdown for you"


def print_results(
    rows: list[dict],
    *,
    full: bool = True,
    compact: bool = False,
    meta: dict | None = None,
) -> None:
    """Print search results as a TOON table: full with snippets (default), or compact."""
    numbered_rows = numbered(rows)
    include_snippets = full and not compact
    columns = RESULT_COLUMNS if include_snippets else COMPACT_COLUMNS
    prepared = []
    for r in numbered_rows:
        url = r.get("url", "")
        kind = documents.document_kind(url) or "web"
        prepared.append(
            {
                **r,
                "snippet": truncate(r.get("snippet", ""), SNIPPET_MAX),
                "title": truncate(r.get("title", ""), 160),
                "type": kind,
                "extract_cmd": extract_cmd_for(url, kind),
            }
        )
    # Provenance first, always in TOON, never behind --json: in `-m auto` the
    # cascade returns the FIRST provider that answers (brave > tavily >
    # parallel > tinyfish), so a silent fallback changes ranking and bias with
    # no visible difference in the table. Without this line an agent debugging
    # "why are these results junk" has no way to know which index answered.
    if meta:
        provenance = {k: v for k, v in meta.items() if v not in (None, "")}
        provenance["result_count"] = len(numbered_rows)
        print(toon_encode(provenance))
    print(render_data_as_toon(columns, prepared))
    print("\n# 💡 [MANDATORY STEP 2 FOR AI AGENTS]")
    print("# Search is Step 1 (discovery). You MUST now execute Step 2:")
    print("# Run `webtrawl extract <url> --max-chars 3000` on the most relevant URL(s) above.")
    print("# If the page is truncated, scroll through chunks with `--offset N` without re-fetching.")
    print("# A row whose `type` is not `web` is a document (pdf/docx/xlsx/csv/...): the very same")
    print("# extract command downloads the file and converts it to markdown. Never curl a document URL.")




OFFER_COLUMNS = ["price", "currency", "availability", "condition", "seller", "valid_until", "source"]
FACT_COLUMNS = ["field", "value"]


def format_structured(facts: dict) -> str:
    """Render extracted structured facts as TOON blocks: offers, identity, ratings."""
    lines: list[str] = []
    offers = facts.get("offers") or []
    if offers:
        rows = [{k: o.get(k) for k in OFFER_COLUMNS} for o in offers]
        lines.append(f"# offers ({len(rows)})")
        lines.append(render_data_as_toon(OFFER_COLUMNS, rows))
        lines.append("")
    identity = facts.get("identity") or {}
    if identity:
        lines.append("# identity")
        lines.append(render_data_as_toon(FACT_COLUMNS, [{"field": k, "value": v} for k, v in identity.items()]))
        lines.append("")
    ratings = facts.get("ratings") or []
    if ratings:
        lines.append("# ratings")
        lines.append(render_data_as_toon(["type", "value", "count"], ratings))
        lines.append("")
    amounts = facts.get("amounts") or []
    if amounts:
        lines.append("# amounts (unstructured, advisory — page declares no Product schema)")
        lines.append(render_data_as_toon(["value", "currency", "format", "context"], amounts))
        lines.append("")
    meta = {
        "url": facts.get("url"),
        "transport": facts.get("transport"),
        "html_bytes": facts.get("html_bytes"),
        "jsonld_types": facts.get("jsonld_types"),
        "microdata_blocks": facts.get("microdata_blocks"),
    }
    lines.append("# meta")
    lines.append(toon_encode({k: v for k, v in meta.items() if v is not None}))
    return "\n".join(lines).rstrip()


def print_payload(payload: dict[str, Any]) -> None:
    """Print a single-value/status payload as TOON."""
    flat = {k: v for k, v in payload.items()}
    print(toon_encode(flat))


def fail(message: str, code: int, suggestion: str | None = None) -> NoReturn:
    err(f"error ({code}): {message}")
    if suggestion:
        err(f"suggestion: {suggestion}")
    raise SystemExit(code)
