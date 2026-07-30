"""WarningBannerView: a centered overlay panel used to surface error
states on a page without abandoning the surrounding layout.

Use case: the GitHub page wants to keep its 2x2 tile grid visible
behind the banner (so you can see *what* data is missing) while also
calling attention to "token missing / expired". Drawing the banner as
an overlay (rather than replacing the page) gives the user enough
context to diagnose without losing orientation.

Visual:

    +------------------------------------------------+
    |                                                |
    |      [icon]   TOKEN NOT SET                    |   <- title (bold)
    |                Set GITHUB_TOKEN env or         |   <- hint  (smaller)
    |                pages.github.token in config.yml|
    |                                                |
    +------------------------------------------------+

The banner is drawn last (on top of the tiles). It does not currently
dim the background — the device LCD is small enough that a dimming
fill would eat too much of the tile real estate and not add much on a
160 ppi screen. If we move to a higher-resolution panel this can be
added in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PIL import ImageDraw

from ..canvas import Canvas
from ..icons import draw_icon
from ..theme import Color, VSCodeDark, SCREEN_W, SCREEN_H
from ..widgets import get_font


# Default geometry. The banner is centered horizontally on the screen
# and roughly centered vertically (slightly above center so it doesn't
# overlap the footer row).
DEFAULT_W = 280
DEFAULT_H = 64
SIDE_PAD = (SCREEN_W - DEFAULT_W) // 2     # 20
# Vertical center of the banner (slightly above the screen center so
# the bottom edge clears the y=148 footer line with a small gap).
TOP_Y = (SCREEN_H - DEFAULT_H) // 2 - 4    # (172-64)/2 - 4 = 50

# Inner padding inside the panel.
INNER_PAD_X = 12
INNER_PAD_Y = 8
ICON_SIZE = 28
ICON_TEXT_GAP = 12
# Vertical gap between the title and the hint line.
TITLE_HINT_GAP = 4

DEFAULT_TITLE_FONT_SIZE = 14
DEFAULT_HINT_FONT_SIZE = 10


@dataclass
class WarningBannerView:
    """Draw a centered warning panel.

    All fields are optional except ``title``; without a title the
    banner draws nothing (callers can use the presence/absence of a
    title as the visibility gate).

    The panel border + background are drawn in a slightly lighter shade
    than the page BG so it reads as "raised", not "filled".
    """

    title: str = ""
    hint: Optional[str] = None  # optional second line (smaller text)
    # Accent color for the border + icon. Default WARNING yellow, but
    # callers can swap to DANGER (red) for "bad credentials" etc.
    accent: Color = VSCodeDark.WARNING

    # Optional overrides.
    icon: str = "status"  # bitmap icon kind (icons.py registry)
    w: int = DEFAULT_W
    h: int = DEFAULT_H
    title_font_size: int = DEFAULT_TITLE_FONT_SIZE
    hint_font_size: int = DEFAULT_HINT_FONT_SIZE
    bg_color: Color = VSCodeDark.BG_PANEL

    def draw(self, canvas: Canvas) -> None:
        if not self.title:
            return

        x = SIDE_PAD
        y = TOP_Y
        d = ImageDraw.Draw(canvas.image)

        # 1) Background panel.
        d.rectangle(
            [(x, y), (x + self.w - 1, y + self.h - 1)],
            fill=(self.bg_color.r, self.bg_color.g, self.bg_color.b),
            outline=(self.accent.r, self.accent.g, self.accent.b),
            width=2,
        )

        # 2) Icon on the left.
        ix = x + INNER_PAD_X
        iy = y + (self.h - ICON_SIZE) // 2
        # draw_icon tint: bitmap icons ignore `color`, so we just draw
        # at default size; the accent border above carries the color.
        draw_icon(canvas, self.icon, ix, iy, self.accent, size=ICON_SIZE)

        # 3) Title text (left-aligned, just right of the icon).
        text_x = ix + ICON_SIZE + ICON_TEXT_GAP
        title_font = get_font("bold", self.title_font_size)
        title_color = self.accent  # title picks up the accent color
        text_y = y + INNER_PAD_Y
        d.text(
            (text_x, text_y),
            self.title,
            fill=(title_color.r, title_color.g, title_color.b),
            font=title_font,
        )

        # 4) Optional hint line below the title.
        if self.hint:
            hint_font = get_font("mono", self.hint_font_size)
            # Estimate title height to place hint below it. textbbox
            # is overkill here; the line height of the title font is
            # a good enough approximation and avoids an extra call.
            hint_y = text_y + self.title_font_size + TITLE_HINT_GAP
            # Clip hint to panel width — text that overruns gets ugly.
            hint_clipped = self._clip_to_width(
                d, self.hint, hint_font,
                max_w=self.w - (text_x - x) - INNER_PAD_X,
            )
            d.text(
                (text_x, hint_y),
                hint_clipped,
                fill=(VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b),
                font=hint_font,
            )

    # ---- Helpers ----

    @staticmethod
    def _clip_to_width(d: ImageDraw.ImageDraw, s: str, font,
                       max_w: int) -> str:
        """Trim ``s`` with a trailing ellipsis if it exceeds ``max_w``.

        Conservative: walks backward from the end of the string and
        stops at the longest prefix that fits (plus "…"). Returns ``s``
        unchanged if it already fits.
        """
        if not s:
            return ""
        bbox = d.textbbox((0, 0), s, font=font)
        if bbox[2] - bbox[0] <= max_w:
            return s
        ellipsis = "…"
        # Binary-ish: trim 4 chars at a time until it fits with the
        # ellipsis, then walk back to the longest that fits. Cheap and
        # good enough for short hint strings.
        trimmed = s
        for cut in range(4, len(s)):
            candidate = s[:-cut] + ellipsis
            bb = d.textbbox((0, 0), candidate, font=font)
            if bb[2] - bb[0] <= max_w:
                return candidate
        # Pathological: even just the ellipsis doesn't fit. Return it
        # anyway; the panel border will clip visually.
        return ellipsis