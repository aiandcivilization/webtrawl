"""A tokenizer and recursive-descent parser for the devalue-style JavaScript payloads
that server-side-rendered frameworks ship inside ``<script>`` tags.

Such a payload is a JavaScript expression, not JSON: it flattens a graph into a
reference array (``$R[n]=value``) and minifies literals (``!0``, ``!1``, ``void 0``).
A regular expression would corrupt string contents, so this module lexes the source
properly and then walks it. Arrow functions, calls and member access have no knowable
runtime value without a JavaScript engine: they decode to None rather than to a guess.

The public entry point is :func:`parse_js_expression`.
"""

from __future__ import annotations

import re
from typing import Any

_NAME_RE = re.compile("[A-Za-z_$\\u0080-\\uffff][A-Za-z0-9_$\\u0080-\\uffff]*")
_NUM_RE = re.compile(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
}

# Keywords after which a '/' starts a regular expression, not a division.
_REGEX_PRECEDING_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "case",
        "do",
        "else",
        "yield",
        "await",
        "throw",
    }
)
_REGEX_UNSAFE_AFTER = frozenset({")", "]", "}", "++", "--"})
# Punctuation after which a '/' can only be a division.
_DIVISION_AFTER = frozenset({")", "]", "}"})

_CIRCULAR = "[Circular reference]"
_MAX_DEPTH = 200


class JsParseError(ValueError):
    """Raised when a script body is not decodable as the supported JSON subset."""


class _Ref:
    """A devalue flattening reference such as ``$R[7]``, resolved in a second pass."""

    __slots__ = ("index",)

    def __init__(self, index: int | str) -> None:
        self.index = index


def _js_truthy(value: Any) -> bool:
    """Report the JavaScript truthiness of a decoded value (``!0`` is true, ``!1`` false)."""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return False
    if isinstance(value, str) and value == "":
        return False
    return True


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #


def _read_string(src: str, i: int) -> tuple[str, int]:
    """Read a quoted JS string starting at ``src[i]``; return its value and the next index."""
    quote = src[i]
    out: list[str] = []
    j = i + 1
    while j < len(src):
        c = src[j]
        if c == "\\":
            j += 1
            if j >= len(src):
                break
            esc = src[j]
            if esc == "u":
                hexits = src[j + 1 : j + 5]
                if len(hexits) == 4 and all(
                    h in "0123456789abcdefABCDEF" for h in hexits
                ):
                    out.append(chr(int(hexits, 16)))
                    j += 5
                    continue
                out.append(esc)
                j += 1
                continue
            if esc == "x":
                hexits = src[j + 1 : j + 3]
                if len(hexits) == 2 and all(
                    h in "0123456789abcdefABCDEF" for h in hexits
                ):
                    out.append(chr(int(hexits, 16)))
                    j += 3
                    continue
                out.append(esc)
                j += 1
                continue
            out.append(_ESCAPES.get(esc, esc))
            j += 1
            continue
        if c == quote:
            return "".join(out), j + 1
        out.append(c)
        j += 1
    raise JsParseError("unterminated string literal")


def _read_template(src: str, i: int) -> tuple[str, int]:
    """Read a template literal, keeping ``${expr}`` interpolations as raw source text."""
    out: list[str] = []
    j = i + 1
    depth = 0
    while j < len(src):
        c = src[j]
        if c == "\\":
            out.append(src[j : j + 2])
            j += 2
            continue
        if depth == 0 and c == "`":
            return "".join(out), j + 1
        if depth == 0 and c == "$" and src[j + 1 : j + 2] == "{":
            out.append("${")
            depth = 1
            j += 2
            continue
        if depth > 0:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            out.append(c)
            j += 1
            continue
        out.append(c)
        j += 1
    raise JsParseError("unterminated template literal")


def _read_regex(src: str, i: int) -> tuple[str, int]:
    """Read a regular-expression literal, honouring escapes and character classes."""
    j = i + 1
    in_class = False
    while j < len(src):
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < len(src) and src[j].isalpha():
                j += 1
            return src[i:j], j
        elif c == "\n":
            break
        j += 1
    raise JsParseError("unterminated regular expression")


def _regex_can_follow(prev: tuple[str, str] | None) -> bool:
    """Decide whether a '/' following ``prev`` opens a regular expression."""
    if prev is None:
        return True
    kind, value = prev
    if kind == "punct":
        return value not in _DIVISION_AFTER and value not in ("++", "--")
    if kind == "name":
        return value not in _REGEX_PRECEDING_KEYWORDS
    return False


def _tokenize(src: str) -> list[tuple[str, str]]:
    """Split a JavaScript expression into (kind, value) tokens.

    Kinds are ``str``, ``tmpl``, ``regex``, ``name``, ``num`` and ``punct``.
    Comments and whitespace are dropped. ``=>`` is emitted as a single punct token.
    """
    tokens: list[tuple[str, str]] = []
    prev: tuple[str, str] | None = None
    i = 0
    size = len(src)
    while i < size:
        c = src[i]
        if c in " \t\r\n\f\v\u00a0\ufeff":
            i += 1
            continue
        if c == "/" and src[i + 1 : i + 2] == "/":
            nl = src.find("\n", i)
            i = size if nl < 0 else nl + 1
            continue
        if c == "/" and src[i + 1 : i + 2] == "*":
            end = src.find("*/", i + 2)
            i = size if end < 0 else end + 2
            continue
        if c in "\"'":
            value, i = _read_string(src, i)
            token = ("str", value)
        elif c == "`":
            value, i = _read_template(src, i)
            token = ("tmpl", value)
        elif c == "/":
            if _regex_can_follow(prev):
                value, i = _read_regex(src, i)
                token = ("regex", value)
            else:
                token = ("punct", "/")
                i += 1
        elif "0" <= c <= "9" or (c == "." and "0" <= src[i + 1 : i + 2] <= "9"):
            match = _NUM_RE.match(src, i)
            if match is None:
                raise JsParseError(f"unparsable number at offset {i}")
            token = ("num", match.group(0))
            i = match.end()
        elif c.isalpha() or c in "_$" or ord(c) > 127:
            match = _NAME_RE.match(src, i)
            if match is None:
                raise JsParseError(f"unparsable identifier at offset {i}")
            token = ("name", match.group(0))
            i = match.end()
        elif src.startswith("=>", i):
            token = ("punct", "=>")
            i += 2
        else:
            token = ("punct", c)
            i += 1
        tokens.append(token)
        prev = token
    return tokens


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class _Parser:
    """Recursive-descent parser for the devalue/JSON subset used by SSR streams."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.table: dict[int | str, Any] = {}
        self.depth = 0

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        idx = self.pos + offset
        return self.tokens[idx] if idx < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise JsParseError("unexpected end of expression")
        self.pos += 1
        return token

    def accept(self, kind: str, value: str) -> bool:
        token = self.peek()
        if token is not None and token[0] == kind and token[1] == value:
            self.pos += 1
            return True
        return False

    def expect(self, kind: str, value: str) -> None:
        if not self.accept(kind, value):
            found = self.peek()
            raise JsParseError(f"expected {value!r}, found {found!r}")

    def parse_value(self) -> Any:
        """Parse one JS value, registering flattening references in ``self.table``."""
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise JsParseError("nesting too deep")
        try:
            return self._parse_value()
        finally:
            self.depth -= 1

    def _parse_value(self) -> Any:
        token = self.peek()
        if token is None:
            raise JsParseError("unexpected end of expression")
        kind, value = token
        if kind == "str" or kind == "tmpl" or kind == "regex":
            self.pos += 1
            return value
        if kind == "num":
            self.pos += 1
            return (
                float(value) if ("." in value or "e" in value.lower()) else int(value)
            )
        if kind == "punct":
            if value == "{":
                return self._parse_object()
            if value == "[":
                return self._parse_array()
            if value == "(":
                return self._parse_paren()
            if value == "-":
                self.pos += 1
                inner = self.parse_value()
                return -inner if isinstance(inner, (int, float)) else None
            if value == "!" and (nxt := self.peek(1)) is not None and nxt[1] != "=":
                self.pos += 1
                return not _js_truthy(self.parse_value())
            if value == "new":
                self.pos += 1
                self._skip_expression()
                return None
            raise JsParseError(f"unsupported punctuation {value!r} in value position")
        if kind == "name":
            if value == "true":
                self.pos += 1
                return True
            if value == "false":
                self.pos += 1
                return False
            if value in ("null", "undefined"):
                self.pos += 1
                return None
            if value == "void":
                self.pos += 1
                self.parse_value()
                return None
            if value == "NaN" or value == "Infinity":
                self.pos += 1
                return None
            if value == "$R":
                return self._parse_reference()
            if value == "async" or self._is_callable_head():
                return self._parse_arrow()
            self.pos += 1
            return None
        raise JsParseError(f"unsupported token {token!r}")

    def _is_callable_head(self) -> bool:
        """Report whether the identifier at the cursor is the parameter of an arrow."""
        return self.peek(1) == ("punct", "=>")

    def _parse_object(self) -> dict[str, Any]:
        self.expect("punct", "{")
        out: dict[str, Any] = {}
        while True:
            token = self.peek()
            if token is None:
                raise JsParseError("unterminated object literal")
            kind, value = token
            if kind == "punct" and value == "}":
                self.pos += 1
                return out
            if kind == "punct" and value == ",":
                self.pos += 1
                continue
            if kind == "punct" and value == "...":
                self.pos += 1
                self.parse_value()
                continue
            if kind in ("str", "name", "num"):
                self.pos += 1
                key = str(value)
            else:
                raise JsParseError(f"unsupported object key {token!r}")
            nxt = self.peek()
            if nxt is not None and nxt[0] == "punct" and nxt[1] == ":":
                self.pos += 1
                out[key] = self.parse_value()
            elif nxt is not None and nxt[0] == "punct" and nxt[1] == "(":
                self._skip_call()
                out[key] = None
            else:
                out[key] = None
            if self.accept("punct", ","):
                continue

    def _parse_array(self) -> list[Any]:
        self.expect("punct", "[")
        out: list[Any] = []
        while True:
            token = self.peek()
            if token is None:
                raise JsParseError("unterminated array literal")
            kind, value = token
            if kind == "punct" and value == "]":
                self.pos += 1
                return out
            if kind == "punct" and value == ",":
                self.pos += 1
                continue
            if kind == "punct" and value == "...":
                self.pos += 1
                self.parse_value()
                continue
            out.append(self.parse_value())
            if self.accept("punct", ","):
                continue

    def _parse_paren(self) -> Any:
        """Parse a parenthesised expression, an IIFE call, or arrow-function parameters."""
        nxt1 = self.peek(1)
        nxt2 = self.peek(2)
        if nxt1 is not None and nxt1[0] == "name" and nxt2 == ("punct", "=>"):
            self.pos += 1
            self._parse_arrow()
            self.expect("punct", ")")
            if self.accept("punct", "("):
                self._skip_call()
            return None
        if self._arrow_ahead():
            return self._parse_arrow()
        self.pos += 1
        inner = self.parse_value()
        if not self.accept("punct", ")"):
            raise JsParseError("unbalanced parentheses")
        if self.accept("punct", "("):
            self._skip_call()
            return None
        return inner

    def _arrow_ahead(self) -> bool:
        """Report whether the '(' at the cursor opens arrow-function parameters."""
        depth = 0
        idx = self.pos
        while idx < len(self.tokens):
            kind, value = self.tokens[idx]
            if kind == "punct":
                if value == "(":
                    depth += 1
                elif value == ")":
                    depth -= 1
                    if depth == 0:
                        nxt = (
                            self.tokens[idx + 1] if idx + 1 < len(self.tokens) else None
                        )
                        return nxt == ("punct", "=>")
            idx += 1
        return False

    def _parse_arrow(self) -> None:
        """Consume an arrow function (or async arrow) and return None for it."""
        self.accept("name", "async")
        token = self.peek()
        if token is None:
            raise JsParseError("unexpected end of expression in arrow")
        if token[0] == "punct" and token[1] == "(":
            depth = 0
            while True:
                inner = self.take()
                if inner[0] != "punct":
                    continue
                if inner[1] == "(":
                    depth += 1
                elif inner[1] == ")":
                    depth -= 1
                    if depth == 0:
                        break
        else:
            self.take()
        self.expect("punct", "=>")
        if self.accept("punct", "{"):
            self._skip_block()
            return None
        body_start = self.pos
        table_snapshot = dict(self.table)
        self.parse_value()
        if self._at_expression_end():
            return None
        self.pos = body_start
        self.table = table_snapshot
        self._skip_expression()
        return None

    def _at_expression_end(self) -> bool:
        """Report whether the cursor sits on the delimiter that terminates an expression."""
        token = self.peek()
        if token is None or token[0] != "punct":
            return False
        return token[1] in (",", ";", ")", "]", "}", "=>")

    def _skip_expression(self) -> None:
        """Consume one expression up to the delimiter that closes it, without interpreting it."""
        depth = 0
        while True:
            token = self.take()
            if token[0] != "punct":
                continue
            value = token[1]
            if value in ("(", "[", "{"):
                depth += 1
            elif value in (")", "]", "}"):
                if depth == 0:
                    self.pos -= 1
                    return
                depth -= 1
            elif depth == 0 and value in (",", ";", "=>"):
                self.pos -= 1
                return

    def _skip_block(self) -> None:
        """Consume a balanced ``{ ... }`` block without interpreting its contents."""
        depth = 1
        while depth:
            token = self.take()
            if token[0] != "punct":
                continue
            if token[1] == "{":
                depth += 1
            elif token[1] == "}":
                depth -= 1

    def _skip_call(self) -> None:
        """Consume a ``( ... )`` argument list, tolerating trailing member access."""
        self.accept("punct", "(")
        depth = 1
        while depth:
            token = self.take()
            if token[0] != "punct":
                continue
            if token[1] == "(":
                depth += 1
            elif token[1] == ")":
                depth -= 1
        while True:
            token = self.peek()
            if token is not None and token[0] == "punct" and token[1] == ".":
                self.pos += 1
                self.take()
                continue
            break

    def _parse_reference(self) -> Any:
        """Parse ``$R[n]`` and its assignment, member-access and call suffixes."""
        self.expect("name", "$R")
        self.expect("punct", "[")
        token = self.take()
        if token[0] == "num":
            index: int | str = int(token[1])
        elif token[0] == "str":
            index = token[1]
        else:
            raise JsParseError(f"unsupported reference index {token!r}")
        self.expect("punct", "]")
        nxt = self.peek()
        if nxt is not None and nxt[0] == "punct" and nxt[1] == "=":
            self.pos += 1
            value = self.parse_value()
            self.table[index] = value
            return value
        if nxt is not None and nxt[0] == "punct" and nxt[1] in (".", "("):
            self._skip_call()
            return None
        return _Ref(index)


def _resolve(
    node: Any, table: dict[int | str, Any], stack: frozenset[int | str]
) -> Any:
    """Replace ``_Ref`` placeholders with the registered values, guarding cycles."""
    if isinstance(node, _Ref):
        if node.index in stack:
            return _CIRCULAR
        if node.index not in table:
            return None
        return _resolve(table[node.index], table, stack | {node.index})
    if isinstance(node, dict):
        return {key: _resolve(value, table, stack) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve(value, table, stack) for value in node]
    return node


def parse_js_expression(source: str) -> Any:
    """Decode one JavaScript value expression into plain Python data.

    Supports JSON literals, the devalue reference table (``$R[n]=``), template
    literals, and minified booleans/undefined. Arrow functions, calls, member
    access and bare identifiers decode to None: their runtime value is not
    knowable without a JavaScript engine, and guessing is worse than admitting it.

    Args:
        source: A JavaScript expression, e.g. ``$R[0]={a:!0}``.

    Returns:
        The decoded value: dict, list, str, int, float, bool or None.

    Raises:
        JsParseError: If the expression leaves the supported subset.
    """
    parser = _Parser(_tokenize(source))
    value = parser.parse_value()
    return _resolve(value, parser.table, frozenset())
