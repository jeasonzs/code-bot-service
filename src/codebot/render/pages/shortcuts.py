"""Shortcuts page: app + keyboard shortcut reference."""

from __future__ import annotations

from typing import Optional
from dataclasses import dataclass

from PIL import ImageDraw

from ..canvas import Canvas
from ..theme import VSCodeDark, Color, SCREEN_W
from ..widgets import get_font
from .base import BasePage


@dataclass
class ShortcutItem:
    name: str
    keys: list[str]   # e.g. ["Cmd", "Shift", "P"]
    icon: str = "•"   # single-char icon


class ShortcutsPage(BasePage):
    title = "Shortcuts"

    def __init__(self, items: Optional[list[ShortcutItem]] = None) -> None:
        # Default items (macOS-flavored); user can override via YAML
        self.items = items or [
            ShortcutItem("VSCode",   ["⌘", "⇧", "P"], "📝"),
            ShortcutItem("Terminal", ["⌃", "`"],     "▸"),
            ShortcutItem("Browser",  ["⌘", "T"],     "⊕"),
            ShortcutItem("Slack",    ["⌘", "K"],     "✉"),
            ShortcutItem("Notes",    ["⌘", "N"],     "✎"),
        ]
        self._current_os = "mac"  # could be detected

    def render(self, canvas: Canvas) -> None:
        canvas.fill(VSCodeDark.BG)
        draw = ImageDraw.Draw(canvas.image)
        font_name = get_font("default", 11)
        font_key = get_font("mono", 11)

        y = 30
        for item in self.items:
            # icon + name
            draw.text((4, y), f"{item.icon} {item.name}", fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font_name)
            # keys (right-aligned)
            keys_str = "+".join(item.keys)
            bbox = draw.textbbox((0, 0), keys_str, font=font_key)
            tw = bbox[2] - bbox[0]
            draw.text((SCREEN_W - tw - 4, y), keys_str, fill=(VSCodeDark.WARNING.r, VSCodeDark.WARNING.g, VSCodeDark.WARNING.b), font=font_key)
            y += 24
