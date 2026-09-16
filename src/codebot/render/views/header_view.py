"""HeaderView: the top row shared by SystemPage / GithubPage / ClaudePage.

Layout:

    y=0..20   >_  <title>               <subtitle>

The `>_` terminal icon is always pinned at the left (INFO blue). The
title is left-aligned next to the icon; the subtitle is right-aligned
to the right edge. If the title would collide with the subtitle it is
truncated with an ellipsis.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import ImageDraw

from ..canvas import Canvas
from ..icons import draw_icon
from ..theme import Color, HEADER_H, SCREEN_W, VSCodeDark
from ..widgets import get_font


# Icon-to-title horizontal gap. Larger than SIDE_MARGIN so the prompt
# reads as a separate element from the title.
_SIDE_MARGIN = 8
_PROMPT_GAP = 16
_ICON_SIZE = 16
_TITLE_FONT_SIZE = 16
_SUBTITLE_FONT_SIZE = 12
# Minimum pixels between the title's right edge and the subtitle's left
# edge. Below this we start truncating the title.
_TITLE_SUBTITLE_GAP = 4


def _truncate_to_width(
    text: str, font, max_w: int, draw: ImageDraw.ImageDraw
) -> str:
    """Shorten ``text`` so its rendered width is ≤ ``max_w``.

    Truncates by character with a trailing ellipsis. Falls back to "…"
    alone when even one char plus ellipsis doesn't fit.
    """
    if not text:
        return ""
    bbox = draw.textbbox((0, 0), text, font=font)
    if bbox[2] - bbox[0] <= max_w:
        return text
    # Reserve 1 char for the ellipsis. If even that doesn't fit,
    # return a single ellipsis.
    while len(text) > 1:
        text = text[:-1]
        candidate = text + "…"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_w:
            return candidate
    return "…"


@dataclass
class HeaderView:
    """Draw the shared top header row.

    ``title`` is the per-page display name (left). ``subtitle`` is an
    optional status string rendered right-aligned (right). ``subtitle_color``
    defaults to FG_DIM; pages that want state-based coloring pass a color
    per-render (e.g. claude status, github event type).
    """

    title: str
    subtitle: str = ""
    subtitle_color: Color = VSCodeDark.FG_DIM

    def draw(self, canvas: Canvas) -> None:
        row_cy = HEADER_H // 2

        # 1) `>_` terminal icon at the left margin.
        draw_icon(
            canvas, "terminal", _SIDE_MARGIN,
            row_cy - _ICON_SIZE // 2,
            VSCodeDark.INFO, size=_ICON_SIZE,
        )

        # 2) Title left-aligned after the icon + gap.
        title_font = get_font("bold", _TITLE_FONT_SIZE)
        subtitle_font = get_font("mono", _SUBTITLE_FONT_SIZE)
        draw = ImageDraw.Draw(canvas.image)

        x_title = _SIDE_MARGIN + _ICON_SIZE + _PROMPT_GAP  # = 40
        x_right = SCREEN_W - _SIDE_MARGIN  # = 312

        # 3) Subtitle right-aligned. Measure first so we can truncate
        #    the title if it would collide with the subtitle.
        if self.subtitle:
            sub_bbox = draw.textbbox((0, 0), self.subtitle, font=subtitle_font)
            sub_w = sub_bbox[2] - sub_bbox[0]
            sub_x = x_right - sub_w
            title_max_w = max(0, sub_x - _TITLE_SUBTITLE_GAP - x_title)
            title_text = _truncate_to_width(
                self.title or "", title_font, title_max_w, draw
            )
        else:
            title_max_w = x_right - x_title
            title_text = _truncate_to_width(
                self.title or "", title_font, title_max_w, draw
            )

        # Title is vertically centered (y baseline offset = font height // 2).
        title_bbox = draw.textbbox((0, 0), title_text, font=title_font)
        title_h = title_bbox[3] - title_bbox[1]
        title_y = row_cy - title_h // 2
        draw.text(
            (x_title, title_y), title_text,
            fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b),
            font=title_font,
        )

        # Subtitle (if any) right-aligned, vertically centered.
        if self.subtitle:
            sub_bbox = draw.textbbox((0, 0), self.subtitle, font=subtitle_font)
            sub_h = sub_bbox[3] - sub_bbox[1]
            sub_y = row_cy - sub_h // 2
            draw.text(
                (sub_x, sub_y), self.subtitle,
                fill=(self.subtitle_color.r,
                      self.subtitle_color.g,
                      self.subtitle_color.b),
                font=subtitle_font,
            )
