"""Custom Commands page: 1-2 user-configured apps side-by-side.

Each item is ``{name, icon_path, command}``. ``icon_path`` is read straight
off disk via Pillow — no copy, no resize; missing/unreadable paths fall
back to a 64×64 dashed-bordered placeholder with a centred "?". Hit-test
on DOWN returns the matching ``command``; daemon fires it via Popen on UP.

Layout follows the same conventions as SystemPage / GithubPage / ClaudePage:
``skip_chrome = True``, full-screen, no title bar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

from ..canvas import Canvas
from ..theme import Color, VSCodeDark, SCREEN_W, SCREEN_H
from ..widgets import draw_text_centered, get_font
from .base import BasePage


log = logging.getLogger("codebot.render.commands")


# Geometry — derived from 320x172 + system.py ROW/CELL conventions.
_DIVIDER_X = SCREEN_W // 2  # 160
_DIVIDER_Y_TOP = 4
_DIVIDER_Y_BOT = SCREEN_H - 8  # 164

_PAIR_ICON_SIZE = 80
_PAIR_ICON_LEFT_CX = 80
_PAIR_ICON_RIGHT_CX = SCREEN_W - 80  # 240
_PAIR_ICON_Y_TOP = 35
_PAIR_TITLE_Y = 121
_PAIR_HIT_PAD = 6
_PAIR_HIT_DEADZONE = 5  # clicks within ±deadzone of divider_x are ignored


@dataclass
class CommandItem:
    name: str
    icon_path: str          # absolute path on disk; "" triggers placeholder
    command: str            # single shell command


class CommandsPage(BasePage):
    """Full-screen Custom Commands sub-page (1 or 2 items)."""

    skip_chrome = True
    title = ""

    def __init__(self, items: list[CommandItem]) -> None:
        # Defensive: caller (make_pages) guarantees 1 or 2 items, but render
        # must not crash even if a page is constructed empty.
        self.items = list(items)[:2]

    def render(self, canvas: Canvas) -> None:
        canvas.fill(VSCodeDark.BG)
        draw = ImageDraw.Draw(canvas.image)

        if len(self.items) == 0:
            return

        if len(self.items) == 2:
            # Divider only makes sense when both slots are populated.
            border_rgb = (VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b)
            draw.line(
                [(_DIVIDER_X, _DIVIDER_Y_TOP), (_DIVIDER_X, _DIVIDER_Y_BOT)],
                fill=border_rgb, width=1,
            )
            self._render_item(
                canvas, self.items[0],
                cx=_PAIR_ICON_LEFT_CX, icon_y=_PAIR_ICON_Y_TOP,
                icon_size=_PAIR_ICON_SIZE, title_y=_PAIR_TITLE_Y,
            )
            self._render_item(
                canvas, self.items[1],
                cx=_PAIR_ICON_RIGHT_CX, icon_y=_PAIR_ICON_Y_TOP,
                icon_size=_PAIR_ICON_SIZE, title_y=_PAIR_TITLE_Y,
            )
        else:
            # Lone item lives in the left slot; no divider drawn.
            self._render_item(
                canvas, self.items[0],
                cx=_PAIR_ICON_LEFT_CX, icon_y=_PAIR_ICON_Y_TOP,
                icon_size=_PAIR_ICON_SIZE, title_y=_PAIR_TITLE_Y,
            )

    def _render_item(
        self, canvas: Canvas, item: CommandItem,
        cx: int, icon_y: int, icon_size: int, title_y: int,
    ) -> None:
        img = _load_icon(item.icon_path, icon_size)
        ix = cx - icon_size // 2
        if img is not None:
            canvas.image.paste(img, (ix, icon_y), img)
        else:
            self._draw_placeholder(canvas, ix, icon_y, icon_size)

        font = get_font("cjk", 12)
        rgb = (VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b)
        draw_text_centered(canvas, item.name, cx, title_y, font, Color(*rgb))

    @staticmethod
    def _draw_placeholder(canvas: Canvas, x: int, y: int, size: int) -> None:
        draw = ImageDraw.Draw(canvas.image)
        border = (VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b)
        # Dashed-style: solid outline reads cleaner on 320x172; keep it 1px.
        draw.rectangle(
            [(x, y), (x + size - 1, y + size - 1)],
            outline=border, width=1,
        )
        font = get_font("bold", max(16, size // 2))
        rgb = (VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b)
        cx, cy = x + size // 2, y + size // 2
        draw_text_centered(
            canvas, "?", cx, cy, font, Color(*rgb),
        )

    def on_touch(self, event_type: int, x: int, y: int) -> Optional[str]:
        # Service daemon already detected DOWN→UP click; here we only react
        # to DOWN and return the matching command.
        if event_type != 0:  # TouchEvent.DOWN
            return None
        if len(self.items) == 0:
            return None
        # Reject clicks too close to the divider.
        if abs(x - _DIVIDER_X) <= _PAIR_HIT_DEADZONE:
            return None
        if len(self.items) == 2:
            # Pick by x; both icons share the same vertical hit band.
            if x < _DIVIDER_X:
                item = self.items[0]
                cx = _PAIR_ICON_LEFT_CX
            else:
                item = self.items[1]
                cx = _PAIR_ICON_RIGHT_CX
        else:
            # Solo: only the left slot is hot; right half is empty.
            item = self.items[0]
            cx = _PAIR_ICON_LEFT_CX

        half = _PAIR_ICON_SIZE // 2 + _PAIR_HIT_PAD
        if abs(x - cx) > half:
            return None
        if not (_PAIR_ICON_Y_TOP - _PAIR_HIT_PAD <= y <= _PAIR_TITLE_Y + _PAIR_HIT_PAD):
            return None
        return item.command


# ---- icon cache ----

_icon_cache: dict[tuple[str, int], Optional[Image.Image]] = {}
_warned_paths: set[str] = set()


def _load_icon(path: str, size: int) -> Optional[Image.Image]:
    """Return a sized RGBA image, or None if path is empty / unreadable."""
    key = (path, size)
    if key in _icon_cache:
        return _icon_cache[key]
    if not path:
        _icon_cache[key] = None
        return None
    try:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(path)
        with Image.open(str(p)) as img:
            img.load()
            rgba = img.convert("RGBA")
            if rgba.size != (size, size):
                rgba = rgba.resize((size, size), Image.LANCZOS)
            out = rgba.copy()
    except (FileNotFoundError, OSError, Image.UnidentifiedImageError, ValueError) as e:
        if path not in _warned_paths:
            log.warning("icon %r unavailable (%s); using placeholder", path, e)
            _warned_paths.add(path)
        out = None
    _icon_cache[key] = out
    return out
