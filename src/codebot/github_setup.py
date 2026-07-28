"""Interactive GitHub PAT capture for ``codebotd setup`` (phase 4).

The GitHub page on the device needs a personal access token. Without one
the page renders a "GITHUB_TOKEN not set" warning banner and the
collector never ticks (see ``codebot.collectors.github``). Token
resolution at runtime is ``$GITHUB_TOKEN`` > ``github.token`` in
``~/.code_bot/config.yml`` > empty — this phase writes the config file
half of that.

Behaviour:

  • ``$GITHUB_TOKEN`` already set  → report it wins at runtime, skip.
  • stdin is not a TTY             → print how to set it later, skip
                                     (keeps ``codebotd setup`` in CI /
                                     pipes non-blocking).
  • token already in config.yml    → show masked, default is *keep*.
  • otherwise                      → prompt (hidden input), validate
                                     against ``GET /user``, save.

Skipping is always one Enter away and never fails the phase: this
returns 0 in every path except an unwritable config file (rc=1, the
"user action required" code) — a missing token degrades one page, it
shouldn't make ``setup`` look broken.

Under ``sudo`` the config is written to the *invoking* user's home and
chowned back to them, so the daemon (running unprivileged) can still
read and rewrite it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


# Anything with this value in config.yml means "never configured" — it's
# the DEFAULTS placeholder from codebot.config.
_PLACEHOLDER = "__REPLACE_ME__"

_PAT_URL = "https://github.com/settings/tokens/new?scopes=repo,read:user&description=Code%20Bot"

_MAX_ATTEMPTS = 3


def run_github_setup() -> int:
    """Offer to store a GitHub PAT in ``~/.code_bot/config.yml``.

    Whether the user gets prompted is decided by
    ``codebot._ui.is_interactive()`` — which is False when the wizard is
    running in ``--yes`` mode or when stdin/stdout aren't TTYs. In that
    case the only non-skip path is "a token is already in config.yml
    and we're being asked to keep it", which the function performs
    silently.
    """
    from . import _ui
    from .config import Config

    cfg_path = Path.home() / ".code_bot" / "config.yml"

    env_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if env_token:
        _ui.check(
            "GitHub token",
            "INFO",
            "$GITHUB_TOKEN set in environment — overrides config.yml",
        )
        return 0

    cfg = Config(cfg_path)
    current = (cfg.get("github", "token") or "").strip()
    if current == _PLACEHOLDER:
        current = ""

    if not _ui.is_interactive():
        if current:
            _ui.check("GitHub token", "INFO", f"already configured in {cfg_path}")
        else:
            _ui.check("GitHub token", "INFO", "non-interactive mode — skipped")
            _ui.hint([
                "To configure it later:",
                f"  • edit {cfg_path} and set github.token, or",
                "  • export GITHUB_TOKEN=<pat> before starting the daemon.",
            ])
        return 0

    if current:
        _ui.check("GitHub token", "INFO", f"already configured: {_mask(current)}")
        if not _ui.confirm("Replace it?", default=False):
            _ui.check("GitHub token", "INFO", "kept")
            return 0

    _ui.hint([
        "Code Bot's GitHub page needs a personal access token",
        "(scopes: repo, read:user — read-only stats, nothing is written).",
        f"Create one at: {_PAT_URL}",
        "Press Enter on an empty prompt to skip; you can add it later.",
    ])

    token = _prompt_token()
    if token is None:
        _ui.check("GitHub token", "INFO", "skipped")
        _ui.hint([
            "To configure it later:",
            f"  • edit {cfg_path} and set github.token, or",
            "  • export GITHUB_TOKEN=<pat> before starting the daemon.",
        ])
        return 0

    cfg.set("github", "token", token)
    cfg.save()
    if not _verify_written(cfg_path, token):
        _ui.error(f"failed to write {cfg_path}; token not saved")
        return 1

    _ui.check("GitHub token", "PASS", f"saved to {cfg_path} (mode 600)")
    return 0


# ---- prompting ----

def _prompt_token() -> str | None:
    """Read + validate a PAT. ``None`` means the user chose to skip.

    Input is hidden so the token never lands in the terminal scrollback.
    A token that fails validation costs a retry, up to ``_MAX_ATTEMPTS``;
    the user can still keep it after a failed check (offline install, or
    a scope we don't probe for).
    """
    from . import _ui

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        token = _ui.password("GitHub token (hidden, Enter to skip):", default="")
        if not token:
            return None

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


def _verify_written(path: Path, token: str) -> bool:
    """Confirm ``token`` actually landed on disk.

    ``Config.save()`` logs and swallows write errors (it's called from
    the daemon's start-up path where a read-only home shouldn't be
    fatal), so the return value tells us nothing — read the file back
    instead of trusting it.
    """
    try:
        import yaml
    except ImportError:
        return False
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    section = data.get("github")
    return isinstance(section, dict) and section.get("token") == token


# ---- misc ----

def _mask(token: str) -> str:
    """``ghp_abc...wxyz`` — enough to recognise, not enough to reuse."""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"
