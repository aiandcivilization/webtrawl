# Changelog

All notable changes to this project are documented here. Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning:
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-10-24

First public release, extracted from an internal toolbox and rewritten to stand
alone.

### Added

- `web` and `news` discovery across Brave, Tavily, Parallel and TinyFish, with
  Google News RSS as a keyless last resort and Tavily keyless access mode.
- `extract` through a four-tier cascade: Jina Reader → TLS fetch with JA3/JA4
  rotation → patched Chromium → Internet Archive, with the transport and elapsed
  time reported per attempt.
- Wall classification (`cookie wall`, `paywall`, `login wall`,
  `maintenance page`) and vendor-scoped consent dismissal.
- `--structured` facts from JSON-LD, microdata and OpenGraph, with an advisory
  amount fallback for pages that declare nothing.
- Disk cache with schema invalidation, and `--offset` / `--max-chars` pagination
  that never re-fetches.
- `image` download with referer repair, Zoomify tile stitching at native
  resolution and `--region` crops.
- `ocr` with a local Tesseract floor and a hedged vision chain; `[illisible]` on
  an unreadable word, never a guess.
- Document conversion (`.pdf`, `.docx`, `.xlsx`, `.csv`, …) inside `extract`.
- `providers`, `config set|show|check` and `agent-info` (a self-describing
  manifest for agent harnesses).
- Semantic exit codes `0`/`1`/`2`/`3`/`4`.

[0.1.0]: https://github.com/aiandcivilization/webtrawl/releases/tag/v0.1.0
