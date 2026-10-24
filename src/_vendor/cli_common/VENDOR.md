# Vendored code

`src/_vendor/cli_common/` is a subset of an internal `cli_common` library, copied
so this repository installs with no private dependency. MIT, same licence as this
project.

- Origin: internal `cli_common` package, commit `7d48ef22`
- Copied modules: `toon_output` (TOON encoder), `ocr` (OCR engine) and
  `ocr_models.toml` (model registry)
- Not copied: the argparse helpers, `toon_help`, `tool_descriptions` and the rest
  of the internal registry — a public tool does not need to publish its siblings.

## Local edits applied to the verbatim copy

| File | Edit | Why |
|---|---|---|
| `ocr.py` | `resources.files("cli_common")` → `resources.files(__package__)` | The copy is not importable as `cli_common`. |
| `ocr.py` | fleet config path → `~/.config/webtrawl/ocr.toml`; cache → `~/.cache/webtrawl/ocr`; env vars renamed `WEBTRAWL_OCR_*` | A wheel upgrade must not read or write another product's directory. |
| `ocr.py` | dropped the `speech-cli` legacy config path | Not this tool's business. |
| `ocr.py` | docstring rewritten | It described the internal fleet. |
| `ocr_models.toml` | config-path comment updated | Same reason. |

Do not re-vendor without repeating these edits. If you change the copy, edit it
here and record the change in this file.
