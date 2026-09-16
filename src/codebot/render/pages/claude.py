"""Claude Code page: live state from ~/.code-bot/claude-state.json.

Layout (mirrors SystemPage, full-screen 2x2 + top header):
  y=0..28    >_ <title>           <model>  <cwd>  <$cost>  (HeaderView)
  ── divider at y=28 ──
  y=28..100  STATUS (active/idle/stopped/error) | CONTEXT (used % + bar)
  ── divider at y=100 ──
  y=100..172 IN (ctx in)                        | OUT (ctx out)

Data source: scripts/claude-statusline.py writes the state file from
Claude Code's statusline payload. Statusline has no event semantics
(no per-tool/per-prompt info), so we surface context window + cost +
model instead - the metrics a USB-screen glance is actually useful for.

Tiles use existing icons: status / context / down / up. No token-in /
token-out icons needed since the tokens shown here are context-window
sizes, not per-message flows.
"""

from __future__ import annotations

import os
from typing import Optional

from PIL import ImageDraw

from ...collectors.claude import ClaudeCollector, ClaudeSnapshot
from ..canvas import Canvas
from ..theme import HEADER_H, VSCodeDark, SCREEN_W
from ..views.header_view import HeaderView
from ..views.tile_view import TileView
from ..views.warning_banner import WarningBannerView
from .base import BasePage


# Layout constants - identical to SystemPage (2x2 dashboard below header).
# HEADER_H (28) + 2 × ROW_H (72) = 172 fills the screen exactly.
ROW_H = 72
ROW1_Y = HEADER_H
ROW2_Y = HEADER_H + ROW_H

CELL_W = SCREEN_W // 2

# 6-state status enum (from claude-status-hook.py events).
# Color choices match the rest of the UI's status vocabulary.
_STATUS_COLOR = {
    "idle":       VSCodeDark.FG_DIM,
    "thinking":   VSCodeDark.SUCCESS,
    "tool":       VSCodeDark.INFO,
    "permission": VSCodeDark.WARNING,
    "stopped":    VSCodeDark.FG_DISABLED,
    "error":      VSCodeDark.DANGER,
}

# Short LCD labels for the 6-state status enum. The collector stores
# full names (semantic clarity, easier to grep / debug) but the STATUS
# tile only has room for ~4 chars at 18 pt bold without risking
# overflow on tight rows. "perm" and "stop" are unambiguous in the
# context of the colored badge.
_STATUS_SHORT = {
    "idle":       "idle",
    "thinking":   "think",
    "tool":       "tool",
    "permission": "perm",
    "stopped":    "stop",
    "error":      "error",
}


def _truncate(s: Optional[str], n: int) -> str:
    if not s:
        return "—"
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_int(n: Optional[int]) -> str:
    return "0" if n is None else str(n)


def _fmt_token_count(n: Optional[int]) -> str:
    """Format a token count with 3 significant digits.

    Returns only the numeric portion (no unit). Pair with ``_token_unit``
    to render the K/M/G/T suffix separately so TileView can lay it out
    in its own smaller style block.

    Rules:
      - None -> "—" (so empty / unparsed state still shows clearly)
      - < 1000 -> integer as-is                  e.g. 812 -> "812"
      - < 1e6  -> divide by 1e3, at most 1 dec   15500 -> "15.5", 1234 -> "1.23"
      - < 1e9  -> divide by 1e6, at most 1 dec
      - < 1e12 -> divide by 1e9, at most 1 dec
      - else   -> divide by 1e12, at most 1 dec

    "3 sig figs with at most 1 decimal": we format with ``{:.1f}`` and
    then trim a trailing ``.0`` so 15000 -> "15" (cleaner on the LCD).
    """
    if n is None:
        return "—"
    if n < 0:
        return "-" + _fmt_token_count(-n)
    if n < 1000:
        return str(n)

    scales = (
        (1_000_000_000_000, 1_000_000_000_000),
        (1_000_000_000,     1_000_000_000),
        (1_000_000,         1_000_000),
        (1_000,             1_000),
    )
    for divisor, _ in scales:
        if n >= divisor:
            value = n / divisor
            s = ("{0:.1f}").format(value)
            if s.endswith(".0"):
                s = s[:-2]
            return s
    return str(n)


def _token_unit(n: Optional[int]) -> str:
    """Unit suffix matching ``_fmt_token_count``: "" / "K" / "M" / "G" / "T".

    Returns "" for None and for n < 1000 (so the tile shows just "812"
    without a stray suffix). Pairs with _fmt_token_count to keep the
    numeric and unit pieces separate for TileView styling.
    """
    if n is None or n < 1000:
        return ""
    if n < 1_000_000:
        return "K"
    if n < 1_000_000_000:
        return "M"
    if n < 1_000_000_000_000:
        return "G"
    return "T"


def _fmt_pct(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return "{0:.0f}".format(n)


def _cwd_basename(cwd: str) -> str:
    if not cwd:
        return ""
    return os.path.basename(cwd.rstrip("/"))


def _footer_text(snap: ClaudeSnapshot) -> str:
    """Compose the header subtitle line: model + cwd/basename (+ cost).

    Reused from the previous footer implementation. The header subtitle
    has more horizontal room than the old 28-char footer cap, but we
    keep the same budget so the line still reads as a glanceable
    one-liner instead of a paragraph.
    """
    model = snap.model_display.strip()
    cwd = _cwd_basename(snap.cwd)
    cost_s = "${0:.2f}".format(snap.cost_usd) if snap.cost_usd is not None else None

    # Try with all three; fall back to model+cwd; then model only.
    candidates: list[list[str]] = []
    base = [p for p in (model, cwd) if p]
    if cost_s:
        candidates.append(base + [cost_s])
    candidates.append(base)
    if model:
        candidates.append([model])

    for cand in candidates:
        text = "  ".join(cand)
        if len(text) <= 28:
            return text

    # Pathological fallback (shouldn't happen, model names > 28 chars).
    if model:
        return _truncate(model, 28)
    if snap.status == "error":
        return "error"
    return "—"


class ClaudePage(BasePage):
    """Real-time Claude Code dashboard (2x2 + top header)."""

    title = ""        # class-level fallback; daemon stamps entry name
    skip_chrome = True

    def __init__(
        self,
        collector: Optional[ClaudeCollector] = None,
        *,
        title: str = "Claude",
    ) -> None:
        self._collector = collector
        self._title = title

    def render(self, canvas: Canvas) -> None:
        snap = self._collector.snapshot() if self._collector else None
        canvas.fill(VSCodeDark.BG)

        if snap is None:
            self._draw_header(canvas, None)
            return

        self._draw_header(canvas, snap)
        self._draw_dividers(canvas)
        self._draw_tiles(canvas, snap)
        if snap.status == "error":
            self._draw_error_banner(canvas, snap)

    # ---- sections (mirror SystemPage) ----

    def _draw_header(self, canvas: Canvas, snap: Optional[ClaudeSnapshot]) -> None:
        subtitle = _footer_text(snap) if snap is not None else "—"
        subtitle_color = (
            _STATUS_COLOR.get(snap.status, VSCodeDark.FG_DIM)
            if snap is not None
            else VSCodeDark.FG_DIM
        )
        HeaderView(
            title=self._title,
            subtitle=subtitle,
            subtitle_color=subtitle_color,
        ).draw(canvas)

    @staticmethod
    def _draw_dividers(canvas: Canvas) -> None:
        d = ImageDraw.Draw(canvas.image)
        border = (VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b)
        # Header / body separator.
        d.line(
            [(0, HEADER_H), (SCREEN_W, HEADER_H)],
            fill=border, width=1,
        )
        # Vertical line between left and right tiles.
        d.line(
            [(SCREEN_W // 2, ROW1_Y), (SCREEN_W // 2, ROW2_Y + ROW_H)],
            fill=border, width=1,
        )
        # Horizontal line between row 1 and row 2.
        d.line([(0, ROW2_Y), (SCREEN_W, ROW2_Y)], fill=border, width=1)

    def _draw_tiles(self, canvas: Canvas, snap: ClaudeSnapshot) -> None:
        color = _STATUS_COLOR.get(snap.status, VSCodeDark.FG_DIM)
        # Map the 6-state enum to short LCD labels (idle / think / tool /
        # perm / stop / error). Full names stay in the collector's
        # status field for debuggability; only the rendered text shrinks.
        status_label = _STATUS_SHORT.get(snap.status, snap.status)

        # ---- STATUS tile (short state name, 24 pt bold) ----
        # TileView's overflow lesson: "permission" (10 chars at 36 pt bold)
        # overflowed the 144 px value region. With short labels the worst
        # case is "error" / "think" (5 chars); 24 pt bold comfortably
        # fits and reads as a real state badge instead of a tiny label.
        TileView(
            x=0, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="status", icon_color=color,
            title="STATUS", title_color=color,
            value_digits=status_label,
            value_unit="",
            value_color=color,
            value_font="bold",
            value_font_size=24,
        ).draw(canvas)

        # ---- CONTEXT tile (used % as number + dotted bar) ----
        # Color the bar green/yellow/red based on the same thresholds
        # used by SystemPage's CPU/MEM bars (<70 / 70-89 / >=90).
        pct = snap.context_used_pct
        if pct is None:
            bar_pct: Optional[float] = None
            bar_color = VSCodeDark.FG_DIM
        else:
            bar_pct = max(0.0, min(100.0, pct))
            if pct >= 90:
                bar_color = VSCodeDark.DANGER
            elif pct >= 70:
                bar_color = VSCodeDark.WARNING
            else:
                bar_color = VSCodeDark.SUCCESS

        TileView(
            x=CELL_W, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="context", icon_color=bar_color,
            title="CTX", title_color=bar_color,
            value_digits=_fmt_pct(pct),
            value_unit="%" if pct is not None else "",
            value_color=bar_color,
            value_font="digital",
            bar_pct=bar_pct,
            bar_color=bar_color,
        ).draw(canvas)

        # ---- IN tile (current context window input tokens) ----
        # value_digits carries the numeric portion; value_unit carries the
        # K/M/G/T suffix so TileView can lay them out in separate style
        # blocks (suffix is smaller / dimmer). value_color matches
        # title_color so the whole tile reads as one INFO blue badge.
        TileView(
            x=0, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="down", icon_color=VSCodeDark.INFO,
            title="IN", title_color=VSCodeDark.INFO,
            value_digits=_fmt_token_count(snap.context_in),
            value_unit=_token_unit(snap.context_in),
            value_color=VSCodeDark.INFO,
            value_font="digital",
        ).draw(canvas)

        # ---- OUT tile (current context window output tokens) ----
        # value_color matches title_color (WARNING) so the whole tile
        # reads as one yellow badge - distinguishes OUT from IN at a
        # glance without needing different icons.
        TileView(
            x=CELL_W, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="up", icon_color=VSCodeDark.WARNING,
            title="OUT", title_color=VSCodeDark.WARNING,
            value_digits=_fmt_token_count(snap.context_out),
            value_unit=_token_unit(snap.context_out),
            value_color=VSCodeDark.WARNING,
            value_font="digital",
        ).draw(canvas)

    @staticmethod
    def _draw_error_banner(canvas: Canvas, snap: ClaudeSnapshot) -> None:
        msg = snap.error or "state file unreadable"
        hint = _truncate(msg, 60)
        WarningBannerView(
            title="Claude state error",
            hint=hint,
            accent=VSCodeDark.DANGER,
            icon="status",
        ).draw(canvas)