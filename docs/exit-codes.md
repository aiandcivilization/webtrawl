# Exit codes

`webtrawl` never hides a failure behind exit 0, and never prints a diagnostic on
stdout. An agent can branch on the code alone:

| Code | Meaning | What the agent should do |
|---|---|---|
| `0` | Success. | Use the payload. |
| `1` | Transient failure: network error, timeout, extraction still incomplete after every tier. | Retry later, or try `--brute` / `--wayback`. |
| `2` | Authentication missing: a provider was requested explicitly and no key resolves. | Ask the operator for a key, or drop `-m`. |
| `3` | Bad input: unknown provider, invalid URL, `--region` outside the picture, empty query. | Fix the arguments. Do not retry. |
| `4` | Rate limited by the provider. | Back off, or switch `-m`. |

## Shell recipes

```bash
# stop a research loop on a missing key instead of looping
webtrawl web "query" -m brave || case $? in
  2) echo "no brave key: falling back"; webtrawl web "query" -m tavily ;;
  4) sleep 30; webtrawl web "query" -m tavily ;;
  *) exit 1 ;;
esac

# extract, and accept an incomplete page as a known partial
webtrawl extract "$URL" --max-chars 3000 || echo "extract failed: $?"

# preflight a machine before a long run
webtrawl providers
webtrawl config check
```

## Standard error, standard output

- **stdout**: the payload (TOON), then an agent hint block whose lines all start
  with `#`. It is byte-clean: no wrapping at 80 columns, no ANSI colour, no
  progress bar. A parser can skip `#` lines; a reader should not, they carry the
  "now run `extract`" instruction.
- **stderr**: warnings and diagnostics only. Nothing on stderr is part of the
  contract.

`webtrawl agent-info` returns the same table as TOON, so a harness can read the
codes at runtime rather than hardcoding them.
