"""System resources page: 2x2 grid dashboard for 1.47" 320x172 LCD.

Layout:
  y=0..28    Header: `>_ <title>` ... <host>   (HeaderView, 28 px tall)
  ── divider at y=28 ──
  y=28..100  CPU | FREQ tiles
  ── divider at y=100 ──
  y=100..172 MEM | GPU (有数据时) / DISK-free tiles

The header + tile rendering is delegated to reusable view classes in
``render.views.*``; this module just composes them with snapshot data
and draws the cell dividers.

Consumes data from SystemCollector (2 Hz background thread) — no own psutil.
"""

from __future__ import annotations

from typing import Optional

from PIL import ImageDraw

from ...collectors.system import SystemCollector
from ..canvas import Canvas
from ..theme import HEADER_H, VSCodeDark, SCREEN_W
from ..views.header_view import HeaderView
from ..views.tile_view import TileView
from .base import BasePage


# Layout constants for the 2x2 dashboard below the top header.
# HEADER_H (28) + 2 × ROW_H (72) = 172 fills the screen exactly; the
# 8px that used to be the footer blank is now absorbed into the header.
ROW_H = 72
ROW1_Y = HEADER_H              # CPU | FREQ top edge
ROW2_Y = HEADER_H + ROW_H      # MEM | DISK/GPU top edge

CELL_W = SCREEN_W // 2         # 160


def _fmt_freq(mhz: float) -> str:
    """Format CPU frequency: '2.8' (1 decimal) or '—' if unavailable."""
    if mhz <= 0:
        return "—"
    return "{0:.1f}".format(mhz / 1000)


class SystemPage(BasePage):
    """Real-time system resource monitor (2x2 dashboard)."""

    # Empty class-level title; the daemon stamps the per-entry display name
    # at construction. The HeaderView uses ``self._title`` directly.
    title = ""
    # Skip the daemon chrome (top indicator + derived title). The page
    # renders its own HeaderView.
    skip_chrome = True

    def __init__(
        self,
        collector: SystemCollector,
        *,
        title: str,
        host_label: str,
    ) -> None:
        self._collector: SystemCollector = collector
        self._title = title
        self._host_label = host_label

    def render(self, canvas: Canvas) -> None:
        snap = self._collector.snapshot()
        canvas.fill(VSCodeDark.BG)

        if snap is None:
            # First sample not ready; show header only so the row
            # doesn't look completely blank on startup.
            self._draw_header(canvas)
            return

        self._draw_header(canvas)
        self._draw_dividers(canvas)
        self._draw_tiles(canvas, snap)

    # ---- Sections ----

    def _draw_header(self, canvas: Canvas) -> None:
        HeaderView(
            title=self._title,
            subtitle=self._host_label,
            subtitle_color=VSCodeDark.INFO,
        ).draw(canvas)

    @staticmethod
    def _draw_dividers(canvas: Canvas) -> None:
        """Vertical and horizontal 1px lines separating the header from
        the body, and the two tile rows from each other. Drawn first so
        tile content overlays them if there is any pixel drift (e.g.
        anti-aliased text descenders). The bottom row's lower edge sits
        on the screen boundary (y=172) so no bottom divider is drawn."""
        d = ImageDraw.Draw(canvas.image)
        border = (VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b)
        # Header / body separator.
        d.line(
            [(0, HEADER_H), (SCREEN_W, HEADER_H)],
            fill=border, width=1,
        )
        # Vertical line between left and right tiles (only between rows).
        d.line(
            [(SCREEN_W // 2, ROW1_Y), (SCREEN_W // 2, ROW2_Y + ROW_H)],
            fill=border, width=1,
        )
        # Horizontal line between row 1 and row 2.
        d.line(
            [(0, ROW2_Y), (SCREEN_W, ROW2_Y)],
            fill=border, width=1,
        )

    def _draw_tiles(self, canvas: Canvas, snap) -> None:
        # CPU cell (x=0..159, y=ROW1_Y..ROW2_Y)
        TileView(
            x=0, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="cpu", icon_color=VSCodeDark.INFO,
            title="CPU", title_color=VSCodeDark.INFO,
            value_digits="{0:.0f}".format(snap.cpu_pct), value_unit="%",
            bar_pct=max(0.0, min(100.0, snap.cpu_pct)),
            bar_color=VSCodeDark.INFO,
        ).draw(canvas)
        # FREQ cell (x=160..319, y=ROW1_Y..ROW2_Y)
        TileView(
            x=CELL_W, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="freq", icon_color=VSCodeDark.FREQ_ACCENT,
            title="FREQ", title_color=VSCodeDark.FREQ_ACCENT,
            value_digits=_fmt_freq(snap.cpu_freq_mhz), value_unit="GHz",
            unit_color=VSCodeDark.FG_DIM,
        ).draw(canvas)
        # MEM cell (x=0..159, y=ROW2_Y..ROW2_Y+ROW_H)
        TileView(
            x=0, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="mem", icon_color=VSCodeDark.MEM_ACCENT,
            title="MEM", title_color=VSCodeDark.MEM_ACCENT,
            value_digits="{0:.0f}".format(snap.mem_pct), value_unit="%",
            bar_pct=max(0.0, min(100.0, snap.mem_pct)),
            bar_color=VSCodeDark.MEM_ACCENT,
        ).draw(canvas)
        # 右下格: 有 GPU 数据时显示 GPU 使用率, 否则显示磁盘剩余空间
        # (x=160..319, y=ROW2_Y..ROW2_Y+ROW_H)
        if snap.gpu_pct is not None:
            TileView(
                x=CELL_W, y=ROW2_Y, w=CELL_W, h=ROW_H,
                icon="gpu", icon_color=VSCodeDark.NET_UP,
                title="GPU", title_color=VSCodeDark.NET_UP,
                value_digits="{0:.0f}".format(snap.gpu_pct), value_unit="%",
                bar_pct=max(0.0, min(100.0, snap.gpu_pct)),
                bar_color=VSCodeDark.NET_UP,
            ).draw(canvas)
        else:
            TileView(
                x=CELL_W, y=ROW2_Y, w=CELL_W, h=ROW_H,
                icon="disk", icon_color=VSCodeDark.NET_UP,
                title="DISK", title_color=VSCodeDark.NET_UP,
                value_digits="{0:.0f}".format(snap.disk_free_gb), value_unit="GB",
                unit_color=VSCodeDark.FG_DIM,
            ).draw(canvas)
