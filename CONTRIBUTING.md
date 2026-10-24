# Contributing

Thanks for improving `webtrawl`.

## Setup

```bash
git clone https://github.com/aiandcivilization/webtrawl.git
cd webtrawl
uv sync
uv run webtrawl --help
```

## Before opening a pull request

```bash
uv run ruff check .
uv run pytest -q      # offline: no network, no API key required
uv build
```

Keep one change per pull request. A new command or a changed output needs a test.
A change to the extraction gate needs the page it was measured on, in the pull
request description: the cascade is tuned by measurements, not by intuition.

## Rules that are not negotiable

- **stdout stays parseable.** Payload and `#`-prefixed hints only. Diagnostics go
  to stderr. No `--json` flag and no ANSI colour.
- **Exit codes are semantic** (`0` ok, `1` transient, `2` auth, `3` input,
  `4` rate limited). Do not add a new failure mode without a code.
- **A wall is never returned as content.** If you relax a gate, show the page it
  now accepts and why it is a page and not a paywall.
- **A transcription is never corrected.** OCR uses `[illisible]` for an
  unreadable word; guessing a name or a date fabricates a source.
- **Do not re-vendor `src/_vendor/` carelessly.** Read
  `src/_vendor/cli_common/VENDOR.md` first: the copy carries local edits.

Do not commit credentials, tokens or private endpoints.
