"""TileView: a single 2x2-grid dashboard tile.

A tile is a 160x72 (or configurable) region of the canvas with this layout:

    +-----------------------------+
    |  [icon]      <value> <unit> |
    |  [icon]                    |
    |                             |
    |       TITLE                 |
    |                             |
    |  ▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪▪  |   <- optional dotted bar
    +-----------------------------+

The icon is drawn at the top-left, the big value+unit right-aligned at
the top-right, the title centered horizontally below the icon, and a
dotted progress bar (drawn at the bottom, starting just past the icon's
right edge — never reaching below the icon) is rendered when `bar_pct`
is not None.

Used by SystemPage to render CPU / MEM / TEMP / FREQ tiles without
copy-paste.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..canvas import Canvas
from ..icons import draw_icon
from ..theme import Color, VSCodeDark
from ..widgets import (
    draw_dotted_bar,
    draw_text_centered,
    draw_value_with_unit,
    get_font,
)


# Default geometry constants (overridable per-instance). These match the
# SystemPage 2x2 layout on the 1.47" 320x172 LCD.
DEFAULT_ICON_SIZE = 40
DEFAULT_TITLE_FONT_SIZE = 11
DEFAULT_VALUE_FONT_SIZE = 36
DEFAULT_UNIT_FONT_SIZE = 12
DEFAULT_BAR_HEIGHT = 4
DEFAULT_BAR_SEGMENTS = 14
DEFAULT_BAR_GAP = 1
# Horizontal gap between the icon's right edge and the bar's left edge.
BAR_LEFT_GAP = 4
# Inner padding from the cell edges.
DEFAULT_PAD = 8
# Vertical offsets within a 72-tall cell.
ICON_TOP_OFFSET = 6
TITLE_TOP_OFFSET = 48
BAR_BOTTOM_OFFSET = 8  # bar is BAR_BOTTOM_OFFSET px above the cell bottom


@dataclass
class TileView:
    """Draws a single dashboard tile.

    All positional values are computed from the (x, y, w, h) cell rect
    plus the small offsets above; callers only need to set the cell
    rect and the tile's data.
    """

    # ---- Cell geometry ----
    x: int
    y: int
    w: int
    h: int

    # ---- Content ----
    icon: str = ""
    icon_color: Color = VSCodeDark.FG
    icon_color2: Optional[Color] = None  # only used by some icons (e.g. "net")

    title: str = ""
    title_color: Color = VSCodeDark.FG

    value_digits: str = ""      # big DSEG digits, e.g. "23" or "2.8"
    value_unit: str = ""        # small bold suffix, e.g. "%" / "GHz" / "°C" / ""
    value_color: Color = VSCodeDark.FG
    unit_color: Color = VSCodeDark.FG

    bar_pct: Optional[float] = None  # 0-100; None hides the bar
    bar_color: Color = VSCodeDark.FG
    bar_n_segments: int = DEFAULT_BAR_SEGMENTS

    # ---- Optional overrides (rare) ----
    icon_size: int = DEFAULT_ICON_SIZE
    value_font_size: int = DEFAULT_VALUE_FONT_SIZE
    unit_font_size: int = DEFAULT_UNIT_FONT_SIZE
    title_font_size: int = DEFAULT_TITLE_FONT_SIZE
    pad: int = DEFAULT_PAD
    bar_height: int = DEFAULT_BAR_HEIGHT

    def draw(self, canvas: Canvas) -> None:
        icon_x = self.x + self.pad
        icon_y = self.y + ICON_TOP_OFFSET
        if self.icon:
            draw_icon(
                canvas, self.icon, icon_x, icon_y, self.icon_color,
                size=self.icon_size, color2=self.icon_color2,
            )

        # Big value (right-aligned, with optional unit suffix).
        if self.value_digits:
            digits_font = get_font("digital", self.value_font_size)
            unit_font = get_font("bold", self.unit_font_size)
            x_right = self.x + self.w - self.pad
            # Cap digits width so the unit always has room (and we can fall
            # back to a smaller single-line render if not).
            max_digits_width = self.w - 2 * self.pad - self._unit_text_width()
            if max_digits_width < self.w // 3:
                max_digits_width = self.w // 3
            draw_value_with_unit(
                canvas,
                self.value_digits, self.value_unit,
                x_right, self.y + 10,
                digits_font, unit_font,
                self.value_color, self.unit_color,
                max_digits_width,
            )

        # Title centered horizontally below the icon.
        if self.title:
            draw_text_centered(
                canvas, self.title,
                icon_x + self.icon_size // 2,
                self.y + TITLE_TOP_OFFSET,
                get_font("bold", self.title_font_size),
                self.title_color,
            )

        # Optional dotted bar at the bottom, starting just past the icon's
        # right edge (so it never reaches below the icon).
        if self.bar_pct is not None:
            bar_x = icon_x + self.icon_size + BAR_LEFT_GAP
            bar_x_end = self.x + self.w - self.pad
            bar_w = bar_x_end - bar_x
            if bar_w > 0:
                draw_dotted_bar(
                    canvas,
                    bar_x, self.y + self.h - BAR_BOTTOM_OFFSET,
                    bar_w, self.bar_height,
                    self.bar_pct, fg=self.bar_color,
                    n_segments=self.bar_n_segments, gap=DEFAULT_BAR_GAP,
                )

    # ---- Internals ----

    def _unit_text_width(self) -> int:
        """Rough width budget reserved for the unit suffix (used to size
        the digits region). Cached lazily; not critical for correctness,
        the fallback in draw_value_with_unit catches overflow."""
        if not self.value_unit:
            return 0
        # ~7 px per character at unit_font_size 12 is a safe over-estimate.
        return max(7, self.unit_font_size) * len(self.value_unit) + 4
