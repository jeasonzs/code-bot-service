"""System resources page: 2x2 grid dashboard for 1.47" 320x172 LCD.

Layout (see plan doc):
  y=0-25    header: `>_ CODEBOT` + HH:MM
  y=26-98   CPU | MEM tiles (big %, dotted bar)
  y=99-146  NET (up/down split) | FREQ (GHz)
  y=147-172 footer: `>_` + temp + fan + disk

Consumes data from SystemCollector (2 Hz background thread) — no own psutil.
"""

from __future__ import annotations

from typing import Optional

from PIL import ImageDraw

from ...collectors.system import SystemCollector
from ..canvas import Canvas
from ..icons import draw_icon
from ..theme import VSCodeDark, SCREEN_W, SCREEN_H
from ..widgets import (
    draw_dotted_bar,
    draw_text_centered,
    draw_text_right,
    get_font,
)
from .base import BasePage


# Layout constants for the 2x2 dashboard (full-screen, no top header).
ROW1_Y = 0                     # CPU | MEM top edge
ROW2_Y = 72                    # NET | FREQ top edge
ROW_H = 72                     # body row height
FOOTER_Y = 148                 # footer baseline (icon y)

CELL_W = SCREEN_W // 2         # 160
TILE_PAD = 8                   # left/right inner padding
ICON_SIZE = 40                 # tile icons
FOOTER_ICON_SIZE = 12


def _fmt_rate(kbs: float) -> str:
    """Format a rate in KB/s; switch to MB/s above 1024."""
    if kbs >= 1024:
        return f"{kbs / 1024:.1f}MB/s"
    return f"{kbs:.0f}KB/s"


def _fmt_freq(mhz: float) -> str:
    """Format CPU frequency: '2.8' (1 decimal) or '—' if unavailable."""
    if mhz <= 0:
        return "—"
    return "{0:.1f}".format(mhz / 1000)


def _draw_big_number(
    canvas: Canvas, text: str, x_right: int, y: int,
    font, color, max_width: int,
) -> None:
    """Right-align a big number; trailing '%' rendered in bold (DSEG lacks it)."""
    draw = ImageDraw.Draw(canvas.image)
    rgb = (color.r, color.g, color.b)

    # Special case: DSEG digit font doesn't have '%' (or '.', '/', etc.).
    # If the text ends with a non-DSEG suffix (commonly '%'), render the
    # digit part in the main font and the suffix in bold at font 12
    # (matching the GHz unit size used in FREQ tile).
    if text.endswith("%") and font.size >= 24:
        digits = text[:-1]
        suffix = "%"
        suffix_font = get_font("bold", 12)
        bw_d = draw.textbbox((0, 0), digits, font=font)
        w_d = bw_d[2] - bw_d[0]
        bw_s = draw.textbbox((0, 0), suffix, font=suffix_font)
        w_s = bw_s[2] - bw_s[0]
        gap = 2
        x_suffix = x_right - w_s
        x_digits_right = x_suffix - gap
        y_suffix = y + (font.size - suffix_font.size)
        if w_d + w_s + gap > max_width:
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

        if snap is None:
            # First sample not ready; show minimal layout
            return

        d = ImageDraw.Draw(canvas.image)
        # ---- Cell dividers (drawn behind content) ----
        border = (VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b)
        # Vertical: between columns (left tiles vs right tiles)
        d.line(
            [(SCREEN_W // 2, ROW1_Y), (SCREEN_W // 2, ROW2_Y + ROW_H)],
            fill=border, width=1,
        )
        # Horizontal: between row1 and row2
        d.line(
            [(0, ROW2_Y), (SCREEN_W, ROW2_Y)],
            fill=border, width=1,
        )
        # Horizontal: between row2 and footer
        d.line(
            [(0, ROW2_Y + ROW_H), (SCREEN_W, ROW2_Y + ROW_H)],
            fill=border, width=1,
        )

        # ---- CPU cell (x=0..159, y=0..72) ----
        self._draw_cpu_tile(canvas, snap.cpu_pct)

        # ---- MEM cell (x=160..319) ----
        self._draw_mem_tile(canvas, snap.mem_pct)

        # ---- TEMP cell (x=0..159, y=72..144) ----
        self._draw_temp_tile(canvas, snap.cpu_temp_c)

        # ---- FREQ cell (x=160..319) ----
        self._draw_freq_tile(canvas, snap.cpu_freq_mhz)

        # ---- Footer (y=148..172) ----
        self._draw_footer(canvas, snap.rx_rate_kbs, snap.tx_rate_kbs, snap.disk_pct)

    # ---- Per-tile helpers ----

    def _draw_cpu_tile(self, canvas: Canvas, cpu_pct: float) -> None:
        x_cell = 0
        icon_x = x_cell + TILE_PAD
        y_icon = ROW1_Y + 6
        draw_icon(canvas, "cpu", icon_x, y_icon, VSCodeDark.INFO, size=ICON_SIZE)
        # Big number on the right
        _draw_big_number(
            canvas, "{0:.0f}%".format(cpu_pct),
            x_cell + CELL_W - TILE_PAD, ROW1_Y + 10,
            get_font("digital", 36), VSCodeDark.FG, max_width=100,
        )
        # Title centered below icon
        title_font = get_font("bold", 11)
        d = ImageDraw.Draw(canvas.image)
        bbox_t = d.textbbox((0, 0), "CPU", font=title_font)
        title_w = bbox_t[2] - bbox_t[0]
        icon_cx = icon_x + ICON_SIZE // 2
        title_left = icon_cx - title_w // 2
        draw_text_centered(
            canvas, "CPU",
            icon_cx, ROW1_Y + 48,
            title_font, VSCodeDark.INFO,
        )
        # Dotted bar: left edge starts just past the icon's right edge
        bar_x_start = icon_x + ICON_SIZE + 4
        bar_x_end = x_cell + CELL_W - TILE_PAD
        draw_dotted_bar(
            canvas,
            bar_x_start, ROW1_Y + ROW_H - 8,
            bar_x_end - bar_x_start, 4,
            cpu_pct, fg=VSCodeDark.INFO, n_segments=14, gap=1,
        )

    def _draw_mem_tile(self, canvas: Canvas, mem_pct: float) -> None:
        x_cell = CELL_W
        icon_x = x_cell + TILE_PAD
        y_icon = ROW1_Y + 6
        draw_icon(canvas, "mem", icon_x, y_icon, VSCodeDark.MEM_ACCENT, size=ICON_SIZE)
        _draw_big_number(
            canvas, "{0:.0f}%".format(mem_pct),
            x_cell + CELL_W - TILE_PAD, ROW1_Y + 10,
            get_font("digital", 36), VSCodeDark.FG, max_width=100,
        )
        title_font = get_font("bold", 11)
        d = ImageDraw.Draw(canvas.image)
        bbox_t = d.textbbox((0, 0), "MEM", font=title_font)
        title_w = bbox_t[2] - bbox_t[0]
        icon_cx = icon_x + ICON_SIZE // 2
        title_left = icon_cx - title_w // 2
        draw_text_centered(
            canvas, "MEM",
            icon_cx, ROW1_Y + 48,
            title_font, VSCodeDark.MEM_ACCENT,
        )
        # Dotted bar: left edge starts just past the icon's right edge
        bar_x_start = icon_x + ICON_SIZE + 4
        bar_x_end = x_cell + CELL_W - TILE_PAD
        draw_dotted_bar(
            canvas,
            bar_x_start, ROW1_Y + ROW_H - 8,
            bar_x_end - bar_x_start, 4,
            mem_pct, fg=VSCodeDark.MEM_ACCENT, n_segments=14, gap=1,
        )

    def _draw_temp_tile(self, canvas: Canvas, cpu_temp_c: Optional[float]) -> None:
        x_cell = 0
        icon_x = x_cell + TILE_PAD
        y_icon = ROW2_Y + 6
        draw_icon(canvas, "temp", icon_x, y_icon, VSCodeDark.NET_UP, size=ICON_SIZE)
        # Big value: "42" in DSEG, "°C" in bold to the right
        d = ImageDraw.Draw(canvas.image)
        if cpu_temp_c is not None:
            num = "{0:.0f}".format(cpu_temp_c)
            unit = "\xb0C"
        else:
            num = "—"
            unit = ""
        font_d = get_font("digital", 36)
        font_u = get_font("bold", 12)
        x_right = x_cell + CELL_W - TILE_PAD
        if unit:
            bbox_u = d.textbbox((0, 0), unit, font=font_u)
            w_u = bbox_u[2] - bbox_u[0]
            y_unit = ROW2_Y + 10 + (36 - 12)
            draw_text_right(canvas, unit, x_right, y_unit, font_u, VSCodeDark.FG)
            x_num_right = x_right - w_u - 4
        else:
            x_num_right = x_right
        draw_text_right(canvas, num, x_num_right, ROW2_Y + 10, font_d, VSCodeDark.FG)
        # Title centered below icon
        draw_text_centered(
            canvas, "TEMP",
            icon_x + ICON_SIZE // 2, ROW2_Y + 48,
            get_font("bold", 11), VSCodeDark.NET_UP,
        )

    def _draw_freq_tile(self, canvas: Canvas, freq_mhz: float) -> None:
        x_cell = CELL_W
        icon_x = x_cell + TILE_PAD
        y_icon = ROW2_Y + 6
        draw_icon(canvas, "freq", icon_x, y_icon, VSCodeDark.FREQ_ACCENT, size=ICON_SIZE)
        freq_str = _fmt_freq(freq_mhz)
        font_d = get_font("digital", 36)
        font_u = get_font("bold", 12)
        d = ImageDraw.Draw(canvas.image)
        bbox_u = d.textbbox((0, 0), "GHz", font=font_u)
        w_u = bbox_u[2] - bbox_u[0]
        x_right = x_cell + CELL_W - TILE_PAD
        x_unit = x_right
        x_num_right = x_unit - w_u - 4
        y_unit = ROW2_Y + 10 + (36 - 12)
        draw_text_right(canvas, freq_str, x_num_right, ROW2_Y + 10, font_d, VSCodeDark.FG)
        draw_text_right(canvas, "GHz", x_unit, y_unit, font_u, VSCodeDark.FG_DIM)
        # Title centered below icon
        draw_text_centered(
            canvas, "FREQ",
            icon_x + ICON_SIZE // 2, ROW2_Y + 48,
            get_font("bold", 11), VSCodeDark.FREQ_ACCENT,
        )

    def _draw_footer(
        self, canvas: Canvas,
        rx_rate_kbs: float, tx_rate_kbs: float, disk_pct: float,
    ) -> None:
        d = ImageDraw.Draw(canvas.image)
        dim = (VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b)
        up_color = (VSCodeDark.NET_UP.r, VSCodeDark.NET_UP.g, VSCodeDark.NET_UP.b)
        down_color = (VSCodeDark.NET_DOWN.r, VSCodeDark.NET_DOWN.g, VSCodeDark.NET_DOWN.b)
        font = get_font("bold", 11)

        def fmt_rate(kbs):
            if kbs >= 1024:
                return "{0:.1f}MB/s".format(kbs / 1024)
            return "{0:.0f}KB/s".format(kbs)

        # `>_` prompt (leftmost)
        draw_icon(canvas, "terminal", 4, FOOTER_Y, VSCodeDark.INFO, size=FOOTER_ICON_SIZE)
        # Slot 1: up arrow + upload rate (green)
        draw_icon(canvas, "up", 32, FOOTER_Y, VSCodeDark.NET_UP, size=FOOTER_ICON_SIZE)
        d.text((46, FOOTER_Y + 1), fmt_rate(tx_rate_kbs), fill=up_color, font=font)
        # Slot 2: down arrow + download rate (cyan)
        draw_icon(canvas, "down", 122, FOOTER_Y, VSCodeDark.NET_DOWN, size=FOOTER_ICON_SIZE)
        d.text((136, FOOTER_Y + 1), fmt_rate(rx_rate_kbs), fill=down_color, font=font)
        # Slot 3: disk + usage %
        draw_icon(canvas, "disk", 212, FOOTER_Y, VSCodeDark.FG_DIM, size=FOOTER_ICON_SIZE)
        d.text((226, FOOTER_Y + 1), "{0:.0f}%".format(disk_pct), fill=dim, font=font)
