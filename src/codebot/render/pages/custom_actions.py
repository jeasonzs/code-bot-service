"""Custom Actions page: 3x2 grid of action buttons (multi-page)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Callable

from PIL import ImageDraw

from ..canvas import Canvas
from ..theme import VSCodeDark, Color, SCREEN_W, SCREEN_H
from ..widgets import get_font
from .base import BasePage


@dataclass
class Action:
    name: str            # Display name (short, e.g. "git status")
    icon: str            # Icon text/emoji
    action_type: str     # "command", "open_app", "hid_keystrokes"
    config: dict         # Action-specific config (command, app, text, etc.)


class CustomActionsPage(BasePage):
    """3x2 grid of action icons. Long-press to trigger."""

    title = "Custom Actions"
    ITEMS_PER_PAGE = 6

    def __init__(self, actions: list[Action]) -> None:
        self.actions = actions or [
            Action("git status", "≡", "command", {"command": "git status"}),
            Action("git log",    "⊜", "command", {"command": "git log --oneline -5"}),
            Action("git push",   "↑", "command", {"command": "git push"}),
            Action("git pull",   "↓", "command", {"command": "git pull"}),
            Action("VSCode",     "{}", "open_app", {"app": "Visual Studio Code"}),
            Action("Terminal",   "▸", "open_app", {"app": "Terminal"}),
        ]

    def num_subpages(self) -> int:
        """Number of sub-pages needed."""
        return max(1, (len(self.actions) + self.ITEMS_PER_PAGE - 1) // self.ITEMS_PER_PAGE)

    def _render_button(
        self, canvas: Canvas,
        x: int, y: int, w: int, h: int,
        action: Action,
    ) -> None:
        """Draw a single action button."""
        # Background
        canvas.paste_rect(VSCodeDark.BG_PANEL, x, y, w, h)
        # Border
        canvas.paste_rect(VSCodeDark.BORDER, x, y + h - 1, w, 1)
        canvas.paste_rect(VSCodeDark.BORDER, x, y, 1, h)
        canvas.paste_rect(VSCodeDark.BORDER, x + w - 1, y, 1, h)
        canvas.paste_rect(VSCodeDark.BORDER, x, y, w, 1)

        # Icon (large, centered horizontally, top)
        draw = ImageDraw.Draw(canvas.image)
        font_icon = get_font("default", 20)
        icon = action.icon or "•"
        bbox = draw.textbbox((0, 0), icon, font=font_icon)
        iw = bbox[2] - bbox[0]
        draw.text((x + (w - iw) // 2, y + 6), icon, fill=(VSCodeDark.INFO.r, VSCodeDark.INFO.g, VSCodeDark.INFO.b), font=font_icon)

        # Name (small, bottom)
        font_name = get_font("mono", 10)
        name = action.name[:8]  # truncate
        bbox = draw.textbbox((0, 0), name, font=font_name)
        nw = bbox[2] - bbox[0]
        draw.text((x + (w - nw) // 2, y + h - 14), name, fill=(VSCodeDark.WARNING.r, VSCodeDark.WARNING.g, VSCodeDark.WARNING.b), font=font_name)

    def render(self, canvas: Canvas, subpage: int = 0) -> None:
        canvas.fill(VSCodeDark.BG)

        # 3x2 grid
        cols, rows = 3, 2
        button_w = (SCREEN_W - 8) // cols
        button_h = 50
        start_x = 4
        start_y = 30

        start_idx = subpage * self.ITEMS_PER_PAGE
        end_idx = min(start_idx + self.ITEMS_PER_PAGE, len(self.actions))

        for i, idx in enumerate(range(start_idx, end_idx)):
            row = i // cols
            col = i % cols
            x = start_x + col * button_w
            y = start_y + row * (button_h + 4)
            self._render_button(canvas, x, y, button_w - 2, button_h, self.actions[idx])

    def on_touch(self, event_type: int, x: int, y: int, subpage: int = 0) -> Optional[str]:
        """Return action_id like 'action:0' if long-press hits a button."""
        # Compute which button (col, row) at (x, y) and return its action index
        cols = 3
        button_w = (SCREEN_W - 8) // cols
        button_h = 50
        start_x = 4
        start_y = 30
        if x < start_x or y < start_y:
            return None
        col = (x - start_x) // button_w
        row = (y - start_y) // (button_h + 4)
        idx = row * cols + col
        action_idx = subpage * self.ITEMS_PER_PAGE + idx
        if action_idx < len(self.actions):
            return f"action:{action_idx}"
        return None
