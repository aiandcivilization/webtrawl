# Benchmark and how to reproduce it

Two numbers carry this project:

1. a page costs far fewer tokens through `webtrawl` than as raw HTML;
2. TOON costs a little less than compact JSON on the same rows.

Both are measured, on the dates and commands below, with one tokenizer named
explicitly. Re-run them before quoting them.

## Environment

- Date: 2026-10-24
- Host: Ubuntu VPS, datacenter IP, no residential proxy
- Python 3.12, `uv` sync of this repository
- Tokenizer: **`o200k_base`** from `tiktoken` (the GPT-4o/o-series vocabulary)
- Raw HTML: `curl -L -A "Mozilla/5.0"`, no header tuning
- CLI output: complete stdout of the command, TOON header block included

## 1. Page tokens: raw HTML versus extracted markdown

```bash
curl -sL -A "Mozilla/5.0" "https://en.wikipedia.org/wiki/Car"            -o /tmp/car.html
curl -sL -A "Mozilla/5.0" "https://docs.python.org/3/library/asyncio.html" -o /tmp/asyncio.html

webtrawl extract "https://en.wikipedia.org/wiki/Car" --max-chars 0 > /tmp/car.md
webtrawl extract "https://docs.python.org/3/library/asyncio.html" --max-chars 0 > /tmp/asyncio.md

python - <<'PY'
import tiktoken, pathlib
enc = tiktoken.get_encoding("o200k_base")
for name, raw, clean in (
    ("Wikipedia: Car", "/tmp/car.html", "/tmp/car.md"),
    ("Python docs: asyncio", "/tmp/asyncio.html", "/tmp/asyncio.md"),
):
    r = pathlib.Path(raw).read_text(errors="ignore")
    c = pathlib.Path(clean).read_text(errors="ignore")
    print(name, len(enc.encode(r)), len(enc.encode(c)))
PY
```

| Page | Raw bytes | Raw tokens | Extracted chars | Extracted tokens | Cut |
|---|---|---|---|---|---|
| `en.wikipedia.org/wiki/Car` | 929,307 | 291,846 | 158,800 | 43,657 | −85.0 % |
| `docs.python.org/3/library/asyncio.html` | 25,411 | 6,943 | 3,739 | 965 | −86.1 % |

Wall clock, `--no-cache`, second run of the same day: **2.0 s** for the Wikipedia
article (tier `jina-reader`), **0.5 s** for the Python page.

What the tool reports for the first one:

```console
$ webtrawl extract "https://en.wikipedia.org/wiki/Car" --max-chars 300
url: "https://en.wikipedia.org/wiki/Car"
transport: jina-reader
cache: miss
total_chars: 157794
tiers[1]{tier,seconds,chars,raw_bytes,status}:
  jina-reader,3.02,157794,0,accepted
elapsed_seconds: 3.02
```

Read the `tiers[]` line before quoting a reduction: tier 1 (Jina) produced this
one. The TLS + Trafilatura tier produces the same order of magnitude, but the
transport that served the text is always stated, so a benchmark cannot silently
credit the wrong layer.

## 2. Format: TOON versus JSON on ten news rows

Same rows, three encodings, same tokenizer. `gnews_news()` is keyless, so this
reproduces with no account.

```bash
python - <<'PY'
import json
from toon import encode
from webtrawl.providers import gnews_news

rows, _meta = gnews_news("central bank rates", count=10)
open("/tmp/rows.toon", "w").write(encode(rows))
open("/tmp/rows.json", "w").write(json.dumps(rows, ensure_ascii=False, indent=2))
open("/tmp/rows.min.json", "w").write(json.dumps(rows, ensure_ascii=False))
PY
```

| Encoding | Characters | Tokens |
|---|---|---|
| TOON | 5,120 | **2,410** |
| JSON, pretty | 5,894 | 2,680 |
| JSON, compact | 5,632 | 2,590 |

TOON is 6.9 % cheaper than *compact* JSON here, and 10 % cheaper than pretty
JSON. That is a modest saving, and this README says so: the payload is dominated
by long snippet strings, where the format buys little. The real savings are in
step 1 — not resending a page's chrome — and in the shape: fixed columns, a
`type` per row, and an `extract_cmd` the agent can run without rebuilding a URL.

## 3. Claims this project does not make

- No benchmark against Exa, Firecrawl, TinyFish or a hosted search API. Those
  comparisons would need keys, identical queries and a human relevance judgement.
- No accuracy score for extraction. Trafilatura's own benchmarks cover that; the
  only extraction claims here are the wall classification and the reductions
  above.
- No claim that the cascade defeats `amazon.fr`, `fnac.com` or `leboncoin.fr`.
  Those answer 403 to both transports from a datacenter IP; only residential
  egress changes that. It is documented as a known limit, not worked around.
