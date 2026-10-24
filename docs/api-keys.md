# API keys: what each one buys, and how to get it

`webtrawl` runs with zero keys. Keys raise rate limits, add search providers and
unlock better OCR. Read `webtrawl providers` before anything else: it reports
each provider, its readiness and **where the key was found** — never the key.

```console
$ webtrawl providers
columns[5]: provider,kind,modes,configured,note
rows[12,]{provider,kind,modes,configured,note}:
  brave,method,"web,news",no,independent index; free tier now needs a card for NEW keys
  tavily,method,"web,news",keyless ok,1k credits/mo free no card; keyless mode works with no account
  ...
```

## Summary

| Provider | Env var | Unlocks | Free tier | Dashboard |
|---|---|---|---|---|
| Brave Search | `BRAVE_API_KEY` | Independent 30B-page index, web + news | 2,000 queries/month; card required for new keys; 1 req/s | <https://api-dashboard.search.brave.com/app/keys> |
| Tavily | `TAVILY_API_KEY` | Agent-optimised discovery, web + news | 1,000 credits/month, no card; keyless mode also exists | <https://app.tavily.com/home> |
| Parallel | `PARALLEL_API_KEY` | Neural search, deep domain coverage | Free credit grant (~5k requests/month), $1/1k after | <https://platform.parallel.ai> |
| TinyFish | `TINYFISH_API_KEY` | Live-rendered search | Free at any wallet balance; key required; 30 req/min | <https://agent.tinyfish.ai/api-keys> |
| Gemini (OCR) | `GEMINI_API_KEY` | Handwriting and degraded print | Free tier, requests-per-day bound | <https://aistudio.google.com/apikey> |
| VLM via OpenRouter | `VLM_API_KEY` | Any OpenAI-compatible vision model | Free models cost $0 | <https://openrouter.ai/keys> |
| Mistral OCR | `MISTRAL_API_KEY` | Document OCR, bounding boxes, confidence | No free API tier: $4 per 1,000 pages | <https://console.mistral.ai/api-keys> |
| None | — | Jina Reader, Google News RSS, Internet Archive, Tesseract | Always free, rate-capped | — |

## Procedures

### Brave Search — the highest-quality snippets

1. Open <https://api-dashboard.search.brave.com/app/keys>.
2. Create an account, then choose the **Free** plan. It includes 2,000 queries
   per month across Search and Answers.
3. New keys require a payment method on file. The plan stays at $0 inside the
   quota; usage above it is billed, so keep an eye on the dashboard if you run a
   loop.
4. Create a key, then either:

```bash
webtrawl config set brave BSA...      # written to ~/.config/webtrawl/config.toml, 0600
export BRAVE_API_KEY=BSA...           # or export it in the shell
```

Free tier limits you to 1 request/second. The CLI maps an HTTP 401 to exit `2`
and an HTTP 429 to exit `4`.

### Tavily — zero-friction, and keyless if you want

1. Open <https://app.tavily.com/home> and sign up. No card.
2. The dashboard shows `tvly-…` keys under **API Keys**.
3. `webtrawl config set tavily tvly-...`

You can skip this entirely: `webtrawl web "query" -m tavily` works in keyless
access mode on a shared, rate-capped pool. The CLI prints `mode: keyless` in the
header so you always know which mode answered.

### Parallel — deepest coverage

1. Create an account at <https://platform.parallel.ai>.
2. Copy a key from **API keys**.
3. `webtrawl config set parallel YOUR_KEY`.

Parallel has no keyless mode: requesting `-m parallel` without a key exits `2`
with a message pointing at the dashboard.

### TinyFish — free at a $0 balance

1. Sign in at <https://agent.tinyfish.ai/api-keys>.
2. Generate a key; no payment method is required, and search stays free at any
   wallet balance.
3. `webtrawl config set tinyfish YOUR_KEY`.

Rate limit is 30 requests/minute. Requesting it explicitly without a key exits
`2`.

### Google AI Studio — handwriting OCR

1. Open <https://aistudio.google.com/apikey> with a Google account.
2. **Create API key** and pick (or create) a project.
3. `webtrawl config set gemini AIza...`, or `export GEMINI_API_KEY=...`.

The free tier is requests-per-day bound and Google restricts it in the EU, UK
and Switzerland for products with users. For a product, use the paid tier or the
VLM path below. `webtrawl ocr page.jpg -p gemini` is a promise: if that provider
fails, the command exits `1` instead of degrading silently.

### OpenRouter VLM — free OCR, any host

1. Create a key at <https://openrouter.ai/keys>.
2. `webtrawl config set vlm sk-or-...`
3. Optionally point it at another OpenAI-compatible host:

```bash
export VLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # Zhipu
export VLM_MODEL=glm-4v-flash                              # documented free model
```

Measured on an 18th-century register: the free model read the act correctly at
$0, in 23.8 s, against 1.1 s for Gemini. Free costs latency, not accuracy.

### Mistral OCR — documents, not snapshots

1. <https://console.mistral.ai/api-keys>, create a key.
2. `webtrawl config set mistral YOUR_KEY`.
3. `webtrawl ocr page.pdf -p mistral` (or a picture).

Billed at $4 per 1,000 pages ($0.40 batched). It is opt-in on purpose: there is
no documented free API tier, and the free *chat* plan is not API access.

## Resolution order and storage

For every key, the first hit wins:

1. environment variable (`BRAVE_API_KEY`, `TAVILY_API_KEY`, …);
2. `~/.config/webtrawl/config.toml` under `[keys]`;
3. for OCR only, `~/.config/webtrawl/ocr.toml` under `[keys]`.

`webtrawl config set` writes the file with mode `0600` and rewrites it in place.
`webtrawl config show` prints a masked view. No command ever prints a key,
including `providers` and `agent-info` — useful when you paste their output into
an issue.

## Rotation and leaks

- Revoke at the provider dashboard, then `webtrawl config set <provider> NEW`.
- This repository ships no credential and `.env*` is gitignored.
- If a key was ever exported into a shell history, rotate it: a `0600` config
  file is not protection against a leaked scrollback.

## Cost control

- Start with keys that are free at the margin: Tavily (or keyless), Gemini free
  tier, Tesseract.
- Brave bills above 2,000 queries/month. Put a coarse guard in your loop: count
  calls, stop at a budget.
- Mistral is the only provider here with a per-page price. Keep it for documents
  where bounding boxes matter.
- Extraction tiers are free. Jina Reader, the TLS tier, the browser tier and the
  archive cost bandwidth and time, never money.
