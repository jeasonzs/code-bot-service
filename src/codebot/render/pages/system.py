"""System resources page: 2x2 grid dashboard for 1.47" 320x172 LCD.

Layout (see plan doc):
  y=0-25    header: `>_ CODEBOT` + HH:MM
  y=26-98   CPU | MEM tiles (big %, dotted bar)
  y=99-146  NET (up/down split) | FREQ (GHz)
  y=147-172 footer: `>_` + temp + fan + disk

Consumes data from SystemCollector (2 Hz background thread) — no own psutil.
"""

from __future__ import annotations

import time
from typing import Optional

from PIL import ImageDraw

from ...collectors.system import SystemCollector
from ..canvas import Canvas
from ..icons import draw_icon
from ..theme import VSCodeDark, SCREEN_W, SCREEN_H
from ..widgets import (
    draw_dotted_bar,
    draw_text_right,
    get_font,
)
from .base import BasePage


# Layout constants for the 2x2 dashboard.
# (No top page-indicator bar — SystemPage sets skip_chrome=True.)
HEADER_Y = 0
HEADER_H = 22                  # y=0..21

ROW1_Y = 22                    # CPU | MEM top edge
ROW2_Y = 94                    # NET | FREQ top edge
ROW_H = 72                     # body row height
FOOTER_Y = 148                 # footer baseline (icon y)

CELL_W = SCREEN_W // 2         # 160
TILE_PAD = 8                   # left/right inner padding
ICON_SIZE = 32                 # tile icons
FOOTER_ICON_SIZE = 12


def _fmt_rate(kbs: float) -> str:
    """Format a rate in KB/s; switch to MB/s above 1024."""
    if kbs >= 1024:
        return f"{kbs / 1024:.1f}MB/s"
    return f"{kbs:.0f}KB/s"


def _fmt_freq(mhz: float) -> str:
    """Format CPU frequency: '2.83' or '—' if unavailable."""
    if mhz <= 0:
        return "—"
    return f"{mhz / 1000:.2f}"


def _draw_big_number(
    canvas: Canvas, text: str, x_right: int, y: int,
    font, color, max_width: int,
) -> None:
    """Right-align a big number; trailing '%' rendered in bold (DSEG lacks it)."""
    draw = ImageDraw.Draw(canvas.image)
    rgb = (color.r, color.g, color.b)

    # Special case: DSEG digit font doesn't have '%' (or '.', '/', etc.).
    # If the text ends with a non-DSEG suffix (commonly '%'), render the
    # digit part in the main font and the suffix in bold, right-aligned.
    if text.endswith("%") and font.size >= 24:
        digits = text[:-1]
        suffix = "%"
        suffix_font = get_font("bold", max(12, font.size * 2 // 3))
        bw_d = draw.textbbox((0, 0), digits, font=font)
        w_d = bw_d[2] - bw_d[0]
        bw_s = draw.textbbox((0, 0), suffix, font=suffix_font)
        w_s = bw_s[2] - bw_s[0]
        gap = 2
        # Right-align: % ends at x_right; digits end at x_right - w_s - gap
        x_suffix = x_right - w_s
        x_digits_right = x_suffix - gap
        # Vertically align bottoms of the two fonts
        y_suffix = y + (font.size - suffix_font.size)
        if w_d + w_s + gap > max_width:
            # Overflow: fall back to bold for the whole thing
            small = get_font("bold", max(12, font.size - 8))
            draw_text_right(canvas, text, x_right, y, small, color)
            return
        draw.text((x_digits_right - w_d, y), digits, fill=rgb, font=font)
        draw.text((x_suffix, y_suffix), suffix, fill=rgb, font=suffix_font)
        return

    # Generic path
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    if tw > max_width:
        small = get_font("bold", max(12, font.size - 8))
        draw_text_right(canvas, text, x_right, y, small, color)
    else:
        draw_text_right(canvas, text, x_right, y, font, color)


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

        # ---- Header (y=0-21) ----
        draw_icon(canvas, "terminal", 4, HEADER_Y, VSCodeDark.INFO, size=14)
        d = ImageDraw.Draw(canvas.image)
        d.text(
            (22, HEADER_Y), "CODEBOT",
            fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b),
            font=get_font("bold", 16),
        )
        draw_text_right(
            canvas, time.strftime("%H:%M"),
            SCREEN_W - 4, HEADER_Y + 1,
            get_font("bold", 18), VSCodeDark.FG_DIM,
        )

        if snap is None:
            # First sample not ready; show minimal layout
            return

        # ---- CPU cell (x=0..159, y=26..98) ----
        self._draw_cpu_tile(canvas, snap.cpu_pct)

        # ---- MEM cell (x=160..319) ----
        self._draw_mem_tile(canvas, snap.mem_pct)

        # ---- NET cell (x=0..159, y=99..146) ----
        self._draw_net_tile(canvas, snap.rx_rate_kbs, snap.tx_rate_kbs)

        # ---- FREQ cell (x=160..319) ----
        self._draw_freq_tile(canvas, snap.cpu_freq_mhz)

        # ---- Footer (y=152..168) ----
        self._draw_footer(canvas, snap.cpu_temp_c, snap.fan_rpm, snap.disk_pct)

    # ---- Per-tile helpers ----

    def _draw_cpu_tile(self, canvas: Canvas, cpu_pct: float) -> None:
        x_cell = 0
        y_icon = ROW1_Y + 6
        draw_icon(canvas, "cpu", x_cell + TILE_PAD, y_icon, VSCodeDark.INFO, size=ICON_SIZE)
        # Label + big number on the right
        draw_text_right(
            canvas, "CPU",
            x_cell + CELL_W - TILE_PAD, ROW1_Y + 2,
            get_font("bold", 12), VSCodeDark.INFO,
        )
        _draw_big_number(
            canvas, f"{cpu_pct:.0f}%",
            x_cell + CELL_W - TILE_PAD, ROW1_Y + 16,
            get_font("digital", 36), VSCodeDark.FG, max_width=100,
        )
        # Dotted bar at the bottom of the cell
        draw_dotted_bar(
            canvas,
            x_cell + TILE_PAD, ROW1_Y + ROW_H - 8,
            CELL_W - 2 * TILE_PAD, 4,
            cpu_pct, fg=VSCodeDark.INFO, n_segments=14, gap=1,
        )

    def _draw_mem_tile(self, canvas: Canvas, mem_pct: float) -> None:
        x_cell = CELL_W
        y_icon = ROW1_Y + 6
        draw_icon(canvas, "mem", x_cell + TILE_PAD, y_icon, VSCodeDark.MEM_ACCENT, size=ICON_SIZE)
        draw_text_right(
            canvas, "MEM",
            x_cell + CELL_W - TILE_PAD, ROW1_Y + 2,
            get_font("bold", 12), VSCodeDark.MEM_ACCENT,
        )
        _draw_big_number(
            canvas, f"{mem_pct:.0f}%",
            x_cell + CELL_W - TILE_PAD, ROW1_Y + 16,
            get_font("digital", 36), VSCodeDark.FG, max_width=100,
        )
        draw_dotted_bar(
            canvas,
            x_cell + TILE_PAD, ROW1_Y + ROW_H - 8,
            CELL_W - 2 * TILE_PAD, 4,
            mem_pct, fg=VSCodeDark.MEM_ACCENT, n_segments=14, gap=1,
        )

    def _draw_net_tile(self, canvas: Canvas, rx_kbs: float, tx_kbs: float) -> None:
        x_cell = 0
        y_icon = ROW2_Y + 8           # 107
        draw_icon(
            canvas, "net",
            x_cell + TILE_PAD, y_icon,
            VSCodeDark.NET_UP, size=ICON_SIZE, color2=VSCodeDark.NET_DOWN,
        )
        # Label: top-left, just right of the icon
        d = ImageDraw.Draw(canvas.image)
        label_x = x_cell + TILE_PAD + ICON_SIZE + 6
        d.text(
            (label_x, ROW2_Y + 0), "NET",
            fill=(VSCodeDark.NET_UP.r, VSCodeDark.NET_UP.g, VSCodeDark.NET_UP.b),
            font=get_font("bold", 11),
        )

        # Auto-unit: MB/s if either side >= 1024 KB/s
        use_mb = rx_kbs >= 1024 or tx_kbs >= 1024
        if use_mb:
            up_str = f"↑{rx_kbs / 1024:.1f}"
            down_str = f"↓{tx_kbs / 1024:.1f}"
            unit = "MB/s"
        else:
            up_str = f"↑{rx_kbs:.0f}"
            down_str = f"↓{tx_kbs:.0f}"
            unit = "KB/s"

        # Two stacked value pairs (up | down) with a vertical divider
        text_x_start = label_x
        cell_right = x_cell + CELL_W - TILE_PAD
        cell_mid = (text_x_start + cell_right) // 2

        font_num = get_font("bold", 18)
        font_unit = get_font("bold", 9)
        y_num = ROW2_Y + 14
        y_unit = ROW2_Y + 34

        # Up (left of divider)
        d.text(
            (text_x_start, y_num), up_str,
            fill=(VSCodeDark.NET_UP.r, VSCodeDark.NET_UP.g, VSCodeDark.NET_UP.b),
            font=font_num,
        )
        d.text(
            (text_x_start, y_unit), unit,
            fill=(VSCodeDark.NET_UP.r, VSCodeDark.NET_UP.g, VSCodeDark.NET_UP.b),
            font=font_unit,
        )
        # Down (right of divider)
        d.text(
            (cell_mid + 4, y_num), down_str,
            fill=(VSCodeDark.NET_DOWN.r, VSCodeDark.NET_DOWN.g, VSCodeDark.NET_DOWN.b),
            font=font_num,
        )
        d.text(
            (cell_mid + 4, y_unit), unit,
            fill=(VSCodeDark.NET_DOWN.r, VSCodeDark.NET_DOWN.g, VSCodeDark.NET_DOWN.b),
            font=font_unit,
        )
        # Vertical divider
        d.line(
            [(cell_mid, ROW2_Y + 4), (cell_mid, ROW2_Y + ROW_H - 4)],
            fill=(VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b),
            width=1,
        )

    def _draw_freq_tile(self, canvas: Canvas, freq_mhz: float) -> None:
        x_cell = CELL_W
        y_icon = ROW2_Y + 14
        draw_icon(canvas, "freq", x_cell + TILE_PAD, y_icon, VSCodeDark.FREQ_ACCENT, size=ICON_SIZE)
        draw_text_right(
            canvas, "FREQ",
            x_cell + CELL_W - TILE_PAD, ROW2_Y + 2,
            get_font("bold", 12), VSCodeDark.FREQ_ACCENT,
        )
        # "2.83 GHz"
        freq_str = _fmt_freq(freq_mhz)
        draw_text_right(
            canvas, freq_str,
            x_cell + CELL_W - TILE_PAD - 26, ROW2_Y + 18,
            get_font("digital", 28), VSCodeDark.FG,
        )
        # "GHz" small label to the right of the number
        draw_text_right(
            canvas, "GHz",
            x_cell + CELL_W - TILE_PAD, ROW2_Y + 28,
            get_font("bold", 12), VSCodeDark.FG_DIM,
        )

    def _draw_footer(
        self, canvas: Canvas,
        cpu_temp_c: Optional[float], fan_rpm: Optional[int], disk_pct: float,
    ) -> None:
        d = ImageDraw.Draw(canvas.image)
        dim = (VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b)
        font = get_font("bold", 11)
        # `>_` prompt
        draw_icon(canvas, "terminal", 4, FOOTER_Y, VSCodeDark.INFO, size=FOOTER_ICON_SIZE)
        # thermo
        draw_icon(canvas, "thermo", 60, FOOTER_Y, VSCodeDark.FG_DIM, size=FOOTER_ICON_SIZE)
        temp_text = f"{cpu_temp_c:.0f}°C" if cpu_temp_c is not None else "—"
        d.text((74, FOOTER_Y + 1), temp_text, fill=dim, font=font)
        # fan
        draw_icon(canvas, "fan", 140, FOOTER_Y, VSCodeDark.FG_DIM, size=FOOTER_ICON_SIZE)
        fan_text = f"{fan_rpm} RPM" if fan_rpm is not None else "—"
        d.text((154, FOOTER_Y + 1), fan_text, fill=dim, font=font)
        # disk
        draw_icon(canvas, "disk", 220, FOOTER_Y, VSCodeDark.FG_DIM, size=FOOTER_ICON_SIZE)
        d.text((234, FOOTER_Y + 1), f"{disk_pct:.0f}%", fill=dim, font=font)
