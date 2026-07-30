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

import time
from typing import Optional

from PIL import ImageDraw

from ...collectors.github import GithubCollector, GithubSnapshot
from ..canvas import Canvas
from ..theme import VSCodeDark, SCREEN_W
from ..views.footer_view import FooterView
from ..views.tile_view import TileView
from ..views.warning_banner import WarningBannerView
from .base import BasePage


# Layout constants: same 2x2 grid geometry as SystemPage.
ROW1_Y = 0
ROW2_Y = 72
ROW_H = 72
FOOTER_Y = 144

CELL_W = SCREEN_W // 2  # 160


# ---- Formatting helpers ----

def _fmt_stars(n: Optional[int]) -> tuple[str, str]:
    """Format star count as (digits, unit). Unit is 'k' if >= 1000.

    DSEG (the 7-seg font used for digits) has a decimal point, but it's
    only 6×5 px and easy to misread at 1.47". We always round to an
    integer k to keep the row clean. < 1000 is shown as the raw count.

    Missing values render as ``"0"`` so the page looks "ready" at
    startup (before the first refresh) instead of placeholder boxes.
    """
    if n is None:
        return "0", ""
    if n < 1000:
        return str(n), ""
    k = round(n / 1000.0)
    return "{0:.0f}".format(k), "k"


def _fmt_int(n: Optional[int]) -> str:
    """Format a count for the value row. ``None`` → ``"0"`` (not "—")
    so the page reads as ready/zero at startup, not as a broken
    placeholder box."""
    return "0" if n is None else str(n)


# ---- Event footer ----

# Map GitHub event type → short verb shown in the footer.
_EVENT_VERB = {
    "PushEvent":         "Push",
    "CreateEvent":       "Create",
    "DeleteEvent":       "Del",
    "PullRequestEvent":  "PR",
    "IssuesEvent":       "Issue",
    "WatchEvent":        "Star",
    "ForkEvent":         "Fork",
    "ReleaseEvent":      "Rel",
    "PublicEvent":       "Public",
    "MemberEvent":       "Member",
    "GollumEvent":       "Wiki",
}
_DEFAULT_EVENT_VERB = "Event"


def _fmt_age(ts: Optional[float]) -> str:
    """Format a unix-timestamp as a short relative time ('2 min ago',
    '3 hr ago', '4 day ago'). Renders '—' for None or future ts."""
    if ts is None:
        return "—"
    s = int(time.time() - ts)
    if s < 0:
        return "now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} hr ago"
    if s < 86400 * 7:
        return f"{s // 86400} day ago"
    return f"{s // 86400} d ago"


def _event_footer_item(snap: Optional[GithubSnapshot]) -> dict:
    """Build a FooterView item dict for the user's latest GitHub event.

    Renders as ``Push main 2 min ago`` etc. — verb (colored by type) +
    object (branch / title / etc.) + relative age.
    """
    if snap is None or snap.latest_event_type is None:
        return {"icon": "context", "value": "—", "color": VSCodeDark.FG_DIM}

    verb = _EVENT_VERB.get(snap.latest_event_type, _DEFAULT_EVENT_VERB)
    obj = snap.latest_event_object or "—"
    # Trim object to keep the row short enough to fit (~280px).
    obj = obj[:12]
    age = _fmt_age(snap.latest_event_ts)

    # Color by event type so the eye can pick it out at a glance.
    color = {
        "PushEvent":        VSCodeDark.SUCCESS,
        "CreateEvent":      VSCodeDark.INFO,
        "PullRequestEvent": VSCodeDark.SYN_FUNC,
        "WatchEvent":       VSCodeDark.WARNING,
        "ForkEvent":        VSCodeDark.WARNING,
        "ReleaseEvent":     VSCodeDark.WARNING,
    }.get(snap.latest_event_type, VSCodeDark.FG_DIM)

    return {"icon": "context", "value": f"{verb} {obj} {age}", "color": color}


# ---- Warning banner ----
#
# Map a token_status value → (title, hint, accent). The banner is
# rendered last (on top of the tiles) when the snapshot's token isn't
# usable. We keep the tile grid visible underneath so the user can see
# *which* data is missing, while the banner calls attention to the
# fix-it step.
_WARNING_BY_STATUS = {
    "no_token": (
        "GITHUB_TOKEN not set",
        "Set $GITHUB_TOKEN env or pages.github.token in ~/.code_bot/config.yml",
        VSCodeDark.WARNING,
    ),
    "bad_auth": (
        "GitHub token rejected",
        "401 Bad Credentials — token expired or revoked. "
        "Regenerate at github.com/settings/tokens",
        VSCodeDark.DANGER,
    ),
}


class GithubPage(BasePage):
    """GitHub stats dashboard. Shares a GithubCollector with the daemon."""

    title = "GitHub"
    # Skip the daemon chrome (top page-indicator bar + title). The page
    # fills the entire screen, matching SystemPage's layout.
    skip_chrome = True

    def __init__(self, collector: Optional[GithubCollector] = None) -> None:
        self._collector = collector
        # Track the last snap we printed so the daemon log doesn't
        # spam once per render frame. None means "haven't printed yet".
        self._last_snap_dump: Optional[tuple] = None

    def render(self, canvas: Canvas) -> None:
        snap = self._collector.snapshot() if self._collector is not None else None
        self._dump_snap(snap)
        canvas.fill(VSCodeDark.BG)

        self._draw_dividers(canvas)
        self._draw_tiles(canvas, snap)
        self._draw_footer(canvas, snap)
        # Warning overlay must be drawn LAST so it sits on top of the
        # tile grid. We keep the grid visible underneath so the user
        # can see *which* fields are unpopulated; the banner just calls
        # attention to the underlying credential issue.
        self._draw_warning(canvas, snap)

    def _dump_snap(self, snap: Optional[GithubSnapshot]) -> None:
        """Print ``snap`` to stdout, but only when it changes.

        The page renders at 8 fps in sim mode, so an unconditional
        print would flood the log. We hash the interesting fields and
        print on first call + on every subsequent change.
        """
        if snap is None:
            key = ("__none__",)
        else:
            key = (snap.user, snap.stars, snap.followers,
                   snap.commits_today, snap.open_prs,
                   snap.latest_event_type, snap.latest_event_object,
                   snap.latest_event_repo, snap.latest_event_ts)
        if key == self._last_snap_dump:
            return
        self._last_snap_dump = key
        # Use the dataclass repr so the field order matches the source.
        print(f"[GithubPage] snap = {snap!r}")

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
        TileView(
            x=0, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="stars", icon_color=VSCodeDark.WARNING,
            title="Star", title_color=VSCodeDark.WARNING,
            value_digits=sd, value_unit=su
        ).draw(canvas)

        # ---- FOLLOWERS (top-right): count of people following the user ----
        TileView(
            x=CELL_W, y=ROW1_Y, w=CELL_W, h=ROW_H,
            icon="follow", icon_color=VSCodeDark.INFO,
            title="Follow", title_color=VSCodeDark.INFO,
            value_digits=_fmt_int(snap.followers if snap else None),
            value_unit="",
        ).draw(canvas)

        # ---- COMMITS (bottom-left): today's commit count + unit "Today" ----
        TileView(
            x=0, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="commits", icon_color=VSCodeDark.SUCCESS,
            title="Commit", title_color=VSCodeDark.SUCCESS,
            value_digits=_fmt_int(snap.commits_today if snap else None),
            value_unit="Today",
        ).draw(canvas)

        # ---- PRS (bottom-right): open PR count + unit "Open" ----
        TileView(
            x=CELL_W, y=ROW2_Y, w=CELL_W, h=ROW_H,
            icon="prs", icon_color=VSCodeDark.SYN_FUNC,
            title="PR", title_color=VSCodeDark.SYN_FUNC,
            value_digits=_fmt_int(snap.open_prs if snap else None),
            value_unit="Open",
        ).draw(canvas)

    @staticmethod
    def _draw_footer(canvas: Canvas, snap: Optional[GithubSnapshot]) -> None:
        FooterView(
            y=FOOTER_Y,
            items=[_event_footer_item(snap)],
        ).draw(canvas)

    @staticmethod
    def _draw_warning(canvas: Canvas, snap: Optional[GithubSnapshot]) -> None:
        """Render the warning overlay if the token isn't usable.

        No banner for transient network errors — those pass through
        silently and the page keeps showing whatever data was last
        successfully fetched. Only credential problems (missing or
        rejected token) get the overlay, since those won't fix
        themselves without user action.
        """
        status = snap.token_status if snap is not None else "ok"
        spec = _WARNING_BY_STATUS.get(status)
        if spec is None:
            return
        title, hint, accent = spec
        WarningBannerView(title=title, hint=hint, accent=accent).draw(canvas)
