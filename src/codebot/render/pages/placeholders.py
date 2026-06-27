"""Placeholder pages: claude, openclaw, hermes (v1 not implemented)."""

from __future__ import annotations

from PIL import ImageDraw

from ..canvas import Canvas
from ..theme import VSCodeDark
from ..widgets import get_font
from .base import BasePage


def _render_placeholder(canvas: Canvas, name: str, message: str) -> None:
    """Render a generic 'not implemented' placeholder."""
    canvas.fill(VSCodeDark.BG)
    draw = ImageDraw.Draw(canvas.image)
    font_big = get_font("default", 18)
    font_small = get_font("default", 11)

    # Centered title
    bbox = draw.textbbox((0, 0), name, font=font_big)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((320 - tw) // 2, 50), name, fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font_big)

    # Underline
    draw.rectangle([(40, 90), (280, 91)], fill=(VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b))

    # Message
    bbox = draw.textbbox((0, 0), message, font=font_small)
    tw = bbox[2] - bbox[0]
    draw.text(((320 - tw) // 2, 110), message, fill=(VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b), font=font_small)

    # Hint
    hint = "v1.0 placeholder"
    bbox = draw.textbbox((0, 0), hint, font=font_small)
    tw = bbox[2] - bbox[0]
    draw.text(((320 - tw) // 2, 140), hint, fill=(VSCodeDark.FG_DISABLED.r, VSCodeDark.FG_DISABLED.g, VSCodeDark.FG_DISABLED.b), font=font_small)


class ClaudePage(BasePage):
    title = "Claude"

    def render(self, canvas: Canvas) -> None:
        _render_placeholder(canvas, "Claude Code", "Not implemented in v1")


class OpenclawPage(BasePage):
    title = "OpenClaw"

    def render(self, canvas: Canvas) -> None:
        _render_placeholder(canvas, "OpenClaw", "Not implemented in v1")


class HermesPage(BasePage):
    title = "Hermes"

    def render(self, canvas: Canvas) -> None:
        _render_placeholder(canvas, "Hermes", "Not implemented in v1")
