"""Render docs/demo.gif from real command output.

The point of this file is that the demo cannot drift from the product: it runs
the CLI, keeps the output, and animates the real bytes. Re-run it after a change
to the output shape:

    uv run python docs/demo.py

Requires no extra dependency: Pillow is already a runtime dependency.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 900, 540
BG = (20, 20, 19)
INK = (250, 249, 245)
DIM = (138, 134, 124)
ACCENT = (196, 98, 60)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_SIZE = 15
LINE_HEIGHT = 21
LEFT, TOP = 22, 24
MAX_CHARS = 92  # fits the widest line at FONT_SIZE, with no clipped tail

COMMANDS = [
    ["webtrawl", "news", "central bank rates", "-n", "2", "--compact"],
    ["webtrawl", "extract", "https://docs.python.org/3/library/asyncio.html", "--max-chars", "200"],
]


def capture() -> list[list[str]]:
    """Run both commands and keep their stdout, split into display lines."""
    outputs: list[list[str]] = []
    for args in COMMANDS:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=180)
        lines: list[str] = []
        for raw in result.stdout.splitlines():
            while len(raw) > MAX_CHARS:
                lines.append(raw[:MAX_CHARS])
                raw = "  " + raw[MAX_CHARS:]
            lines.append(raw)
        outputs.append(lines)
    return outputs


def frame(lines: list[tuple[str, tuple[int, int, int]]], footer: str) -> Image.Image:
    """One still: the visible lines, then a dim footer line."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    small = ImageFont.truetype(FONT_PATH, 12)
    y = TOP
    for text, colour in lines:
        draw.text((LEFT, y), text, font=font, fill=colour)
        y += LINE_HEIGHT
    draw.text((LEFT, HEIGHT - 26), footer, font=small, fill=DIM)
    return img


def build() -> None:
    outputs = capture()
    footer = "no API key required  ·  MIT  ·  github.com/aiandcivilization/webtrawl"

    frames: list[Image.Image] = []
    durations: list[int] = []
    shown: list[tuple[str, tuple[int, int, int]]] = []

    for args, output in zip(COMMANDS, outputs, strict=True):
        shown = []  # a clear screen between commands, like a real session
        typed = ""
        # Type the command in chunks of three characters: fast, and small enough
        # a GIF encoder keeps the file under a megabyte.
        for i in range(0, len(" ".join(args)), 3):
            typed = " ".join(args)[: i + 3]
            line = [*shown, (f"$ {typed}", INK)]
            frames.append(frame(line, footer))
            durations.append(60)
        shown.append((f"$ {typed}", INK))
        frames.append(frame(shown, footer))
        durations.append(250)

        # Reveal the real output, two lines per frame.
        for i in range(0, len(output), 2):
            shown.extend((text, INK) for text in output[i : i + 2])
            visible = shown[-20:]
            frames.append(frame(visible, footer))
            durations.append(90)
        durations[-1] = 1800

    # Hold the last frame so the loop does not snap back.
    frames[-1].save(
        Path(__file__).with_name("demo.gif"),
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        colors=48,
    )
    size = Path(__file__).with_name("demo.gif").stat().st_size
    print(f"docs/demo.gif  {size / 1024:.0f} KB")


if __name__ == "__main__":
    if shutil.which("webtrawl") is None:
        raise SystemExit("install the console script first: uv run python docs/demo.py")
    build()
