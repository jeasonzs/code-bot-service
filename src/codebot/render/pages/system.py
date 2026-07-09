"""System resources page: 2x2 grid dashboard for 1.47" 320x172 LCD.

Layout (see plan doc):
  y=0-25    header: `>_ CODEBOT` + HH:MM
  y=26-98   CPU | MEM tiles (big %, dotted bar)
  y=99-146  NET (up/down split) | FREQ (GHz)
  y=147-172 footer: `>_` + temp + fan + disk

The tile / footer rendering is delegated to reusable view classes in
``render.views.*``; this module just composes them with snapshot data
and draws the cell dividers.

Consumes data from SystemCollector (2 Hz background thread) — no own psutil.
"""

from __future__ import annotations

from typing import Optional

from PIL import ImageDraw

from ...collectors.system import SystemCollector
from ..canvas import Canvas
from ..theme import VSCodeDark, SCREEN_W, SCREEN_H
from ..views.footer_view import FooterView
from ..views.tile_view import TileView
from .base import BasePage


# Layout constants for the 2x2 dashboard (full-screen, no top header).
ROW1_Y = 0                     # CPU | MEM top edge
ROW2_Y = 72                    # NET | FREQ top edge
ROW_H = 72                     # body row height
FOOTER_Y = 148                 # footer baseline (icon y)

CELL_W = SCREEN_W // 2         # 160


def _fmt_freq(mhz: float) -> str:
    """Format CPU frequency: '2.8' (1 decimal) or '—' if unavailable."""
    if mhz <= 0:
        return "—"
    return "{0:.1f}".format(mhz / 1000)


def _fmt_rate(kbs: float) -> str:
    """Format a KB/s rate; switch to MB/s above 1024."""
    if kbs >= 1024:
        return "{0:.1f}MB/s".format(kbs / 1024)
    return "{0:.0f}KB/s".format(kbs)


class SystemPage(BasePage):
    """Real-time system resource monitor (2x2 dashboard)."""

    # Empty title disables daemon chrome's default title — the page renders
    # its own header (terminal prompt + "CODEBOT" + clock).
    title = ""
    # Skip the top page-indicator bar; the page fills the entire screen.
    skip_chrome = True

    def __init__(self, collector: SystemCollector) -> None:
        self._collector: SystemCollector = collector

    def render(self, canvas: Canvas) -> None:
        snap = self._collector.snapshot()
        canvas.fill(VSCodeDark.BG)

        if snap is None:
            # First sample not ready; show minimal layout
            return

        self._draw_dividers(canvas)
        self._draw_tiles(canvas, snap)
        self._draw_footer(canvas, snap)

    # ---- Sections ----

    @staticmethod
    def _draw_dividers(canvas: Canvas) -> None:
        """Vertical and horizontal 1px lines separating the 4 cells and
        the footer. Drawn first so tile / footer content overlays them
        if there is any pixel drift (e.g. anti-aliased text descenders)."""
        d = ImageDraw.Draw(canvas.image)
        border = (VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b)
        d.line(
            [(SCREEN_W // 2, ROW1_Y), (SCREEN_W // 2, ROW2_Y + ROW_H)],
            fill=border, width=1,
        )
        d.line(
            [(0, ROW2_Y), (SCREEN_W, ROW2_Y)],
            fill=border, width=1,
        )
        d.line(
            [(0, ROW2_Y + ROW_H), (SCREEN_W, ROW2_Y + ROW_H)],
            fill=border, width=1,
        )

    @staticmethod
    def _draw_tiles(canvas: Canvas, snap) -> None:
        # CPU cell (x=0..159, y=0..72)
        TileView(
            x=0, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="cpu", icon_color=VSCodeDark.INFO,
            title="CPU", title_color=VSCodeDark.INFO,
            value_digits="{0:.0f}".format(snap.cpu_pct), value_unit="%",
            bar_pct=max(0.0, min(100.0, snap.cpu_pct)),
            bar_color=VSCodeDark.INFO,
        ).draw(canvas)
        # MEM cell (x=160..319, y=0..72)
        TileView(
            x=CELL_W, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="mem", icon_color=VSCodeDark.MEM_ACCENT,
            title="MEM", title_color=VSCodeDark.MEM_ACCENT,
            value_digits="{0:.0f}".format(snap.mem_pct), value_unit="%",
            bar_pct=max(0.0, min(100.0, snap.mem_pct)),
            bar_color=VSCodeDark.MEM_ACCENT,
        ).draw(canvas)
        # TEMP cell (x=0..159, y=72..144)
        if snap.cpu_temp_c is not None:
            temp_digits = "{0:.0f}".format(snap.cpu_temp_c)
            temp_unit = "\xb0C"  # °
        else:
            temp_digits = "—"
            temp_unit = ""
        TileView(
            x=0, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="temp", icon_color=VSCodeDark.NET_UP,
            title="TEMP", title_color=VSCodeDark.NET_UP,
            value_digits=temp_digits, value_unit=temp_unit,
        ).draw(canvas)
        # FREQ cell (x=160..319, y=72..144)
        TileView(
            x=CELL_W, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="freq", icon_color=VSCodeDark.FREQ_ACCENT,
            title="FREQ", title_color=VSCodeDark.FREQ_ACCENT,
            value_digits=_fmt_freq(snap.cpu_freq_mhz), value_unit="GHz",
            unit_color=VSCodeDark.FG_DIM,
        ).draw(canvas)

    @staticmethod
    def _draw_footer(canvas: Canvas, snap) -> None:
        FooterView(
            y=FOOTER_Y,
            items=[
                {"icon": "up", "value": _fmt_rate(snap.tx_rate_kbs),
                 "color": VSCodeDark.NET_UP},
                {"icon": "down", "value": _fmt_rate(snap.rx_rate_kbs),
                 "color": VSCodeDark.NET_DOWN},
                {"icon": "disk", "value": "{0:.0f}%".format(snap.disk_pct),
                 "color": VSCodeDark.FG_DIM},
            ],
        ).draw(canvas)
