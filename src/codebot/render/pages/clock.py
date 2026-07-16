"""Clock page: large white time + separate green ms row + Chinese date.

Layout (320x172, no chrome — fills the entire screen):

  y=10..32   DATE row   "2026年7月16日 周四"  CJK 18pt  INFO blue  (left-anchored)
  y=50..112  TIME row   "HH:MM:SS"            bold 60pt white        (centered)
  y=120..148 MS row     ".mmm"               bold 24pt SUCCESS      (centered)

Visual hierarchy: time dominates, ms reads as a precision readout,
date is a small annotation pinned to the top-left.
"""

from __future__ import annotations

import datetime as _dt

from PIL import ImageDraw

from ..canvas import Canvas
from ..theme import Color, VSCodeDark, SCREEN_W
from ..widgets import draw_text_centered, get_font
from .base import BasePage


# Pure white (255,255,255). VSCodeDark.FG is 212/212/212 (near-white); the
# spec explicitly asks for "白色" so use full white for max contrast.
_WHITE = Color(255, 255, 255)


# Chinese short weekday names. ISO weekday(): Mon=0..Sun=6.
_WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# Horizontal inset for the left-anchored date row.
_LEFT_PAD = 8


class ClockPage(BasePage):
    """Big white clock + green ms + Chinese date header."""

    # Empty title + skip_chrome -> daemon draws no top indicator / title
    # bar; the page owns the full 320x172 canvas.
    title = ""
    skip_chrome = True

    def render(self, canvas: Canvas) -> None:
        now = _dt.datetime.now()
        canvas.fill(VSCodeDark.BG)
        d = ImageDraw.Draw(canvas.image)

        # ---- DATE row: "2026年7月16日 周四" — CJK small, INFO blue, left ----
        date_str = "{0}年{1}月{2}日 {3}".format(
            now.year, now.month, now.day, _WEEKDAY_ZH[now.weekday()]
        )
        date_font = get_font("cjk", 18)
        d.text((_LEFT_PAD, 10), date_str,
               fill=(VSCodeDark.INFO.r, VSCodeDark.INFO.g, VSCodeDark.INFO.b),
               font=date_font)

        # ---- TIME row: "HH:MM:SS" big bold white, centered ----
        time_str = "{0:02d}:{1:02d}:{2:02d}".format(
            now.hour, now.minute, now.second
        )
        time_font = get_font("bold", 60)
        draw_text_centered(canvas, time_str, SCREEN_W // 2, 50,
                           time_font, _WHITE)

        # ---- MS row: ".mmm" smaller bold SUCCESS green, centered ----
        ms_str = ".{0:03d}".format(now.microsecond // 1000)
        ms_font = get_font("bold", 24)
        draw_text_centered(canvas, ms_str, SCREEN_W // 2, 122,
                           ms_font, VSCodeDark.SUCCESS)