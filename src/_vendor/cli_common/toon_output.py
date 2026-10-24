"""Shared TOON (Token-Oriented Object Notation) tabular encoder for god-tier CLI tools.

Single source of truth for the hand-rolled ``columns[N]: / rows[N,]{cols}:``
emitter previously copy-pasted in transcribe_tool and world_model_tool (and
re-implemented with drifting escaping rules in twitter/jobspy/yahoo tools).
Use this module instead of re-implementing per tool.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["encode_primitive", "parse_compact_rows", "render_data_as_toon"]


def encode_primitive(value: Any, delimiter: str = ",") -> str:
    """Encode one scalar as a TOON-safe primitive (quoted+escaped when unsafe)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return "null"
        if value == 0:
            return "0"
        return str(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        if not value or value != value.strip() or value in ("null", "true", "false"):
            safe = False
        elif value.startswith("-"):
            safe = False
        else:
            try:
                float(value)
                safe = False
            except ValueError:
                unsafe_chars = [
                    ":",
                    delimiter,
                    "[",
                    "]",
                    "{",
                    "}",
                    '"',
                    "\\",
                    "\n",
                    "\r",
                    "\t",
                ]
                safe = not any(char in value for char in unsafe_chars)
        if safe:
            return value
        return f'"{escaped}"'
    return encode_primitive(str(value), delimiter)


def render_data_as_toon(
    columns: list[str], rows: list[dict[str, Any]], indent_size: int = 2
) -> str:
    """Render columns + row dicts as a compact TOON table block."""
    encoded_cols = [encode_primitive(c) for c in columns]
    lines = [f"columns[{len(columns)}]: {','.join(encoded_cols)}"]
    lines.append(f"rows[{len(rows)},]{{{','.join(columns)}}}:")
    indent = " " * indent_size
    for row in rows:
        row_vals = [encode_primitive(row.get(col)) for col in columns]
        lines.append(f"{indent}{','.join(row_vals)}")
    return "\n".join(lines)


def parse_compact_rows(text: str) -> list[dict[str, Any]]:
    """Read the compact TOON table published by some listing commands.

    Some tools publish a tighter dialect than the canonical one::

        @tweets
        rows[2]
        {id,author,text,likes,rts,time}
        1975913762344608129,@charliermarsh,As of Python 3.14,3050,352,Oct 08 13:18

    ``toon.decode`` does not read this dialect, so a caller that consumes
    those tools needs this reader. A cell is ``~`` when the value is missing,
    ``1`` / ``0`` for a boolean, and a plain string otherwise.

    :raises ValueError: when the text carries no compact table.
    """
    lines = text.splitlines()
    if len(lines) < 2 or not lines[1].startswith("rows["):
        raise ValueError("no compact TOON table in the payload")

    columns: list[str] = []
    cursor = 2
    if cursor < len(lines) and lines[cursor].startswith("{"):
        header = lines[cursor]
        columns = [part.strip() for part in header[1 : header.rindex("}")].split(",")]
        columns = [column for column in columns if column]
        cursor += 1

    rows: list[dict[str, Any]] = []
    for line in lines[cursor:]:
        if not line.strip():
            continue
        values = line.split(",")
        if len(values) < len(columns):
            values += [""] * (len(columns) - len(values))
        rows.append(
            dict(zip(columns, (_compact_value(value) for value in values), strict=False))
        )
    return rows


def _compact_value(raw: str) -> Any:
    """Read one cell of a compact table: ``~`` is nothing, digits are numbers."""
    value = raw.strip()
    if value == "~":
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value
