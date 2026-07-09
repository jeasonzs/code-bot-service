"""GitHub page: 2x2 dashboard of GitHub stats (Stars / Streak / Commits /
PRs) plus a footer showing the latest CI status.

Layout (mirrors SystemPage, full-screen 2x2 grid):

    y=0-72    STARS  (k)         | STREAK (Days)        [always "—" for now]
    y=72-144  COMMITS (Today)    | PRS (Open)
    y=148-172  >_  [status]  ok/fail/run/wait #N <workflow>

Renders ``—`` when GITHUB_TOKEN is missing or any field fails to fetch.
The page reads data from a shared ``GithubCollector`` (background
thread, refreshed every 60s).
"""

from __future__ import annotations

from typing import Optional

from PIL import ImageDraw

from ...collectors.github import GithubCollector, GithubSnapshot
from ..canvas import Canvas
from ..theme import VSCodeDark, SCREEN_W
from ..views.footer_view import FooterView
from ..views.tile_view import TileView
from .base import BasePage


# Layout constants: same 2x2 grid geometry as SystemPage.
ROW1_Y = 0
ROW2_Y = 72
ROW_H = 72
FOOTER_Y = 148

CELL_W = SCREEN_W // 2  # 160


# ---- Formatting helpers ----

def _fmt_stars(n: Optional[int]) -> tuple[str, str]:
    """Format star count as (digits, unit). Unit is 'k' if >= 1000."""
    if n is None:
        return "—", ""
    if n < 1000:
        return str(n), ""
    k = n / 1000.0
    if k < 10:
        return "{0:.1f}".format(k), "k"
    return "{0:.0f}".format(k), "k"


def _fmt_int(n: Optional[int]) -> str:
    return "—" if n is None else str(n)


# ---- CI footer ----

# Map GitHub status/conclusion → text color used in the footer item.
_CI_COLORS = {
    "success":         VSCodeDark.SUCCESS,
    "failure":         VSCodeDark.DANGER,
    "timed_out":       VSCodeDark.DANGER,
    "in_progress":     VSCodeDark.INFO,
    "queued":          VSCodeDark.FG_DIM,
    "action_required": VSCodeDark.WARNING,
    "cancelled":       VSCodeDark.FG_DIM,
    "neutral":         VSCodeDark.FG_DIM,
    "skipped":         VSCodeDark.FG_DIM,
}
_DEFAULT_CI_COLOR = VSCodeDark.FG_DIM

# Status word rendered as the footer value. Keep short — the footer
# row is only ~280px wide and the workflow name follows.
_CI_LABEL = {
    "success":         "ok",
    "failure":         "fail",
    "timed_out":       "fail",
    "in_progress":     "run",
    "queued":          "wait",
    "action_required": "req",
    "cancelled":       "cncl",
    "neutral":         "skip",
    "skipped":         "skip",
}
_DEFAULT_CI_LABEL = "ci"


def _ci_footer_item(snap: Optional[GithubSnapshot]) -> dict:
    """Build a FooterView item dict for the latest CI run."""
    if snap is None or snap.ci_status is None:
        return {"icon": "status", "value": "—", "color": VSCodeDark.FG_DIM}

    color = _CI_COLORS.get(snap.ci_status, _DEFAULT_CI_COLOR)
    label = _CI_LABEL.get(snap.ci_status, _DEFAULT_CI_LABEL)
    wf = (snap.ci_workflow or "ci").split(" ")[0][:8]  # trim long names
    if snap.ci_run_number is not None:
        value = f"{label} #{snap.ci_run_number} {wf}"
    else:
        value = f"{label} {wf}"
    return {"icon": "status", "value": value, "color": color}


class GithubPage(BasePage):
    """GitHub stats dashboard. Shares a GithubCollector with the daemon."""

    title = "GitHub"
    # Skip the daemon chrome (top page-indicator bar + title). The page
    # fills the entire screen, matching SystemPage's layout.
    skip_chrome = True

    def __init__(self, collector: Optional[GithubCollector] = None) -> None:
        self._collector = collector

    def render(self, canvas: Canvas) -> None:
        snap = self._collector.snapshot() if self._collector is not None else None
        canvas.fill(VSCodeDark.BG)

        self._draw_dividers(canvas)
        self._draw_tiles(canvas, snap)
        self._draw_footer(canvas, snap)

    # ---- Sections ----

    @staticmethod
    def _draw_dividers(canvas: Canvas) -> None:
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
    def _draw_tiles(canvas: Canvas, snap: Optional[GithubSnapshot]) -> None:
        # ---- STARS (top-left): digits "1.2" + unit "k", bar to 10k ----
        sd, su = _fmt_stars(snap.stars if snap else None)
        bar_pct = None
        if snap is not None and snap.stars:
            # Cap at 10k stars for the bar (so 10k+ shows 100%).
            bar_pct = max(0.0, min(100.0, (snap.stars / 10000.0) * 100.0))
        TileView(
            x=0, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="stars", icon_color=VSCodeDark.WARNING,
            title="STARS", title_color=VSCodeDark.WARNING,
            value_digits=sd, value_unit=su,
            bar_pct=bar_pct, bar_color=VSCodeDark.WARNING,
        ).draw(canvas)

        # ---- STREAK (top-right): always "—" (per user decision) ----
        TileView(
            x=CELL_W, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="streak", icon_color=VSCodeDark.INFO,
            title="STREAK", title_color=VSCodeDark.INFO,
            value_digits="—", value_unit="",
        ).draw(canvas)

        # ---- COMMITS (bottom-left): today's commit count + unit "Today" ----
        TileView(
            x=0, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="commits", icon_color=VSCodeDark.SUCCESS,
            title="COMMITS", title_color=VSCodeDark.SUCCESS,
            value_digits=_fmt_int(snap.commits_today if snap else None),
            value_unit="Today",
        ).draw(canvas)

        # ---- PRS (bottom-right): open PR count + unit "Open" ----
        TileView(
            x=CELL_W, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="prs", icon_color=VSCodeDark.SYN_FUNC,
            title="PRS", title_color=VSCodeDark.SYN_FUNC,
            value_digits=_fmt_int(snap.open_prs if snap else None),
            value_unit="Open",
        ).draw(canvas)

    @staticmethod
    def _draw_footer(canvas: Canvas, snap: Optional[GithubSnapshot]) -> None:
        FooterView(
            y=FOOTER_Y,
            items=[_ci_footer_item(snap)],
        ).draw(canvas)
