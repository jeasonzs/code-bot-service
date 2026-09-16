"""Interactive GitHub PAT capture for ``codebotd setup`` (phase 4).

Token resolution at runtime is ``$GITHUB_TOKEN`` env > ``token`` from
the YAML entry — this phase produces the entry half of that.

Returns ``(rc, entry | None)``:
  - ``(0, {"type": "github", "token": "..."})`` on success — caller
    appends the entry to the pages array.
  - ``(0, {"type": "github", "token": ""})`` when ``$GITHUB_TOKEN`` is
    set — page works via env fallback; we write an empty entry just so
    the daemon knows to build the page at all.
  - ``(0, None)`` when the user skipped or non-interactive mode — no
    entry added, no error.
  - ``(1, None)`` when the config write or the validation step
    unrecoverably failed.

The function does NOT touch ``Config`` itself. Persistence is the
caller's job (either ``setup.py`` phase 4 or ``pages_registry``).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional


_PAT_URL = "https://github.com/settings/tokens/new?scopes=repo,read:user&description=Code%20Bot"

_MAX_ATTEMPTS = 3


def run_github_setup(*, prefilled_token: str = "") -> tuple[int, Optional[dict]]:
    """Prompt for / reuse a GitHub PAT and return ``(rc, entry)``.

    When ``prefilled_token`` is non-empty (Modify flow), the prompt
    defaults to that token so a quick Enter keeps the existing one;
    typing replaces it. When empty (Add flow), the prompt is blank.
    """
    from . import _ui

    env_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if env_token and not prefilled_token:
        _ui.check(
            "GitHub token",
            "INFO",
            "$GITHUB_TOKEN set — entry will use env at runtime (token stays empty)",
        )
        return 0, {"type": "github", "token": ""}

    if not _ui.is_interactive():
        _ui.check("GitHub token", "INFO", "non-interactive — skipped")
        return 0, None

    if not prefilled_token and not env_token:
        _ui.hint([
            "Code Bot's GitHub page needs a personal access token",
            "(scopes: repo, read:user — read-only stats, nothing is written).",
            f"Create one at: {_PAT_URL}",
            "Press Enter on an empty prompt to skip; you can add it later.",
        ])

    token = _prompt_token(prefilled=prefilled_token)
    if token is None:
        _ui.check("GitHub token", "INFO", "skipped")
        _ui.hint([
            "To configure it later:",
            "  • add a github entry to ~/.code_bot/config.yml pages:, or",
            "  • export GITHUB_TOKEN=<pat> before starting the daemon.",
        ])
        return 0, None

    return 0, {"type": "github", "token": token}


def modify_github_entry(cfg, entry: dict) -> Optional[dict]:
    """Modify-flow wrapper: re-run setup with the existing token
    pre-filled. Returns the new entry, or ``None`` to keep the old one
    (user cancelled)."""
    from . import _ui

    if not _ui.confirm(
        "Re-run GitHub token setup? (Enter keeps the existing token.)",
        default=False,
    ):
        return None

    prefilled = (entry.get("token") or "").strip()
    rc, new_entry = run_github_setup(prefilled_token=prefilled)
    if new_entry is None:
        return None
    return new_entry


# ---- prompting ----

def _prompt_token(*, prefilled: str = "") -> str | None:
    """Read + validate a PAT. ``None`` means the user chose to skip.

    Input is hidden so the token never lands in the terminal scrollback.
    A token that fails validation costs a retry, up to ``_MAX_ATTEMPTS``;
    the user can still keep it after a failed check (offline install, or
    a scope we don't probe for).
    """
    from . import _ui

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        token = _ui.password(
            "GitHub token (hidden, Enter to skip):", default=prefilled,
        )
        if not token:
            return None
        # If the user just hit Enter on a pre-filled token, accept it
        # without re-validating (we already accepted it once).
        if prefilled and token == prefilled:
            _ui.check("GitHub token", "INFO", "kept existing token")
            return token

        with _ui.spinner("Validating against api.github.com/user …"):
            login, err = _validate(token)
        if login:
            _ui.check("GitHub token", "PASS", f"authenticated as {login}")
            return token

        _ui.warn(err)
        if _ui.confirm("Save it anyway?", default=False):
            return token
        if attempt < _MAX_ATTEMPTS:
            _ui.info("Try again, or press Enter to skip.")
    _ui.warn("Too many failed attempts.")
    return None


# ---- validation ----

def _validate(token: str) -> tuple[str | None, str]:
    """``(login, "")`` when the token works, else ``(None, reason)``.

    Only distinguishes the cases the user can act on: rejected (401 —
    wrong or revoked token), 403 (valid but rate-limited / SSO-blocked),
    and unreachable (offline / proxy). Same endpoint the collector uses
    for its first tick, so a pass here means the page will populate.
    """
    req = urllib.request.Request("https://api.github.com/user")
    req.add_header("Authorization", "token " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        login = body.get("login") if isinstance(body, dict) else None
        if not login:
            return None, "GitHub replied without a login field."
        return str(login), ""
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, "GitHub rejected the token (401 Bad Credentials)."
        if e.code == 403:
            return None, "GitHub returned 403 (rate limited, or SSO not authorised)."
        return None, f"GitHub returned HTTP {e.code}."
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"Could not reach api.github.com ({e}); can't validate offline."
    except json.JSONDecodeError:
        return None, "GitHub returned a malformed response."


# ---- misc ----

def _mask(token: str) -> str:
    """``ghp_abc...wxyz`` — enough to recognise, not enough to reuse."""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"