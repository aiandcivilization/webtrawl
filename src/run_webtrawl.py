"""Console entrypoint for the webtrawl CLI (see :mod:`webtrawl.cli`)."""

from __future__ import annotations

from webtrawl.cli import app


def main() -> None:
    """Run the CLI. Typer owns argument parsing, exit codes and help."""
    app()


if __name__ == "__main__":
    main()
