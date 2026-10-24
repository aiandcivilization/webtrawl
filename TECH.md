# 🛠️ TECH.md — Architecture & Technology Stack Reference

> **Purpose of this document:**  
> This file lists all libraries, APIs, protocols, and architectural modules used by `internet_search_tool`.  
> For each technology, it documents its exact purpose, pros, limitations, and the **Upgrade Trigger** (when and why to replace it if a superior SOTA tool emerges).

---

## 🏗️ Core Technology Matrix

| Technology / Library | Layer | Purpose & Role | Key Strengths | Limitations | Upgrade Trigger / Alternatives |
|---|---|---|---|---|---|
| **Brave Search API** | Discovery / Index | Web & News search query resolver (independent index of >20B pages). | Fast, independent of Google/Bing, high-relevance SERP snippets. | Free tier requires credit card for new keys; 1 req/sec on free tier. | Replace/Supplement if **Exa.ai** or **Parallel API** provides better semantic embeddings or higher free limits. |
| **Tavily API** | Discovery / Index | AI-native search fallback (keyless mode supported). | Optimized for LLM agents, provides clean text snippets natively, keyless tier. | Rate-limited on keyless pool during traffic spikes. | Upgrade if **Firecrawl Search** or **Parallel Turbo** offers better latency/token cost ratio. |
| **Parallel Search API** | Discovery / Index | Enterprise SOTA web search (advanced neural retrieval). | #1 on Artificial Analysis Search Index (August 2026), deep domain coverage. | Key required, $1/1k calls after free quota. | Benchmark against Exa Neural Search. |
| **Jina Reader (`r.jina.ai`)** | Extraction (Tier 1) | Keyless URL-to-clean-markdown converter proxy. | Zero configuration, strips standard boilerplate, fast (~20 RPM free). | Blocked by hard Cloudflare Turnstile/PerimeterX captchas on enterprise sites. | Fall back to Tier 2 (Scrapling) when blocked. |
| **Scrapling (`scrapling[fetchers]`)** | Extraction (Tier 2 / Stealth) | Anti-bot bypass fetcher with TLS fingerprint spoofing & headless browsers. | Uses `curl_cffi` + `browserforge` + `patchright`, 784x faster than BS4, bypasses Cloudflare Turnstile. | Heavy dependencies (requires headless browser for JS-rendered DOMs). | Upgrade if **Camoufox standalone** or **Crawl4AI v1** offers lighter binary footprint. |
| **curl-cffi** | Network / TLS | Mimics browser JA3/JA4 TLS fingerprints and HTTP/2 frames (Chrome/Safari). | Evades Cloudflare bot detection at the network transport layer. | Only handles HTTP transport (does not execute client-side JS by itself). | Monitor for updates in JA4 signature databases. |
| **Pillow** | Image / Assembly | Reads a picture's pixel size; stitches a Zoomify tile rectangle into one JPEG. | Already present in the venv; tile addressing is arithmetic, so a 3888x5184 page rebuilds in 5 s. | Cannot invent missing tiles: a refused tile raises instead of leaving a white patch. | If a viewer moves to IIIF, request the `region` endpoint directly instead of stitching. |
| **Tesseract 5** | OCR / local floor | Reads printed text from a picture, offline, in 0.6 s. | Always installed, no key, no network, deterministic. | Measured on an 18th-century register: two meaningless lines out of a full act. It is a floor for print, not a handwriting engine. | Replace only if a small local VLM (e.g. an ONNX olmOCR/DeepSeek-OCR build) fits the VPS without a GPU. |
| **Gemini 3.5 Flash (vision)** | OCR / handwriting ceiling | Transcribes manuscript and print from a base64 image. | Measured: the register act in 1.1 s (3.4 s with a crop round-trip), [illisible] used instead of a guess when the instruction says so. Free tier exists, RPD-bound. | EU/UK/CH free-tier restrictions apply to products with users; paid tier removes them. Output varies between calls on a faded photo. | Switch provider when a cheaper document model wins on a manuscript benchmark; the CLI keeps the provider pluggable. |
| **Mistral OCR (la Plateforme)** | OCR / document specialist | `POST /v1/ocr`, `mistral-ocr-latest`, accepts `image_url` (base64 data URL) and returns `pages[].markdown` with optional bounding boxes. | Purpose-built for documents, 40+ languages, confidence scores per word/block available. $4 per 1000 pages ($0.40 batched): $0.004 an act. | No API free tier documented (the free *chat* plan is not free API access); key required, so it is an opt-in path here. | Adopt as default if a key is provisioned and its manuscript accuracy beats Gemini on our own samples. |
| **Trafilatura** | Content Parsing / Cleaning | Heuristic-based main text and metadata extractor from raw HTML. | SOTA precision for removing boilerplate, navbars, footers, ads, and comment noise without regex fragility. | Requires raw HTML string input. | Benchmark against **Resiliparse** or **Readability-lxml** if speed is bottleneck. |
| **python-toon / cli_common** | Serialization / Output | Token-efficient tabular serialization for AI agents. | ~40-60% token savings over JSON/YAML, structured 7-column output with `extract_cmd`. | Text format requiring parser on recipient side. | Standard format across all god-tier CLI tools. |
| **Typer** | CLI & UX | Command-line parsing, subcommands, exit code handling. | Clean typing annotations, auto-generated help, strict exit codes (0/1/2/3/4). | None. | Industry standard Python CLI framework. |
| **Rich** | REMOVED | Was used for stdout and stderr. | **Removed 2026-10-08.** `Console.print` soft-wraps to 80 columns when stdout is a pipe, which split quoted values mid-string: `--json` raised `Invalid control character` and the tool could not decode its own TOON (`Unterminated string`). The data channel now uses builtin `print`; stderr diagnostics use a plain `err()` helper. Do not reintroduce rich on stdout — the byte-clean contract (cli-tool-packaging Rule 1) depends on it. | — | — |

---

## 🖼️ Image Acquisition (measured 2026-10-24)

A genealogy investigation stalled because the only evidence on a page was a
scan. Three beliefs were wrong, and each one is now covered by a measurement:

| Belief | Measurement | Consequence in the code |
|---|---|---|
| "The CDN is bot-walled, only a browser gets the picture" | `curl_cffi` with **no** `Referer` → 403; with `Referer: <page that embeds it>` → 200, 300 ms | `images._plain_fetch` always sends the referer. The browser is an escalation, not the default. |
| "The viewer serves a bigger JPEG than 1000 px" | `thumbnail-1000-fill.jpg` → 200; `thumbnail-1100..2000` → 406 | `THUMB_LADDER` stops at 1000 and the ceiling is probed, not guessed |
| "Native resolution is unreachable" | `ImageProperties.xml` gives 3888x5184, TILESIZE 256; the native page stitches in 5 s (336 tiles, 2.3 MB) | `--full` rebuilds the page, `--region X,Y,W,H` fetches only the tiles that cover the rectangle |
| "A thread pool is how you fetch 50 tiles" | Same 2300x900 native crop: **3.44 s** cold with a 6-thread pool, **0.58 s** over one keep-alive async session; 3 pages went **13.6 s -> 3.05 s** | `images.fetch_many_async` reuses one `curl_cffi.AsyncSession`; the thread pool is gone |

Two details that cost a debugging round and must not be re-derived:

- **`TileGroupN` is ignored by the CDN.** `TileGroup9/6-0-0.jpg` answers 200 and
  `TileGroup0/6-0-0.jpg` answers the same bytes, so the group is hard-coded to 0.
- **`TileGroup0` is not always right on other viewers.** The group is derived
  from a tile index that spans the whole pyramid; this CDN ignores it, a
  standard Zoomify install does not. If a tile answers 404 at a high level,
  compute the group before suspecting the coordinates.

Bursts matter: the CDN starts challenging beyond roughly a dozen simultaneous
requests, so **one semaphore bounds the whole command** -- pages and tiles share
it, because connections are now reused across the batch instead of being opened
per tile. Measured: 18 concurrent connections (3 pages x 6 workers) made the CDN
throttle and took 65 s; 8 shared connections do the same work in 3 s. Each URL
retries twice, and a batch where *every* URL failed is the only case that
escalates to ONE browser session (~9 s total instead of ~9 s each) -- a partial
loss is retried over the same session.

Cancellation is the second reason for async. A cancelled task stops at its await;
a cancelled thread cannot be killed, so an abandoned provider used to keep the
process alive for its full socket timeout. That is where the measured 43 s stall
came from: 25 s deadline + 18 s waiting for a thread nobody wanted any more.

---

## 🔤 OCR: the floor, the ceiling, and why it never degrades silently

Measured on an 18th-century parish register (Saint-Eustache, Paris), same crop:

| Engine | Time | Output |
|---|---|---|
| tesseract 5.3.4, `-l fra` | 0.6 s | `® Auï, Le,` / `= LL: EAl Ja` — line noise |
| Gemini 3.5 Flash (vision) | 1.1 s | `Du mardi 27 avril 1762.` / `1 Marie, né d'hier, fils de` / `2 du Roy et de Marie Fra…` |
| Gemini with its reasoning pass | 24.8 s | the same three lines — so the pass is off by default and `--deep` turns it on |

The chain is hedged, not sequential: `fast` starts Gemini flash-lite, and if it
has not answered within `hedge_after` (1.5 s) the next vision provider starts in
parallel; the first non-empty answer wins and the others are **cancelled**. A
provider failure costs 60 s of cooldown, not a 5-minute blacklist, and the reason
is appended to `~/.cache/god-tier-cli/ocr/last_failures.log`.

| Path | Measured |
|---|---|
| one picture, healthy Gemini | 1.1 s model, **1.9 s** end to end |
| 3 local pictures, `-c 3` | 0.7 s cached, ~3 s cold |
| 3 pages: download + crop + OCR | **8.6 s** cold (was 31 s sequential, 65 s with parallel pages) |
| repeat of the same page and region | **1.1 s** — picture cache then transcription cache, no network |

Two rules follow, and both are encoded in `ocr.py`:

- **The local engine is a floor, not a fallback.** It runs when no vision model is
  configured or when every vision provider has failed or timed out, and its result
  carries `note: "FLOOR ENGINE ... treat this transcription as unreliable"` so the
  caller cannot mistake line noise for an act. A provider pinned with `-p gemini`
  is a promise: if it fails, the command exits 1 instead of degrading.
- **The transcription is never corrected.** The prompt demands `[illisible]` for
  an unreadable word, because a completed word is a fabricated source. A cut
  answer is detected from `finishReason` and reported as `answer cut by the model`
  instead of passing as a short act.

### Free providers, and the ones that only look free

| Claim | Verified |
|---|---|
| "Hugging Face gives free inference credits" | **False for free accounts.** The pricing page is explicit: Free = *None* monthly credits; PRO = $2/month. HF is a model catalogue, not a free OCR API. |
| "Mistral OCR is free" (post seen on X) | **False.** $4/1000 pages. The free *plan* is chat/Studio, not API. |
| "There are many free Chinese OCR APIs" | **Partly true, and usable.** Any OpenAI-compatible host plugs into this CLI's `vlm` provider: Zhipu `glm-4v-flash` (documented as a free model), Alibaba DashScope (`qwen-vl-ocr` free quota on signup), SiliconFlow and ModelScope (free tiers). Only OpenRouter was tested here, because its key already existed in the fleet. |
| "Free models are good enough" | **Measured, not assumed.** `dots-studio/dots-3-note-preview:free` on the register crop: correct three lines, `cost: 0`, 23.8 s — against 1.1 s for Gemini. Free costs latency, not accuracy. |

Switching the free host is configuration, not code:

```bash
internet-search config set vlm KEY                     # OpenRouter by default
export VLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
export VLM_MODEL=glm-4v-flash                          # Zhipu, documented free
```

Key handling: `internet-search config set gemini KEY` (file is 0600), or
`GEMINI_API_KEY` in the environment. `internet-search providers` reports which
OCR engine is ready and where its key came from, never the key itself.

---

## 🔄 Two-Stage Extraction Architecture

```
[ Query: internet-search web "..." ]
                 │
                 ▼
[ Stage 1: Index Discovery ] ──► Brave API / Tavily Keyless / Parallel
                 │
                 ▼
[ Output TOON Table with extract_cmd column ]
                 │
                 ▼ (Agent runs extract_cmd)
[ Stage 2: Multi-Tier Extraction & Sanitization ]
                 │
                 ├──► Tier 1: Jina Reader (Fast keyless proxy)
                 │         │
                 │         ├── If Clean ──► Sanitize & Return Markdown
                 │         └── If Bot-Blocked / Captcha ──► Trigger Tier 2
                 │
                 ├──► Tier 2: Scrapling (curl_cffi Chrome TLS) + Trafilatura Parsing
                 │         │
                 │         ├── If Clean ──► Sanitize & Return Markdown
                 │         └── If Hard Captcha ──► Return Clean Fallback Warning (Zero JS Leak)
                 │
                 ├──► Sanitizer Filter: Strips minified JS, Adobe Reactor, and tracking blobs
                 │
                 └──► Tier 3: Local Disk Cache & Offset Scrolling (~/.cache/internet-search/extract_cache/)
                           │
                           ├── Hit (Fresh) ──► Sub-millisecond slice, 0 network requests
                           └── Miss / Stale ──► Fetch, sanitize, cache full markdown, slice [offset : offset + 4000]
```

---

## 🔬 Deep Synergy: Why Scrapling + Trafilatura Are Indissociable

A common architectural question is: *"Why do we need Trafilatura if Scrapling already fetches the page?"*

The answer lies in the **strict separation between the Transport Layer and the Semantic Density Layer**:

```
┌───────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. SCRAPLING = THE STEALTH INFILTRATOR (Transport & Anti-Bot Layer)                               │
│ ➔ Role : Spoofs Chrome JA3/JA4 TLS fingerprints and HTTP/2 frames to bypass Cloudflare.          │
│ ➔ Payload Produced : The ENTIRE RAW HTML DOM of the page (complete with 100% of UI noise).       │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼ (Raw HTML: 223,065 bytes on Wikipedia)
┌───────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. TRAFILATURA = THE SURGICAL BOILERPLATE CLEANER (DOM Text Density NLP Layer)                    │
│ ➔ Role : Calculates per-node text-to-tag ratios, locates the substantive <article> node,          │
│          and DISCARDS 100% of header navbars, language menus, footers, ads, and cookie widgets.   │
│ ➔ Payload Produced : Pure, dense substantive Markdown.                                           │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼ (Clean Markdown: 17,175 bytes)
                                    TOKEN REDUCTION: -92.3%
```

### Empirical Proof (Live Benchmark on Wikipedia CAR Article):

| Pipeline Stage | Tool | Payload Size | Estimated Token Cost | Contents Extracted |
|---|---|---|---|---|
| **Raw Fetch** | `Scrapling.Fetcher.get()` | **223,065 bytes** | $\approx 55,000\text{ tokens}$ | Menus in 80 languages, search bars, edit history, legal footers, tracking scripts. |
| **Clean Extraction** | `trafilatura.extract()` | **17,175 bytes** | $\approx 4,000\text{ tokens}$ | **100% pure scientific article** (headers `#`, definitions, clinical citations). |

### Summary Rules:
* **Scrapling without Trafilatura:** You bypass Cloudflare, but you dump 50,000 tokens of useless navigation garbage into the agent's context window.
* **Trafilatura without Scrapling:** You have the best text cleaner in the world, but Cloudflare blocks you with HTTP 403 / Captcha HTML on protected sites.
* **Scrapling + Trafilatura (Golden Pair):** Scrapling unlocks the door, Trafilatura extracts only the gold and leaves the trash outside.

---

## 📈 Benchmarking & Future Replacement Criteria

1. **Search Layer Replacement Criteria :**
   - If a provider demonstrates higher F1 scores on agent search benchmarks (e.g. *Artificial Analysis Search Index*) while maintaining $<1.0\text{s}$ latency and $< \$1/1\text{k}$ requests.
2. **Extraction Layer Replacement Criteria :**
   - If a parser outperforms `Trafilatura + Scrapling` on F1 text extraction accuracy on CommonCrawl / News / Academic datasets without increasing latency.
3. **Bot Bypass Replacement Criteria :**
   - If an open-source library passes 100% of Cloudflare Turnstile / Datadome interactive challenges in $<1.5\text{s}$ in headless mode.


---

## 🔧 Structured-Data Layer (added 2026-09-26)

### Why a third parser exists

Trafilatura is a *text-density* parser: it keeps the densest `<article>` node and
discards the rest. Structured facts — price, availability, sku, brand, gtin,
rating — are published outside that node. A page could therefore be fetched
successfully and still return **no price**, which is what the pre-2026-09-26
extraction cascade did on every French storefront.

The layer is deliberately generic. It reads whatever the page declares
(JSON-LD, microdata, OpenGraph) and never hardcodes a vertical:

```
structured.py
├── extract_jsonld()      all @type nodes, @graph + concatenated documents
├── extract_microdata()   itemscope blocks flattened to properties
├── extract_meta()        OpenGraph / product meta tags
├── extract_facts()       offers + identity + ratings, normalised
├── extract_amounts()     advisory fallback for pages with no Product schema
└── parse_money()         locale-agnostic amount parsing
```

### Two parser bugs this layer had to fix

1. **Decimal corruption.** Stripping the XSSI/concatenated-JSON wrapper by regex
   (`\.\d{1,2}` removal) silently rewrote `"price": "82.51"` into `"82"`. Replaced
   by `json.JSONDecoder.raw_decode`, which parses concatenated documents *without*
   touching the numeric literals.
2. **Symbol-before-decimals.** French storefronts print
   `<div class="price">99€<sup>95</sup></div>`. The decimals sit in a `<sup>`
   *after* the symbol, so no locale-agnostic amount regex (`\d+[.,]\d{2}\s*€`)
   matches it. `parse_money` now covers the `99€<sup>95</sup>` shape, and
   `extract_amounts` reports those values with surrounding context, flagged
   advisory so a basket total is never mistaken for a price.

### Transport escalation

`fetch_html()` now escalates to the patched-Chromium tier
(`StealthyFetcher`, `solve_cloudflare=True`) whenever the TLS-only tier returns a
challenge **or a 200 shell that declares no structured data** — the second case
being the client-rendered storefront.

Measured on the 2026-09-26 repair: `community.acer.com` went from
`[EXTRACTION BLOCKED BY ANTI-BOT / CAPTCHA]` to 15,665 characters of extracted
forum content. Prose extraction is unchanged elsewhere (Wikipedia: 14,648 chars).

### Known limits (not code-fixable)

`amazon.fr`, `fnac.com` and `leboncoin.fr` answer 403 to both transports, and
Amazon additionally serves a degraded variant page (`?th=1`) under a browser
fingerprint. These are IP- and reputation-level bot managers, not TLS
fingerprints: per the IP doctrine in the scraping skill, no amount of engine
tuning fixes them — only residential proxy egress does. Do not spend time
re-tuning the stack against them.

---

## 💥 Brute mode (`extract --brute`, added 2026-09-26)

### Why it exists

Default `extract` is latency-first: Jina proxy, then TLS-only fetch, then the
patched-Chromium tier only as fallback. On Vercel Bot Manager (e.g.
`shotgun.live`, "Vercel Security Checkpoint", HTTP 429) the fast tiers always
lose: no JavaScript executes, so the challenge never clears. `--brute` skips
them and goes straight to the real browser tier. No cache by design (bot-wall
sessions are single-use).

### Why patchright-via-Scrapling, not Camoufox standalone

- `StealthyFetcher` (Scrapling, patchright engine, `solve_cloudflare=True`) is
  already a declared dependency and drives a real patched Chromium: JS runs,
  the solver runs. That is the whole gap vs TLS-only fetch.
- Camoufox (patched Firefox, C++-level spoofing, bypasses Turnstile / managed
  challenges per X reports 2026-09) stays the documented **upgrade trigger**:
  same bypass class, extra binary weight, adopted only if StealthyFetcher
  provably fails on a target class.
- No residential proxy by operator decision: datacenter IP stays the known
  residual block vector. If `--brute` still returns a checkpoint, it is the IP
  reputation, not the engine — do not re-tune, switch egress or source.

### Symmetry contract (auditable)

- `providers.jina_extract(url, timeout)` = default cascade (fast → slow).
- `providers.brute_extract(url, timeout)` = browser tier only, same signature.
- CLI `extract --brute` mirrors `--structured`: same paginate tail, only the
  fetch head differs. Vercel markers live in `is_bot_blocked` + patterns so
  both paths detect the checkpoint instead of returning it as content.

---

## 🧱 Wall detection, consent dismissal and the archive tier (added 2026-10-24)

### The failure this fixes

A consent panel, a paywall or a maintenance notice carries enough prose to clear
the old completeness gate. The cascade returned it as the page. Measured on
`gw.geneanet.org/pierfit` (tarteaucitron, 2,765 chars of cookie panel) and
`en.geneanet.org/cercles/...` (paywall, 5,701 chars of masked record labels).
The agent then answered from the wall as if it were the document.

### Detection

`providers.detect_wall(text)` groups signature phrases by type: `cookie wall`,
`paywall`, `login wall`, `maintenance page`. Two distinct phrases classify the
text; one phrase only counts when it heads a document under 2,500 chars and the
whole text is under 6,000 chars. Priority is paywall → login wall → cookie wall,
because a page can carry both and the paywall is the useful classification.

`extraction_reject_reason` rejects `cookie wall` and `maintenance page`. A
`paywall` or `login wall` passes the gate but the cascade records it as
`partial`, keeps the longest such text, and lets the archive tier replace it.

### Consent dismissal

`dismiss_cookie_banner(page)` is a Scrapling `page_action`. It only touches
vendor containers: `#tarteaucitronRoot`, `#didomi-host`, `#onetrust-consent-sdk`,
`#axeptio_overlay`, `.cky-consent-container`, `#cookiebot`,
`#CybotCookiebotDialog`. It prefers reject/deny controls, then accept, then
removes the container after 50 ms.

Contrastive failure, kept as a regression rule: the first implementation clicked
the first visible button whose text matched `/accept|ok/`. On a Geneanet tree it
matched `ok` inside `Family Book`, navigated to the family book, and extracted
the wrong page. Never click a generic page button for consent.

### Archive tier

`wayback_extract(url)` queries the CDX API with `limit=-3` (the newest three
snapshots, not the oldest three), filters `statuscode:200` and `mimetype:text/html`,
collapses by digest, fetches `https://web.archive.org/web/<ts>id_/<url>` (the raw
capture), and extracts with Trafilatura. The density floor is disabled for this
tier: archived HTML carries the archive toolbar and inline payloads, so text share
of raw bytes says nothing. The bot/cookie/nav checks still apply.

It runs automatically after the browser tier, and `--wayback` runs it directly.
Why it matters: a datacenter IP is refused by managed challenges that the archive
crawler passed from a different egress, and the snapshot often predates the
paywall. Measured: `en.geneastar.org` (Cloudflare Turnstile, 403 from the VPS)
returns a 2025-05-18 snapshot with 792 chars of usable text.

### Browser tier and the density floor

The patched-Chromium tier passes `gate_bytes=0`: it has already executed the
JavaScript, so 643 chars of prose over an 84 KB bundle-heavy HTML is a rendered
page, not a client-side shell. The TLS tier keeps the strict floor.

### Cache invalidation

`extract_cache.CACHE_SCHEMA = 2`. A record written by the old gate may hold a
wall; the getters ignore records whose schema does not match. Cache entries are
disposable, so invalidation costs nothing.

### Symmetry

- `providers.jina_extract` = Jina → TLS (`chrome`, `tor145`, `chrome116`) → patched Chromium → Internet Archive.
- `providers.brute_extract` = patched Chromium → Internet Archive.
- `extract --wayback` = Internet Archive only.
- A paywall found at Jina skips the two browser tiers (a server-side paywall
  cannot be lifted by JavaScript) and spends the budget on the archive.

---

## ⚡ TLS JA3/JA4 profile rotation, timeout units, and semantic DOM fallback (added 2026-10-24)

### 1. `Fetcher.get` timeout unit bug (`seconds` vs `milliseconds`)

- `StealthyFetcher.fetch` (Playwright/patchright) expects `timeout` in **milliseconds** (`int(timeout * 1000)`).
- `Fetcher.get` (`curl_cffi.requests.Session`) expects `timeout` in **seconds** and defaults to `retries=3`.
- Passing `timeout=int(timeout * 1000)` to `Fetcher.get` turned a 20 s budget into a 20,000 s (5.5 h) timeout × 3 retries, causing 71 s–122 s hangs on slow servers (`prabook.com`, `dezede.org`, `blouinartsalesindex.com`).
- Fixed in `_tls_fetch_html`: `timeout` is passed in integer seconds (`max(1, int(timeout_seconds))`) with `retries=1`.

### 2. TLS JA3/JA4 rotation (`chrome` → `tor145` → `chrome116`)

- Cloudflare JA3/JA4 bot rules on sites such as `gw.geneanet.org` and `en.geneastar.org` block `curl_cffi`'s default `chrome` (`chrome131`/`chrome150`) fingerprint with HTTP 403 (`Just a moment...`), forcing a 15 s browser launch that often fails Turnstile on datacenter IPs.
- `_tls_fetch_html(url, timeout_seconds)` rotates through `TLS_IMPERSONATE_PROFILES = ("chrome", "tor145", "chrome116")` via `curl_cffi` and loads any host cookies saved by `reverse-cli` (`~/.cache/reverse-cli/cookies/<host>.json`).
- Measured on `gw.geneanet.org` and `en.geneastar.org`: Tier 2 now resolves in **0.9–3.4 s** with HTTP 200 (96 KB HTML) without launching Chromium.

### 3. Semantic DOM container fallback (`extract_semantic_main`)

- `trafilatura.extract` is tuned for article prose and discards `<ul>/<li>/<div>` structured trees (genealogy fiches, directory cards, tables), keeping only footer boilerplate (e.g. the 643-char `"The owner of this family tree is a Premium member..."` footer on `gw.geneanet.org/pierfit` while dropping the entire family tree inside `<div id="perso">`).
- `_extract_html_markdown(html)` pairs `trafilatura.extract(..., favor_recall=True, include_tables=True, include_links=True)` with `extract_semantic_main(html)` (`BeautifulSoup` over `#perso, #fiche-content, article, main, [role='main'], #main-content, .main-content, #content` after decomposing nav/header/footer/cookie containers and stripping unrendered `{{ ... }}` templates).
- When the semantic container yields valid content that beats Trafilatura (or passes when Trafilatura fails the density floor), it is returned with `semantic_container=True` so structured short-line trees are not misclassified as navigation chrome.
- `_normalize_extract_url(url)` also strips `type=tree` on `*.geneanet.org` so GeneWeb serves the server-rendered HTML fiche instead of an empty JavaScript canvas (`gw_v5_graphtree`).
