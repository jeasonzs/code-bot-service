"""System resources page: CPU, MEM, DISK, NET metrics."""

from __future__ import annotations

import psutil
from typing import Optional

from ..canvas import Canvas
from ..theme import VSCodeDark, Color, SCREEN_W, SCREEN_H, INDICATOR_H, TITLE_H
from ..widgets import draw_indicator, draw_title, draw_hint, draw_progress_bar, get_font
from .base import BasePage
from PIL import ImageDraw, ImageFont


def _color_for_pct(pct: float) -> Color:
    """Pick a color based on load."""
    if pct >= 90: return VSCodeDark.DANGER
    if pct >= 70: return VSCodeDark.WARNING
    return VSCodeDark.SUCCESS


class SystemPage(BasePage):
    """Real-time system resource monitor."""

    title = "System"

    def __init__(self) -> None:
        self._last_net = psutil.net_io_counters()
        self._last_time = 0.0
        self._cached: dict = {}

    def _sample(self) -> None:
        """Sample current system metrics (call ~2 Hz)."""
        import time
        now = time.time()
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net = psutil.net_io_counters()

        # Network rate (delta / time)
        elapsed = now - self._last_time if self._last_time else 1.0
        rx_rate = (net.bytes_recv - self._last_net.bytes_recv) / max(elapsed, 0.001)
        tx_rate = (net.bytes_sent - self._last_net.bytes_sent) / max(elapsed, 0.001)
        self._last_net = net
        self._last_time = now

        # CPU frequency (MHz)
        try:
            cpu_freq = psutil.cpu_freq().current
        except (AttributeError, OSError):
            cpu_freq = 0

        self._cached = {
            "cpu_pct": cpu_pct,
            "cpu_freq": cpu_freq,
            "mem_pct": mem.percent,
            "mem_used_gb": mem.used / (1024 ** 3),
            "mem_total_gb": mem.total / (1024 ** 3),
            "disk_pct": disk.percent,
            "disk_used_gb": disk.used / (1024 ** 3),
            "disk_total_gb": disk.total / (1024 ** 3),
            "rx_rate_kbs": rx_rate / 1024,
            "tx_rate_kbs": tx_rate / 1024,
        }

    def render(self, canvas: Canvas) -> None:
        self._sample()
        d = self._cached
        canvas.fill(VSCodeDark.BG)

        # Top status bar (will be drawn by daemon)
        # Page indicator and title
        font_label = get_font("default", 11)
        font_value = get_font("mono", 11)
        draw = ImageDraw.Draw(canvas.image)

        # CPU
        y = 30
        draw.text((4, y), "CPU", fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font_label)
        cpu_text = f"{d['cpu_pct']:.0f}%"
        bbox = draw.textbbox((0, 0), cpu_text, font=font_value)
        tw = bbox[2] - bbox[0]
        draw.text((SCREEN_W - tw - 4, y), cpu_text, fill=(_color_for_pct(d['cpu_pct']).r, _color_for_pct(d['cpu_pct']).g, _color_for_pct(d['cpu_pct']).b), font=font_value)
        canvas.progress_bar(4, y + 12, SCREEN_W - 8, 6, d['cpu_pct'], _color_for_pct(d['cpu_pct']), VSCodeDark.BG_PANEL)

        # Memory
        y = 54
        draw.text((4, y), "MEM", fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font_label)
        mem_text = f"{d['mem_used_gb']:.1f}/{d['mem_total_gb']:.1f}G"
        bbox = draw.textbbox((0, 0), mem_text, font=font_value)
        tw = bbox[2] - bbox[0]
        draw.text((SCREEN_W - tw - 4, y), mem_text, fill=(_color_for_pct(d['mem_pct']).r, _color_for_pct(d['mem_pct']).g, _color_for_pct(d['mem_pct']).b), font=font_value)
        canvas.progress_bar(4, y + 12, SCREEN_W - 8, 6, d['mem_pct'], _color_for_pct(d['mem_pct']), VSCodeDark.BG_PANEL)

        # Disk
        y = 78
        draw.text((4, y), "DISK", fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font_label)
        disk_text = f"{d['disk_used_gb']:.0f}/{d['disk_total_gb']:.0f}G"
        bbox = draw.textbbox((0, 0), disk_text, font=font_value)
        tw = bbox[2] - bbox[0]
        draw.text((SCREEN_W - tw - 4, y), disk_text, fill=(_color_for_pct(d['disk_pct']).r, _color_for_pct(d['disk_pct']).g, _color_for_pct(d['disk_pct']).b), font=font_value)
        canvas.progress_bar(4, y + 12, SCREEN_W - 8, 6, d['disk_pct'], _color_for_pct(d['disk_pct']), VSCodeDark.BG_PANEL)

        # Network
        y = 102
        draw.text((4, y), "NET", fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font_label)
        # Format rates nicely
        def fmt_rate(kbs: float) -> str:
            if kbs >= 1024: return f"{kbs/1024:.1f}MB/s"
            return f"{kbs:.0f}KB/s"
        net_text = f"↓{fmt_rate(d['rx_rate_kbs'])} ↑{fmt_rate(d['tx_rate_kbs'])}"
        bbox = draw.textbbox((0, 0), net_text, font=font_value)
        tw = bbox[2] - bbox[0]
        draw.text((SCREEN_W - tw - 4, y), net_text, fill=(VSCodeDark.INFO.r, VSCodeDark.INFO.g, VSCodeDark.INFO.b), font=font_value)
        # Mini network bar (24 px tall)
        canvas.progress_bar(4, y + 14, (SCREEN_W - 8) // 2 - 2, 4,
                            min(100, d['rx_rate_kbs'] / 100), VSCodeDark.SUCCESS, VSCodeDark.BG_PANEL)
        canvas.progress_bar(4 + (SCREEN_W - 8) // 2 + 2, y + 14, (SCREEN_W - 8) // 2 - 2, 4,
                            min(100, d['tx_rate_kbs'] / 100), VSCodeDark.WARNING, VSCodeDark.BG_PANEL)

        # Footer: CPU freq
        y = 144
        freq_text = f"CPU @ {d['cpu_freq']:.0f} MHz" if d['cpu_freq'] else ""
        if freq_text:
            draw.text((4, y), freq_text, fill=(VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b), font=font_value)
