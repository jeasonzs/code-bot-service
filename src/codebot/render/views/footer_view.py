"""FooterView: a horizontal strip of icon+value items at the bottom of
the screen, with the `>_` prompt always pinned to the far left.

Items are passed as a list of dicts (or FooterItem dataclass instances)
of the form {icon, value, color}, and are laid out in the remaining
width after the prompt:

  • 1 item  → spans the rest of the row, anchored at the left.
  • 2 items → each takes half the remaining width, anchored at the
              left of its half.
  • N items (N≥3) → distributed across the row: the first is flush
              with the left edge of the remaining space, the last is
              right-aligned to the right edge, and the items in
              between are spaced evenly between them.

The `>_` prompt is always drawn at the far left, in INFO color, with
no value. Both the prompt and the items are vertically centered
within the row's height. A configurable left/right margin (default
8 px) keeps items from crowding the screen edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Union

from PIL import Image, ImageDraw

from ..canvas import Canvas
from ..icons import draw_icon
from ..theme import Color, VSCodeDark, SCREEN_W
from ..widgets import get_font


DEFAULT_ICON_SIZE = 16
DEFAULT_FONT_SIZE = 16
# Row height: SystemPage footer is 24 px tall (y=148..172). Vertical
# centering math uses this to place the icon and text on the row's
# optical center.
DEFAULT_ROW_H = 28
# Horizontal padding (left AND right) inside the content area. Keeps
# items from crowding the screen edges.
SIDE_MARGIN = 8
# Distance between the prompt's right edge and the start of the items
# area. Larger than SIDE_MARGIN so the prompt reads as a separate
# element from the data items.
PROMPT_GAP = 16
# Distance between an item's icon and its value text.
ITEM_GAP = 4


@dataclass
class FooterItem:
    """One item in the footer: a small icon + a text value + a color."""

    icon: str
    value: str
    color: Color

    @classmethod
    def from_dict(cls, d: dict) -> "FooterItem":
        """Convenience: accept {icon, value, color} dicts from callers."""
        return cls(icon=d["icon"], value=d["value"], color=d["color"])


# Public type: callers can pass either dicts or FooterItem instances.
FooterItemInput = Union[FooterItem, dict]


@dataclass
class FooterView:
    """Draws the footer row. The `>_` prompt is always shown at the
    far left; the remaining `items` are laid out in the space to the
    right of the prompt, respecting SIDE_MARGIN padding on both edges
    and vertically centering icon+text on the row's optical center."""

    y: int
    items: Sequence[FooterItemInput] = ()

    # Optional overrides (rare).
    icon_size: int = DEFAULT_ICON_SIZE
    font_size: int = DEFAULT_FONT_SIZE
    row_h: int = DEFAULT_ROW_H

    def draw(self, canvas: Canvas) -> None:
        # Vertical center of the row. Icons and text are aligned to
        # this y so the row reads as visually balanced.
        row_cy = self.y + self.row_h // 2

        # 1) `>_` prompt (always present, always at left margin)
        prompt_x = SIDE_MARGIN
        draw_icon(
            canvas, "terminal", prompt_x,
            row_cy - self.icon_size // 2,
            VSCodeDark.INFO, size=self.icon_size,
        )

        # 2) Layout items in the remaining width.
        if not self.items:
            return

        font = get_font("bold", self.font_size)
        d = ImageDraw.Draw(canvas.image)
        items = [self._coerce(it) for it in self.items]

        # Items content area: from the prompt's right edge (with a gap)
        # to the right screen margin.
        content_x = prompt_x + self.icon_size + PROMPT_GAP
        content_w = SCREEN_W - SIDE_MARGIN - content_x

        slots = self._compute_slots(len(items), content_x, content_w, items, font)

        # Compute the y position of the value text so its vertical
        # center aligns with row_cy. PIL's text(xy, ...) uses the
        # top-left of the text bbox as the anchor, so we offset by
        # half the text's measured height.
        text_h = self._text_height(d, "Mg", font)  # "Mg" approximates cap+descender

        for item, (x_start, _x_end) in zip(items, slots):
            ix = x_start
            iy = row_cy - self.icon_size // 2
            tx = x_start + self.icon_size + ITEM_GAP
            ty = row_cy - text_h // 2
            draw_icon(canvas, item.icon, ix, iy, item.color,
                      size=self.icon_size)
            d.text(
                (tx, ty),
                item.value,
                fill=(item.color.r, item.color.g, item.color.b),
                font=font,
            )

    # ---- Layout ----

    def _compute_slots(self, n: int, x: int, w: int, items: list, font) -> list[tuple[int, int]]:
        """Return [(x_start, x_end), ...] for each of n items, where
        x_start is the leading edge of the item (where its icon goes)
        and x_end is the leading edge of the next item (or the right
        edge of the content area for the last item).

        - n == 1: one item, anchored at the left of the remaining
                  width.
        - n == 2: two equal slots, each ½ of the remaining width; both
                  items render left-aligned in their half.
        - n >= 3:  the first (n-1) items are placed at equal intervals
                    starting at the left edge; the last item is
                    right-aligned to the right edge of the content
                    area so it isn't pushed off-screen. The gap
                    between items is computed so the (n-1) gaps
                    between the first and the last are all equal.
        """
        if n == 1:
            return [(x, x + w)]
        if n == 2:
            half = w // 2
            return [(x, x + half), (x + half, x + w)]
        # n >= 3: right-align the last item; place the rest at equal
        # intervals to its left so all gaps are uniform.
        last_w = self._item_width(items[-1], font)
        last_x = x + w - last_w
        gap = (last_x - x) // (n - 1)
        return [(x + i * gap, x + (i + 1) * gap) for i in range(n - 1)] + \
               [(last_x, last_x + last_w)]

    # ---- Helpers ----

    @staticmethod
    def _coerce(it: FooterItemInput) -> FooterItem:
        if isinstance(it, FooterItem):
            return it
        return FooterItem.from_dict(it)

    def _item_width(self, item: "FooterItem", font) -> int:
        """Width of one rendered item: icon + ITEM_GAP + text."""
        dummy = Image.new("RGB", (1, 1))
        d = ImageDraw.Draw(dummy)
        text_w = self._text_width(d, item.value, font)
        return self.icon_size + ITEM_GAP + text_w

    @staticmethod
    def _text_width(d: ImageDraw.ImageDraw, s: str, font) -> int:
        if not s:
            return 0
        bbox = d.textbbox((0, 0), s, font=font)
        return bbox[2] - bbox[0]

    @staticmethod
    def _text_height(d: ImageDraw.ImageDraw, s: str, font) -> int:
        if not s:
            return 0
        bbox = d.textbbox((0, 0), s, font=font)
        return bbox[3] - bbox[1]
