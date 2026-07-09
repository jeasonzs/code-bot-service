"""GitHub API collector: user activity, total repo stars, latest CI run.

Pulls data from api.github.com using a personal access token. Token
lookup order (first match wins):

  1. ``$GITHUB_TOKEN`` environment variable (12-factor / CI override)
  2. ``Config`` (defaults to ``~/.code_bot/config.yml``)

Designed to run as a background thread (one full refresh every
``refresh_interval`` seconds) and degrade gracefully when no token is
available or the API is unreachable — in that case ``snapshot()``
returns a snapshot with all fields ``None`` and the page renders ``—``
for every tile.

Fetching strategy
-----------------
- All fields are refreshed together in one ``_refresh()`` tick. A single
  snapshot keeps the tiles coherent (no "5 stars but 0 PRs" while the
  next API call is in flight).
- HTTP errors are logged and the previous snapshot is retained. The
  daemon never crashes on a GitHub outage.
- Default refresh = 60s (the GitHub API allows 5000 req/h with a token).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


log = logging.getLogger("codebot.github")


GITHUB_API = "https://api.github.com"
# Cap on repos enumerated when summing stars (10 pages * 100/page = 1000).
_STARS_MAX_PAGES = 10


@dataclass
class GithubSnapshot:
    user: Optional[str]            # login of the authenticated user
    stars: Optional[int]           # sum of stargazers_count across user's repos
    followers: Optional[int]       # people following the user (from /user.followers)
    commits_today: Optional[int]   # commits authored by user since 00:00 UTC
    open_prs: Optional[int]        # open PRs authored by user
    # Latest public event in the user's activity feed (from
    # /users/{user}/events). The page renders these into a one-liner
    # like "Push main 2 min ago".
    latest_event_type: Optional[str]    # "PushEvent" / "PullRequestEvent" / ...
    latest_event_object: Optional[str]  # branch / PR title / repo name, etc.
    latest_event_repo: Optional[str]    # short repo name, e.g. "code-bot-service"
    latest_event_ts: Optional[float]    # unix time of the event
    ts: float                           # unix time of the snapshot
    # Token/credential health. The page renders a Warning overlay when
    # this is anything other than "ok"; transient errors don't trip it.
    #   "ok"        — last API call succeeded (or no calls yet because
    #                 token is unset; see "no_token" below)
    #   "no_token"  — token is empty (env or config). Page shows
    #                 "GITHUB_TOKEN not set" warning.
    #   "bad_auth"  — GitHub returned HTTP 401 (bad/expired/revoked
    #                 PAT). Page shows "Bad credentials" warning.
    #   "transient" — network/timeout/5xx — page keeps showing the
    #                 last known data without a warning overlay.
    token_status: str = "ok"
    token_error: Optional[str] = None  # short human-readable hint


def _empty_snapshot() -> GithubSnapshot:
    return GithubSnapshot(
        user=None, stars=None, followers=None,
        commits_today=None, open_prs=None,
        latest_event_type=None, latest_event_object=None,
        latest_event_repo=None, latest_event_ts=None,
        ts=0.0,
        token_status="ok", token_error=None,
    )


def _event_unix_ts(iso: Optional[str]) -> Optional[float]:
    """Parse a GitHub ISO 8601 timestamp into a unix float (seconds)."""
    if not iso:
        return None
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return ct.timestamp()
    except (ValueError, TypeError):
        return None


class GithubAuthError(Exception):
    """Raised by :meth:`GithubCollector._get` when GitHub returns HTTP 401.

    Callers (currently only :meth:`GithubCollector._refresh`) should
    catch this, mark the snapshot's ``token_status`` as ``"bad_auth"``,
    and keep the previous snapshot's data (or empty values). A 401 is
    NOT a transient error — it won't fix itself until the user updates
    the token.
    """


class GithubCollector:
    """Background thread that refreshes GitHub stats every ``refresh_interval``."""

    def __init__(self, refresh_interval: float = 60.0,
                 config: Optional["Config"] = None) -> None:
        self.refresh_interval = refresh_interval
        self._lock = threading.Lock()
        self._latest: GithubSnapshot = _empty_snapshot()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Token resolution: env > config file > empty.
        if config is None:
            from ..config import Config
            config = Config()
        env_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
        cfg_token = config.get("github", "token") or ""
        self._token = env_token or cfg_token
        if not env_token and cfg_token:
            log.info("Loaded GitHub token from %s", config.path)

        # If no token is configured, mark the initial snapshot so the
        # page can render a Warning immediately (instead of waiting for
        # the first refresh tick to fail).
        if not self._token:
            self._latest = GithubSnapshot(
                user=None, stars=None, followers=None,
                commits_today=None, open_prs=None,
                latest_event_type=None, latest_event_object=None,
                latest_event_repo=None, latest_event_ts=None,
                ts=0.0,
                token_status="no_token",
                token_error="Set GITHUB_TOKEN env or github.token in ~/.code_bot/config.yml",
            )

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="github-collector", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def snapshot(self) -> GithubSnapshot:
        """Get the latest snapshot. Returns a copy to avoid races."""
        with self._lock:
            return GithubSnapshot(**self._latest.__dict__)

    # ---- Background thread ----

    def _run(self) -> None:
        if not self._token:
            log.warning("GITHUB_TOKEN not set; GithubPage will show '—'")
            return  # never tick — keep the all-None snapshot
        # One immediate refresh so the page has data on first render.
        self._refresh()
        while not self._stop.is_set():
            self._stop.wait(self.refresh_interval)
            if self._stop.is_set():
                break
            self._refresh()

    def _refresh(self) -> None:
        # /user returns the full profile — login, followers count, etc.
        # One call gives us both `user` and `followers`; no need to
        # hit /users/{login} separately.
        try:
            me = self._fetch_user_profile()
            if me is None:
                # Transient error (network / 5xx / etc.) — keep the
                # previous snapshot's data but mark token_status as
                # "transient" so the page can show a soft hint if it
                # wants to. For now we don't render a banner for
                # transient errors, so we just leave status as "ok".
                return
            user = me.get("login")
            followers = int(me.get("followers", 0) or 0)

            stars = self._fetch_total_stars(user)
            commits_today = self._fetch_commits_today(user)
            open_prs = self._fetch_open_prs(user)
            ev = self._fetch_latest_event(user)

            snap = GithubSnapshot(
                user=user, stars=stars, followers=followers,
                commits_today=commits_today, open_prs=open_prs,
                latest_event_type=ev[0] if ev else None,
                latest_event_object=ev[1] if ev else None,
                latest_event_repo=ev[2] if ev else None,
                latest_event_ts=ev[3] if ev else None,
                ts=time.time(),
                token_status="ok", token_error=None,
            )
            with self._lock:
                self._latest = snap
        except GithubAuthError as e:
            # 401 Bad Credentials — keep previous data but flag the
            # snapshot so the page can show a Warning banner.
            log.warning("GitHub auth failure: %s", e)
            with self._lock:
                prev = self._latest
                self._latest = GithubSnapshot(
                    **prev.__dict__,
                    token_status="bad_auth",
                    token_error="GitHub token rejected (401). "
                                "Check ~/.code_bot/config.yml or "
                                "$GITHUB_TOKEN.",
                )

    # ---- HTTP ----

    def _get(self, path: str) -> Optional[object]:
        """GET ``path`` (relative to GITHUB_API, or absolute URL) and return
        the parsed JSON. ``None`` on any error (logged at WARNING).

        HTTP 401 (Bad Credentials) is propagated via the
        :class:`GithubAuthError` exception so :meth:`_refresh` can flag
        the snapshot's ``token_status`` as ``"bad_auth"`` and surface a
        Warning overlay on the page.
        """
        url = path if path.startswith("http") else (GITHUB_API + path)
        req = urllib.request.Request(url)
        req.add_header("Authorization", "token " + self._token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        try:
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    body = ""
                    try:
                        body = e.read().decode("utf-8", errors="replace")[:200]
                    except Exception:
                        pass
                    log.warning("GitHub GET %s → 401 Bad Credentials: %s", url, body)
                    raise GithubAuthError("HTTP 401 Bad Credentials") from e
                log.warning("GitHub GET %s failed: HTTP %s", url, e.code)
                return None
        except GithubAuthError:
            raise
        except (urllib.error.URLError, json.JSONDecodeError,
                TimeoutError, OSError) as e:
            log.warning("GitHub GET %s failed: %s", url, e)
            return None

    # ---- Field fetchers ----

    def _fetch_user_profile(self) -> Optional[dict]:
        """Fetch the authenticated user's full profile.

        Returns the raw ``/user`` dict (login, followers, name, …) or
        ``None`` on error. Callers should pull out the specific fields
        they need; we don't unpack here so that future fields (e.g.
        following, public_repos) can be added without touching the
        fetcher.
        """
        data = self._get("/user")
        if not isinstance(data, dict):
            return None
        return data

    def _fetch_total_stars(self, user: str) -> Optional[int]:
        """Sum stargazers_count across the user's own repos. Paginate
        through up to ``_STARS_MAX_PAGES`` (1000 repos) to bound cost."""
        total = 0
        for page in range(1, _STARS_MAX_PAGES + 1):
            data = self._get(
                f"/users/{user}/repos?per_page=100&page={page}"
                f"&type=owner&sort=updated"
            )
            if not isinstance(data, list):
                return None if page == 1 else total
            if not data:
                return total
            for repo in data:
                total += int(repo.get("stargazers_count", 0) or 0)
            if len(data) < 100:
                return total
        return total

    def _fetch_commits_today(self, user: str) -> Optional[int]:
        """Commits authored by user since 00:00 UTC. Uses the search API
        (the only way to get this without enumerating every repo)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00Z")
        q = f"author:{user}+committer-date:>={today}"
        path = "/search/commits?q=" + urllib.parse.quote(q, safe=":+=")
        data = self._get(path)
        if not isinstance(data, dict):
            return None
        return int(data.get("total_count", 0) or 0)

    def _fetch_open_prs(self, user: str) -> Optional[int]:
        """Open PRs authored by user."""
        q = f"author:{user}+type:pr+state:open"
        path = "/search/issues?q=" + urllib.parse.quote(q, safe=":+=")
        data = self._get(path)
        if not isinstance(data, dict):
            return None
        return int(data.get("total_count", 0) or 0)

    def _fetch_latest_event(self, user: str) -> Optional[tuple]:
        """Latest event in the user's activity feed.

        Returns ``(type, object, repo_short, unix_ts)`` or ``None`` if
        the feed is empty / the request failed. The page renders these
        into a one-liner like "Push main 2 min ago".

        The ``object`` is the most identifying piece of the event
        payload — a branch name for Push/Create/Delete, the first
        commit's message for PushEvent (when ref is empty), a PR/issue
        title (truncated) for the corresponding events, etc.
        """
        path = f"/users/{user}/events?per_page=1"
        data = self._get(path)
        if not isinstance(data, list) or not data:
            return None
        e = data[0]
        ev_type = e.get("type") or ""
        payload = e.get("payload") or {}
        repo_full = (e.get("repo") or {}).get("name") or ""
        repo_short = repo_full.split("/", 1)[-1] if "/" in repo_full else repo_full
        return (ev_type, _event_object(ev_type, payload),
                repo_short, _event_unix_ts(e.get("created_at")))


def _event_object(ev_type: str, p: dict) -> str:
    """Pick the most identifying short string from an event payload."""
    if ev_type == "PushEvent":
        # Prefer the branch name (e.g. "main"); fall back to first commit.
        ref = (p.get("ref") or "").split("/")[-1]
        if ref:
            return ref
        commits = p.get("commits") or []
        if commits:
            return (commits[0].get("message") or "push").split("\n", 1)[0][:10]
        return "push"
    if ev_type == "CreateEvent":
        ref_type = p.get("ref_type") or "branch"
        ref = p.get("ref") or ""
        if ref_type == "repository":
            return "repo"
        return ref or ref_type
    if ev_type == "DeleteEvent":
        return p.get("ref") or "branch"
    if ev_type == "PullRequestEvent":
        title = ((p.get("pull_request") or {}).get("title") or "PR")
        return title[:12]
    if ev_type == "IssuesEvent":
        title = ((p.get("issue") or {}).get("title") or "issue")
        return title[:12]
    if ev_type == "WatchEvent":
        return "star"
    if ev_type == "ForkEvent":
        return ((p.get("forkee") or {}).get("name") or "fork")[:12]
    if ev_type == "ReleaseEvent":
        return ((p.get("release") or {}).get("tag_name") or "tag")[:12]
    if ev_type == "PublicEvent":
        return "public"
    return ev_type.replace("Event", "").lower()[:12]
