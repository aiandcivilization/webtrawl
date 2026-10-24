# Positioning: what the web tooling market asks for, and what this tool answers

Gathered from X/Twitter between February and October 2026, plus the measurements
in `docs/benchmark.md`. Each row is a complaint practitioners posted publicly,
the gap it exposes, and the decision it produced here. This is the evidence
behind the README's claims — not a competitor teardown.

| Observation (public post, date) | Gap | What `webtrawl` does |
|---|---|---|
| "What's the best open-source alternative to Tavily for AI agents? Looking for web search, crawling **and** content extraction tools." — @rhrits, 2026-10-23 | The market ships either search **or** extraction. An agent needs both, and the join is where it loses the source. | One binary, two stages, and every search row carries its own `extract` command in an `extract_cmd` column. |
| "AI agents browse the web like it's 2005 … a tiered channel dispatcher that picks the right fetch tool" — @chenzeling4, 2026-07 | A single fetch strategy always loses: a keyless proxy is blocked, a TLS client is fingerprinted, a browser is slow. | Four tiers with fixed escalation, the transport and elapsed seconds reported per attempt, and the Internet Archive after the browser. |
| An agent summarised a login-walled page and returned "garbage in, garbage out" — @josepha_ai_tool, 2026-05 | Native fetch tools return a wall **as content**, and the model cannot tell. | `detect_wall` classifies `cookie wall` / `paywall` / `login wall` / `maintenance page` and rejects or flags the text instead of returning it. |
| Tool output eats the context (a Playwright snapshot is ~56 KB; 20 issues ~59 KB; "40 % of your context is gone in 30 minutes") — @thisguyknowsai, 2026-10-20 | The unit of cost is tokens of *noise*, not tokens of information. | −85 % tokens against raw HTML on the benchmark page; `--max-chars` + `--offset` slices that never re-fetch; a `tokens`-shaped output (`TOON`) instead of verbose JSON. |
| "Brave: 2,000 free calls/month, card required. Tavily: 1,000, no card." — @0X_Vetra, 2026-02 | Free tiers differ, and a tool that hardcodes one provider dies with that provider's quota. | Provider-agnostic discovery with an explicit fallback chain and a keyless path that needs no account at all. |
| Hosted alternatives route every request through one vendor's servers — TinyFish launch thread, 2026-05 | Privacy and lock-in: the search query and the page both leave the machine. | Local-first: keys stay in `~/.config/webtrawl`, no proxy through a third party, and the only network calls are the ones the cascade needs. |
| Screenshot-based agents advertise a "3x faster" replacement — same thread | Speed claims with no per-page evidence. | Every extraction reports its tier and seconds, so a slow page can be attributed instead of hand-waved. |

## What this tool deliberately does not claim

- It is **not** a semantic/vector search engine. Exa-class retrieval finds pages a
  keyword index cannot; `webtrawl` does keyword discovery plus reliable
  extraction, and stops there.
- It is **not** a crawler. There is no `/map` endpoint and no site sweep: one URL
  per call, so a run cannot quietly consume a site.
- It does **not** defeat reputation-level bot managers (`amazon.fr`, `fnac.com`,
  `leboncoin.fr`) from a datacenter IP. That is documented as a limit, not sold
  as a feature.

## Where the edge actually is

1. **The gate.** Most fetch tools optimise for *getting bytes*. The expensive
   failure is *returning the wrong bytes confidently* — a cookie panel, a
   paywall, a Vercel checkpoint. Classifying that is the differentiator.
2. **The contract with the agent.** Semantic exit codes, a self-describing
   `agent-info` manifest, `next_cmd` on every paginated slice, and no JSON flag.
   A harness can drive the tool without a prompt that explains the tool.
3. **Zero-key operation.** A tool that needs a credit card before it prints a
   result does not survive the first five minutes of a trial.
