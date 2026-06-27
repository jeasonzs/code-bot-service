"""Quick Actions page: shutdown / reboot / logout buttons."""

from __future__ import annotations

from typing import Optional
from dataclasses import dataclass
import sys

from PIL import ImageDraw

from ..canvas import Canvas
from ..theme import VSCodeDark, SCREEN_W
from ..widgets import get_font
from .base import BasePage


@dataclass
class QuickAction:
    name: str
    icon: str
    command: str  # platform-specific command
    enabled: bool = True


class QuickActionsPage(BasePage):
    title = "Quick Actions"

    def __init__(self) -> None:
        # Platform-specific commands
        if sys.platform == "darwin":
            self.actions = [
                QuickAction("Power",  "⏻", "osascript -e 'tell app \"System Events\" to shut down'"),
                QuickAction("Reboot", "↻", "osascript -e 'tell app \"System Events\" to restart'"),
                QuickAction("Logout", "⏏", "osascript -e 'tell app \"System Events\" to log out'"),
            ]
        elif sys.platform == "win32":
            self.actions = [
                QuickAction("Power",  "⏻", "shutdown /s /t 0"),
                QuickAction("Reboot", "↻", "shutdown /r /t 0"),
                QuickAction("Logout", "⏏", "shutdown /l"),
            ]
        else:  # Linux
            self.actions = [
                QuickAction("Power",  "⏻", "systemctl poweroff"),
                QuickAction("Reboot", "↻", "systemctl reboot"),
                QuickAction("Logout", "⏏", "loginctl terminate-user $USER"),
            ]

    def render(self, canvas: Canvas) -> None:
        canvas.fill(VSCodeDark.BG)
        draw = ImageDraw.Draw(canvas.image)
        font_icon = get_font("default", 28)
        font_name = get_font("default", 12)
        font_warn = get_font("default", 9)

        # 3 large buttons
        button_w = (SCREEN_W - 16) // 3
        button_h = 90
        start_x = 6
        start_y = 30

        for i, action in enumerate(self.actions):
            x = start_x + i * (button_w + 2)
            y = start_y
            # Red background for danger
            canvas.paste_rect(VSCodeDark.DANGER, x, y, button_w, button_h)

            # Icon
            bbox = draw.textbbox((0, 0), action.icon, font=font_icon)
            iw = bbox[2] - bbox[0]
            draw.text((x + (button_w - iw) // 2, y + 12), action.icon, fill=(255, 255, 255), font=font_icon)

            # Name
            bbox = draw.textbbox((0, 0), action.name, font=font_name)
            nw = bbox[2] - bbox[0]
            draw.text((x + (button_w - nw) // 2, y + 56), action.name, fill=(255, 255, 255), font=font_name)

        # Warning text
        draw.text((4, 130), "Tap to execute. No confirmation.", fill=(VSCodeDark.WARNING.r, VSCodeDark.WARNING.g, VSCodeDark.WARNING.b), font=font_warn)

    def on_touch(self, event_type: int, x: int, y: int) -> Optional[str]:
        """Map touch to quick action (single click)."""
        button_w = (SCREEN_W - 16) // 3
        if 30 <= y <= 120:
            idx = x // (button_w + 2)
            if 0 <= idx < len(self.actions):
                return f"quick_action:{idx}"
        return None
