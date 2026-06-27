"""Common widgets for page renderers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .canvas import Canvas
from .theme import Color, VSCodeDark, SCREEN_W, INDICATOR_H, TITLE_H


# Font cache
_font_cache: dict[tuple[str, int], ImageFont.ImageFont] = {}


def get_font(name: str = "default", size: int = 12) -> ImageFont.ImageFont:
    """Get a font (with caching).

    Tries to load JetBrains Mono or Cascadia Code (monospace) for numbers.
    Falls back to default if not available.
    """
    key = (name, size)
    if key in _font_cache:
        return _font_cache[key]

    font = None
    if name == "mono":
        # Try common monospace fonts
        candidates = [
            "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/Library/Fonts/JetBrainsMono-Regular.ttf",  # macOS
            "C:\\Windows\\Fonts\\consola.ttf",  # Windows
        ]
        for path in candidates:
            if Path(path).exists():
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except OSError:
                    continue
    if font is None:
        font = ImageFont.load_default(size=size)
    _font_cache[key] = font
    return font


def draw_indicator(canvas: Canvas, current_index: int, total: int) -> None:
    """Draw the 7-segment page indicator at top.

    current_index: 0-based page index
    total: total number of pages
    """
    if total == 0: return
    seg_w = SCREEN_W // total
    for i in range(total):
        x = i * seg_w
        color = VSCodeDark.INDICATOR_ACTIVE if i == current_index else VSCodeDark.INDICATOR_BASE
        canvas.paste_rect(color, x, 0, seg_w - 1, INDICATOR_H)  # -1 for gap


def draw_title(canvas: Canvas, title: str) -> None:
    """Draw the page title below the indicator bar."""
    y = INDICATOR_H + 2
    font = get_font("default", 14)
    draw = ImageDraw.Draw(canvas.image)
    draw.text((4, y), title, fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font)


def draw_hint(canvas: Canvas, hint: str) -> None:
    """Draw hint text at the bottom of the screen."""
    y = 172 - 8
    font = get_font("default", 9)
    draw = ImageDraw.Draw(canvas.image)
    draw.text((4, y), hint, fill=(VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b), font=font)


def draw_progress_bar(
    canvas: Canvas,
    x: int, y: int, w: int, h: int,
    pct: float,
    label: str = "",
    value_text: str = "",
) -> None:
    """Draw a progress bar with optional label and value text."""
    canvas.progress_bar(x, y, w, h, pct, VSCodeDark.INFO, VSCodeDark.BG_PANEL)
    if label:
        font = get_font("mono", 11)
        draw = ImageDraw.Draw(canvas.image)
        draw.text((x, y - 12), label, fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font)
    if value_text:
        font = get_font("mono", 11)
        draw = ImageDraw.Draw(canvas.image)
        # right-align value text
        bbox = draw.textbbox((0, 0), value_text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((x + w - tw, y - 12), value_text, fill=(VSCodeDark.WARNING.r, VSCodeDark.WARNING.g, VSCodeDark.WARNING.b), font=font)
