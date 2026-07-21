"""Common widgets for page renderers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
# Python 3.8 ships `importlib.resources` without `files()` (added in 3.9);
# fall back to the backport so the package installs cleanly on 3.8 too.
try:
    from importlib.resources import files
except ImportError:  # pragma: no cover — only hit on Python < 3.9
    from importlib_resources import files  # type: ignore[no-redef]
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .canvas import Canvas
from .theme import Color, VSCodeDark, SCREEN_W, INDICATOR_H, TITLE_H


log = logging.getLogger("codebot.render.widgets")


# Font cache
_font_cache: dict[tuple[str, int], ImageFont.ImageFont] = {}


# Project-bundled font directory (DSEG 7-segment).
# importlib.resources 跟随 wheel 安装; 4 层 parent×4 只在开发模式 (-e)
# 才指向 <repo>/fonts/, pip install . 后会指向 site-packages/codebot/fonts
# 不存在 → font 加载失败。
_FONTS_DIR = files("codebot") / "fonts"


def get_font(name: str = "default", size: int = 12) -> ImageFont.ImageFont:
    """Get a font (with caching).

    Names:
      "default"  — PIL load_default (no asset needed)
      "mono"     — DejaVu Sans Mono / JetBrains Mono (whichever is installed)
      "bold"     — DejaVu Sans Mono Bold (heavier weight, same family as "mono")
      "digital"  — DSEG7-Classic-Bold (7-segment display, bundled in fonts/)
      "cjk"      — Noto Sans CJK SC (Simplified Chinese) for date / zh-CN labels
    """
    key = (name, size)
    if key in _font_cache:
        return _font_cache[key]

    font = None
    if name == "mono":
        # 三平台: 系统级字体目录 + 用户级字体目录
        candidates = [
            # Linux 系统级
            "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            # Linux 用户级 (per-user fontconfig)
            str(Path.home() / ".local/share/fonts/JetBrainsMono-Regular.ttf"),
            str(Path.home() / ".fonts/JetBrainsMono-Regular.ttf"),
            # macOS 系统级
            "/Library/Fonts/JetBrainsMono-Regular.ttf",
            "/System/Library/Fonts/Menlo.ttc",
            "/System/Library/Fonts/SFMono-Regular.otf",
            # macOS 用户级
            str(Path.home() / "Library/Fonts/JetBrainsMono-Regular.ttf"),
            # Windows 系统级 (C:\Windows\Fonts)
            "C:\\Windows\\Fonts\\consola.ttf",
            # Windows 用户级 (Win 10+ 字体商店安装位置)
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\consola.ttf"),
        ]
    elif name == "bold":
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            str(Path.home() / ".local/share/fonts/JetBrainsMono-Bold.ttf"),
            str(Path.home() / ".fonts/JetBrainsMono-Bold.ttf"),
            "/Library/Fonts/JetBrainsMono-Bold.ttf",
            "/System/Library/Fonts/SFMono-Bold.otf",
            str(Path.home() / "Library/Fonts/JetBrainsMono-Bold.ttf"),
            "C:\\Windows\\Fonts\\consolab.ttf",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\consolab.ttf"),
        ]
    elif name == "digital":
        # 7-segment display font, bundled with the project.
        # _FONTS_DIR 走 importlib.resources; pip install . 后路径跟随 wheel.
        # 不存在时回退到默认字体而非系统 DejaVu, 避免硬编码系统路径不一致。
        dseg_path = _FONTS_DIR / "DSEG7Classic-Bold.ttf"
        try:
            if dseg_path.is_file():
                candidates = [str(dseg_path)]
            else:
                log.warning(
                    "DSEG7 font not found at %s; falling back to default font",
                    dseg_path,
                )
                candidates = []  # 直接走末尾 load_default 兜底
        except OSError as e:
            log.warning("DSEG7 font lookup failed (%s); using default font", e)
            candidates = []
    elif name == "cjk":
        # CJK font for date / Chinese labels. TTC index is ignored by
        # Pillow's getbbox path; PIL picks the first face that has the
        # requested glyph, so the .ttc file works as-is on Linux/macOS.
        candidates = [
            # Linux 系统级
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            # Linux 用户级
            str(Path.home() / ".local/share/fonts/NotoSansCJK-Regular.ttc"),
            # macOS
            "/System/Library/Fonts/PingFang.ttc",
            "/Library/Fonts/Songti.ttc",
            # Windows
            "C:\\Windows\\Fonts\\msyh.ttc",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\msyh.ttc"),
        ]
    else:
        candidates = []

    for path in candidates:
        if Path(path).exists():
            try:
                font = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default(size=size)
    _font_cache[key] = font
    return font


def draw_indicator(canvas: Canvas, current_index: int, total: int) -> None:
    """Draw the 7-segment page indicator at top.

    current_index: 0-based page index
    total: total number of pages
    """
    if total == 0: return
    seg_w = SCREEN_W // total
    for i in range(total):
        x = i * seg_w
        color = VSCodeDark.INDICATOR_ACTIVE if i == current_index else VSCodeDark.INDICATOR_BASE
        canvas.paste_rect(color, x, 0, seg_w - 1, INDICATOR_H)  # -1 for gap


def draw_title(canvas: Canvas, title: str) -> None:
    """Draw the page title below the indicator bar."""
    y = INDICATOR_H + 2
    font = get_font("default", 14)
    draw = ImageDraw.Draw(canvas.image)
    draw.text((4, y), title, fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font)


def draw_hint(canvas: Canvas, hint: str) -> None:
    """Draw hint text at the bottom of the screen."""
    y = 172 - 8
    font = get_font("default", 9)
    draw = ImageDraw.Draw(canvas.image)
    draw.text((4, y), hint, fill=(VSCodeDark.FG_DIM.r, VSCodeDark.FG_DIM.g, VSCodeDark.FG_DIM.b), font=font)


def draw_progress_bar(
    canvas: Canvas,
    x: int, y: int, w: int, h: int,
    pct: float,
    label: str = "",
    value_text: str = "",
) -> None:
    """Draw a progress bar with optional label and value text."""
    canvas.progress_bar(x, y, w, h, pct, VSCodeDark.INFO, VSCodeDark.BG_PANEL)
    if label:
        font = get_font("mono", 11)
        draw = ImageDraw.Draw(canvas.image)
        draw.text((x, y - 12), label, fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font)
    if value_text:
        font = get_font("mono", 11)
        draw = ImageDraw.Draw(canvas.image)
        # right-align value text
        bbox = draw.textbbox((0, 0), value_text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((x + w - tw, y - 12), value_text, fill=(VSCodeDark.WARNING.r, VSCodeDark.WARNING.g, VSCodeDark.WARNING.b), font=font)


def draw_dotted_bar(
    canvas: Canvas,
    x: int, y: int, w: int, h: int,
    pct: float,
    fg: Color,
    bg: Color = Color(50, 50, 50),
    n_segments: int = 14,
    gap: int = 1,
) -> None:
    """Draw a segmented (dotted) horizontal bar — battery/level-meter style.

    `n_segments` cells across width `w`, each `cell_w` wide, separated by `gap` px.
    First `fill_n = round(pct / 100 * n_segments)` cells get `fg`, the rest get `bg`.
    """
    if w <= 0 or h <= 0 or n_segments <= 0:
        return
    # Each cell: integer width. Total occupied = n * cell_w + (n-1) * gap. Solve for cell_w.
    cell_w = max(1, (w - (n_segments - 1) * gap) // n_segments)
    fill_n = max(0, min(n_segments, round(pct / 100.0 * n_segments)))
    for i in range(n_segments):
        cx = x + i * (cell_w + gap)
        color = fg if i < fill_n else bg
        canvas.paste_rect(color, cx, y, cell_w, h)


def draw_text_right(
    canvas: Canvas,
    s: str,
    x_right: int,
    y: int,
    font: ImageFont.ImageFont,
    color: Color,
) -> int:
    """Draw text right-aligned so it ends at x_right. Returns the width drawn."""
    draw = ImageDraw.Draw(canvas.image)
    bbox = draw.textbbox((0, 0), s, font=font)
    tw = bbox[2] - bbox[0]
    draw.text((x_right - tw, y), s, fill=(color.r, color.g, color.b), font=font)
    return tw


def draw_text_centered(
    canvas: Canvas,
    s: str,
    cx: int,
    y: int,
    font: ImageFont.ImageFont,
    color: Color,
) -> int:
    """Draw text horizontally centered on cx. Returns the width drawn."""
    draw = ImageDraw.Draw(canvas.image)
    bbox = draw.textbbox((0, 0), s, font=font)
    tw = bbox[2] - bbox[0]
    draw.text((cx - tw // 2, y), s, fill=(color.r, color.g, color.b), font=font)
    return tw


def draw_value_with_unit(
    canvas: Canvas,
    digits: str,
    unit: str,
    x_right: int,
    y: int,
    digits_font: ImageFont.ImageFont,
    unit_font: ImageFont.ImageFont,
    digits_color: Color,
    unit_color: Color,
    max_digits_width: int,
) -> None:
    """Right-align a big numeric value with a small unit suffix.

    Layout: [digits (big)] [unit (small)], both right-aligned to x_right.
    The unit is rendered lower (y offset = digits_font.size - unit_font.size)
    so it sits on the same visual baseline as the digits.

    If `digits` doesn't fit `max_digits_width` even after shrinking the
    font, falls back to a single-line right-aligned render at a smaller size.

    Used by the dashboard tiles where the main value is in DSEG (digits
    font) and the unit (e.g. "%", "GHz", "°C") is in bold (unit font),
    since DSEG only ships the 10 digits.
    """
    draw = ImageDraw.Draw(canvas.image)
    rgb_d = (digits_color.r, digits_color.g, digits_color.b)
    rgb_u = (unit_color.r, unit_color.g, unit_color.b)

    if not unit:
        # No unit: just right-align the digits.
        bbox = draw.textbbox((0, 0), digits, font=digits_font)
        tw = bbox[2] - bbox[0]
        if tw > max_digits_width:
            small = get_font("bold", max(12, digits_font.size - 8))
            draw_text_right(canvas, digits, x_right, y, small, digits_color)
        else:
            draw_text_right(canvas, digits, x_right, y, digits_font, digits_color)
        return

    bw_u = draw.textbbox((0, 0), unit, font=unit_font)
    w_u = bw_u[2] - bw_u[0]
    y_unit = y + (digits_font.size - unit_font.size)

    # Unit sits at x_right, digits to its left.
    draw.text((x_right - w_u, y_unit), unit, fill=rgb_u, font=unit_font)
    x_digits_right = x_right - w_u - 4

    bw_d = draw.textbbox((0, 0), digits, font=digits_font)
    w_d = bw_d[2] - bw_d[0]
    if w_d + w_u + 4 > max_digits_width:
        # Doesn't fit side-by-side: shrink to a single line at smaller size.
        small = get_font("bold", max(12, digits_font.size - 8))
        draw_text_right(canvas, digits + unit, x_right, y, small, digits_color)
        return
    draw.text((x_digits_right - w_d, y), digits, fill=rgb_d, font=digits_font)
