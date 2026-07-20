"""Shortcuts page: app + keyboard shortcut reference (platform-aware).

Items store **semantic modifier names** (``"cmd"``, ``"shift"``, ``"ctrl"``,
``"alt"``) so the same data renders correctly on every platform:

  - macOS:   ``cmd``  -> "⌘",   ``shift`` -> "⇧",   ``ctrl`` -> "⌃",   ``alt`` -> "⌥"
  - Windows: ``cmd``  -> "Ctrl", ``shift`` -> "Shift", ``ctrl`` -> "Ctrl", ``alt`` -> "Alt"
  - Linux:   ``cmd``  -> "Ctrl", ``shift`` -> "Shift", ``ctrl`` -> "Ctrl", ``alt`` -> "Alt"

Users on Win/Linux see ASCII keys (avoids the "?" boxes their default
fonts sometimes show for ⌘/⇧/⌃); macOS users see the proper glyphs.
The semantic form keeps YAML config files portable.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

from PIL import ImageDraw

from ..canvas import Canvas
from ..theme import VSCodeDark, SCREEN_W
from ..widgets import get_font
from .base import BasePage


# Modifier -> glyph per platform. ``cmd`` is the "primary" modifier —
# Ctrl on Win/Linux, Cmd (⌘) on macOS.
_MOD_GLYPHS = {
    "mac":   {"cmd": "⌘", "shift": "⇧", "ctrl": "⌃", "alt": "⌥"},
    "win":   {"cmd": "Ctrl", "shift": "Shift", "ctrl": "Ctrl", "alt": "Alt"},
    "linux": {"cmd": "Ctrl", "shift": "Shift", "ctrl": "Ctrl", "alt": "Alt"},
}


def _current_platform_key() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform == "win32":
        return "win"
    return "linux"


@dataclass
class ShortcutItem:
    name: str
    keys: list[str]   # semantic modifier names: "cmd" / "shift" / "ctrl" / "alt", or literal keys
    icon: str = "•"


class ShortcutsPage(BasePage):
    title = "Shortcuts"

    def __init__(self, items: Optional[list[ShortcutItem]] = None) -> None:
        # Default items use semantic modifiers (P4.5).
        # Note: VSCode Command Palette is shown as ``Cmd+Shift+P`` everywhere;
        # on Win/Linux that's Ctrl+Shift+P — same shortcut, different glyph.
        self.items = items or [
            ShortcutItem("VSCode",   ["cmd", "shift", "P"], "📝"),
            ShortcutItem("Terminal", ["ctrl", "`"],          "▸"),
            ShortcutItem("Browser",  ["cmd", "T"],          "⊕"),
            ShortcutItem("Slack",    ["cmd", "K"],          "✉"),
            ShortcutItem("Notes",    ["cmd", "N"],          "✎"),
        ]
        self._os_key = _current_platform_key()

    def _render_keys(self, semantic_keys: list[str]) -> str:
        glyphs = _MOD_GLYPHS[self._os_key]
        parts = [glyphs.get(k.lower(), k) for k in semantic_keys]
        return "+".join(parts)

    def render(self, canvas: Canvas) -> None:
        canvas.fill(VSCodeDark.BG)
        draw = ImageDraw.Draw(canvas.image)
        font_name = get_font("default", 11)
        font_key = get_font("mono", 11)

        y = 30
        for item in self.items:
            # icon + name
            draw.text((4, y), f"{item.icon} {item.name}",
                      fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b),
                      font=font_name)
            # keys (right-aligned)
            keys_str = self._render_keys(item.keys)
            bbox = draw.textbbox((0, 0), keys_str, font=font_key)
            tw = bbox[2] - bbox[0]
            draw.text((SCREEN_W - tw - 4, y), keys_str,
                      fill=(VSCodeDark.WARNING.r, VSCodeDark.WARNING.g, VSCodeDark.WARNING.b),
                      font=font_key)
            y += 24
