"""Claude Code page: live state from ~/.code-bot/claude-state.json.

Layout (mirrors SystemPage, full-screen 2x2 + footer):
  y=0..72    STATUS (active/idle/stopped/error) | TOKENS IN (ctx in)
  y=72..144  TOKENS OUT (ctx out)               | CONTEXT (used % + bar)
  y=144..172 >_  Model  cwd/basename  $cost     (footer)

Data source: scripts/claude-statusline.py writes the state file from
Claude Code's statusline payload. Statusline has no event semantics
(no per-tool/per-prompt info), so we surface context window + cost +
model instead - the metrics a USB-screen glance is actually useful for.

Tiles use existing icons: status / down / up / context. No token-in /
token-out icons needed since the tokens shown here are context-window
sizes, not per-message flows.
"""

from __future__ import annotations

import os
from typing import Optional

from PIL import ImageDraw

from ...collectors.claude import ClaudeCollector, ClaudeSnapshot
from ..canvas import Canvas
from ..theme import VSCodeDark, SCREEN_W
from ..views.footer_view import FooterView
from ..views.tile_view import TileView
from ..views.warning_banner import WarningBannerView
from .base import BasePage


# Layout constants - identical to SystemPage (2x2 dashboard, full screen).
ROW1_Y = 0
ROW2_Y = 72
ROW_H = 72
FOOTER_Y = 144

CELL_W = SCREEN_W // 2

# Statusline version: 4-state enum (vs the 6-state hook version).
# Color choices match the rest of the UI's status vocabulary.
_STATUS_COLOR = {
    "active":  VSCodeDark.SUCCESS,
    "idle":    VSCodeDark.FG_DIM,
    "stopped": VSCodeDark.FG_DISABLED,
    "error":   VSCodeDark.DANGER,
}


def _truncate(s: Optional[str], n: int) -> str:
    if not s:
        return "—"
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_int(n: Optional[int]) -> str:
    return "0" if n is None else str(n)


def _fmt_pct(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return "{0:.0f}".format(n)


def _cwd_basename(cwd: str) -> str:
    if not cwd:
        return ""
    return os.path.basename(cwd.rstrip("/"))


def _footer_text(snap: ClaudeSnapshot) -> str:
    """Compose the footer line: model + cwd/basename (+ cost if room).

    Statusline doesn't give us per-event info, so we surface the most
    glanceable session metadata instead. The footer fits ~28 chars
    after the `>_` prompt, so we drop cost when model + cwd already
    fills the budget (cost is the least-glanceable of the three).
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
    """Real-time Claude Code dashboard (2x2 + footer)."""

    title = ""        # match SystemPage: empty title, page renders its own chrome
    skip_chrome = True

    def __init__(self, collector: Optional[ClaudeCollector] = None) -> None:
        self._collector = collector

    def render(self, canvas: Canvas) -> None:
        snap = self._collector.snapshot() if self._collector else None
        canvas.fill(VSCodeDark.BG)

        if snap is None:
            return

        self._draw_dividers(canvas)
        self._draw_tiles(canvas, snap)
        self._draw_footer(canvas, snap)
        if snap.status == "error":
            self._draw_error_banner(canvas, snap)

    # ---- sections (mirror SystemPage) ----

    @staticmethod
    def _draw_dividers(canvas: Canvas) -> None:
        d = ImageDraw.Draw(canvas.image)
        border = (VSCodeDark.BORDER.r, VSCodeDark.BORDER.g, VSCodeDark.BORDER.b)
        d.line(
            [(SCREEN_W // 2, ROW1_Y), (SCREEN_W // 2, ROW2_Y + ROW_H)],
            fill=border, width=1,
        )
        d.line([(0, ROW2_Y), (SCREEN_W, ROW2_Y)], fill=border, width=1)
        d.line(
            [(0, ROW2_Y + ROW_H), (SCREEN_W, ROW2_Y + ROW_H)],
            fill=border, width=1,
        )

    @staticmethod
    def _draw_tiles(canvas: Canvas, snap: ClaudeSnapshot) -> None:
        color = _STATUS_COLOR.get(snap.status, VSCodeDark.FG_DIM)

        # ---- STATUS tile (state name, 18 pt bold - fits "stopped") ----
        # TileView's overflow lesson: "permission" (10 chars at 36 pt bold)
        # overflowed the 144 px value region. We use 18 pt bold so all 4
        # status names fit. The STATUS title underneath already labels
        # the row, so the small name reads as a state badge.
        TileView(
            x=0, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="status", icon_color=color,
            title="STATUS", title_color=color,
            value_digits=snap.status,
            value_unit="",
            value_color=color,
            value_font="bold",
            value_font_size=18,
        ).draw(canvas)

        # ---- TOKENS IN (current context window input tokens) ----
        TileView(
            x=CELL_W, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="down", icon_color=VSCodeDark.INFO,
            title="CTX IN", title_color=VSCodeDark.INFO,
            value_digits=_fmt_int(snap.context_in),
            value_unit="",
            value_color=VSCodeDark.FG,
            value_font="digital",
        ).draw(canvas)

        # ---- TOKENS OUT (current context window output tokens) ----
        TileView(
            x=0, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="up", icon_color=VSCodeDark.WARNING,
            title="CTX OUT", title_color=VSCodeDark.WARNING,
            value_digits=_fmt_int(snap.context_out),
            value_unit="",
            value_color=VSCodeDark.FG,
            value_font="digital",
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
            x=CELL_W, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="context", icon_color=bar_color,
            title="CONTEXT", title_color=bar_color,
            value_digits=_fmt_pct(pct),
            value_unit="%" if pct is not None else "",
            value_color=bar_color,
            value_font="digital",
            bar_pct=bar_pct,
            bar_color=bar_color,
        ).draw(canvas)

    @staticmethod
    def _draw_footer(canvas: Canvas, snap: ClaudeSnapshot) -> None:
        FooterView(
            y=FOOTER_Y,
            items=[
                {
                    "icon": "terminal",
                    "value": _footer_text(snap),
                    "color": _STATUS_COLOR.get(snap.status, VSCodeDark.FG_DIM),
                },
            ],
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