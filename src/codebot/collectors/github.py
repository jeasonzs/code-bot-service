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
    streak: Optional[int]          # always None (streak not computed)
    commits_today: Optional[int]   # commits authored by user since 00:00 UTC
    open_prs: Optional[int]        # open PRs authored by user
    ci_status: Optional[str]       # success / failure / in_progress / queued / ...
    ci_workflow: Optional[str]     # workflow display name
    ci_repo: Optional[str]         # "owner/repo" of the run
    ci_run_number: Optional[int]
    ci_age_min: Optional[int]      # minutes since the run was created
    ts: float                      # unix time of the snapshot


def _empty_snapshot() -> GithubSnapshot:
    return GithubSnapshot(
        user=None, stars=None, streak=None,
        commits_today=None, open_prs=None,
        ci_status=None, ci_workflow=None, ci_repo=None,
        ci_run_number=None, ci_age_min=None, ts=0.0,
    )


def _age_minutes(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0, int((time.time() - ct.timestamp()) / 60))
    except (ValueError, TypeError):
        return None


class GithubCollector:
    """Background thread that refreshes GitHub stats every ``refresh_interval``."""

    def __init__(self, refresh_interval: float = 60.0,
                 ci_repo: str = "code-bot",
                 config: Optional["Config"] = None) -> None:
        self.refresh_interval = refresh_interval
        self._lock = threading.Lock()
        self._latest: GithubSnapshot = _empty_snapshot()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Resolve the token + ci_repo. Env vars win over the config
        # file (12-factor / CI override); config file wins over the
        # hardcoded default. Config is constructed lazily here so the
        # collector can still be used standalone (tests, scripts).
        if config is None:
            from ..config import Config
            config = Config()
        env_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
        env_repo = (os.environ.get("GITHUB_CI_REPO") or "").strip()
        cfg_token = config.get("github", "token") or ""
        cfg_repo = config.get("github", "ci_repo") or ci_repo
        self._token = env_token or cfg_token
        self._ci_repo_name = env_repo or cfg_repo
        if not env_token and cfg_token:
            log.info("Loaded GitHub token from %s", config.path)

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
        user = self._fetch_user()
        if user is None:
            return  # keep the previous snapshot; no partial updates

        stars = self._fetch_total_stars(user)
        commits_today = self._fetch_commits_today(user)
        open_prs = self._fetch_open_prs(user)
        ci = self._fetch_latest_ci(user)

        snap = GithubSnapshot(
            user=user, stars=stars, streak=None,
            commits_today=commits_today, open_prs=open_prs,
            ci_status=ci[0] if ci else None,
            ci_workflow=ci[1] if ci else None,
            ci_repo=ci[2] if ci else None,
            ci_run_number=ci[3] if ci else None,
            ci_age_min=ci[4] if ci else None,
            ts=time.time(),
        )
        with self._lock:
            self._latest = snap

    # ---- HTTP ----

    def _get(self, path: str) -> Optional[object]:
        """GET ``path`` (relative to GITHUB_API, or absolute URL) and return
        the parsed JSON. ``None`` on any error (logged at WARNING)."""
        url = path if path.startswith("http") else (GITHUB_API + path)
        req = urllib.request.Request(url)
        req.add_header("Authorization", "token " + self._token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, TimeoutError, OSError) as e:
            log.warning("GitHub GET %s failed: %s", url, e)
            return None

    # ---- Field fetchers ----

    def _fetch_user(self) -> Optional[str]:
        data = self._get("/user")
        if not isinstance(data, dict):
            return None
        return data.get("login")

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

    def _fetch_latest_ci(self, user: str) -> Optional[tuple]:
        """Latest workflow run on the configured repo (default ``code-bot``).
        Returns ``(status, workflow_name, repo, run_number, age_minutes)``
        or ``None`` if there are no runs / the request failed.

        ``status`` is the ``conclusion`` for completed runs, or the
        ``status`` (queued / in_progress) for in-flight runs.
        """
        path = f"/repos/{user}/{self._ci_repo_name}/actions/runs?per_page=1"
        data = self._get(path)
        if not isinstance(data, dict):
            return None
        runs = data.get("workflow_runs", [])
        if not runs:
            return None
        r = runs[0]
        gh_status = r.get("status")
        conclusion = r.get("conclusion")
        combined = conclusion if gh_status == "completed" else gh_status
        wf = (r.get("name") or "").strip() or "ci"
        repo = f"{user}/{self._ci_repo_name}"
        run_num = r.get("run_number")
        age_min = _age_minutes(r.get("created_at"))
        return (combined, wf, repo, run_num, age_min)
