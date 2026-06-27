"""GitHub page: notifications, review requests, assigned issues.

Uses `gh` CLI to fetch data. If gh not available or not logged in,
shows a placeholder.
"""

from __future__ import annotations

import json
import subprocess
import shutil
from typing import Optional

from PIL import ImageDraw

from ..canvas import Canvas
from ..theme import VSCodeDark, SCREEN_W
from ..widgets import get_font
from .base import BasePage


class GithubPage(BasePage):
    title = "GitHub"

    def __init__(self) -> None:
        self._stats = {
            "notifications": "—",
            "review_requests": "—",
            "issues": "—",
            "user": "—",
        }
        self._last_fetch = 0.0

    def _fetch(self) -> bool:
        """Try to fetch from gh CLI. Returns True on success."""
        if not shutil.which("gh"):
            return False
        try:
            # Get current user
            r = subprocess.run(["gh", "api", "user", "-q", ".login"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                self._stats["user"] = r.stdout.strip()
            # Notifications count
            r = subprocess.run(["gh", "api", "notifications", "-q", "length"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                self._stats["notifications"] = r.stdout.strip()
            # Review requests
            r = subprocess.run(
                ["gh", "search", "prs", "--review-requested=@me", "--state=open", "--json", "number", "-q", "length"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                self._stats["review_requests"] = r.stdout.strip()
            # Assigned issues
            r = subprocess.run(
                ["gh", "search", "issues", "--assignee=@me", "--state=open", "--json", "number", "-q", "length"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                self._stats["issues"] = r.stdout.strip()
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return False

    def render(self, canvas: Canvas) -> None:
        canvas.fill(VSCodeDark.BG)
        draw = ImageDraw.Draw(canvas.image)
        font_user = get_font("default", 11)
        font_label = get_font("default", 12)
        font_value = get_font("mono", 20)

        # User handle
        if self._stats["user"] != "—":
            draw.text((4, 28), f"@{self._stats['user']}", fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font_user)

        # Three stat rows
        labels = [
            ("Notifications", self._stats["notifications"], VSCodeDark.WARNING),
            ("Review requests", self._stats["review_requests"], VSCodeDark.SUCCESS),
            ("Assigned issues", self._stats["issues"], VSCodeDark.INFO),
        ]
        y = 50
        for label, val, color in labels:
            draw.text((4, y), label, fill=(VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b), font=font_label)
            # Right-align value
            bbox = draw.textbbox((0, 0), str(val), font=font_value)
            vw = bbox[2] - bbox[0]
            draw.text((SCREEN_W - vw - 4, y - 2), str(val), fill=(color.r, color.g, color.b), font=font_value)
            y += 32

    def refresh(self) -> None:
        """Fetch new data (call ~every 5 minutes)."""
        import time
        if time.time() - self._last_fetch < 60:  # limit rate
            return
        self._last_fetch = time.time()
        self._fetch()
