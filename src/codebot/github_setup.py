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

import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


# Anything with this value in config.yml means "never configured" — it's
# the DEFAULTS placeholder from codebot.config.
_PLACEHOLDER = "__REPLACE_ME__"

_PAT_URL = "https://github.com/settings/tokens/new?scopes=repo,read:user&description=Code%20Bot"

_MAX_ATTEMPTS = 3


def run_github_setup(assume_yes: bool = True) -> int:
    """Offer to store a GitHub PAT in ``~/.code_bot/config.yml``.

    ``assume_yes`` is accepted for phase-signature symmetry but only
    gates the *re*-prompt when a token is already configured; the
    prompt itself is driven by whether stdin is a TTY, since this phase
    collects input rather than confirming a destructive action.
    """
    from .config import Config

    cfg_path = Path.home() / ".code_bot" / "config.yml"

    env_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if env_token:
        print("  $GITHUB_TOKEN is set in the environment; it overrides config.yml.")
        print("  Skipping token setup.")
        return 0

    cfg = Config(cfg_path)
    current = (cfg.get("github", "token") or "").strip()
    if current == _PLACEHOLDER:
        current = ""

    if not sys.stdin.isatty():
        print("  stdin is not a TTY — skipping the interactive token prompt.")
        if current:
            print(f"  A token is already configured in {cfg_path}.")
        else:
            _print_manual_hint(cfg_path)
        return 0

    if current:
        print(f"  A GitHub token is already configured in {cfg_path}:")
        print(f"    github.token = {_mask(current)}")
        if assume_yes or not _ask_yes_no("  Replace it?", default=False):
            print("  Keeping the existing token.")
            return 0

    print("  Code Bot's GitHub page needs a personal access token")
    print("  (scopes: repo, read:user — read-only stats, nothing is written).")
    print(f"  Create one at: {_PAT_URL}")
    print("  Press Enter on an empty prompt to skip; you can add it later.")
    print()

    token = _prompt_token()
    if token is None:
        print("  Skipped.")
        _print_manual_hint(cfg_path)
        return 0

    cfg.set("github", "token", token)
    cfg.save()
    if not _verify_written(cfg_path, token):
        print(f"  ERROR: failed to write {cfg_path}; token not saved.", file=sys.stderr)
        _print_manual_hint(cfg_path)
        return 1

    print(f"  Saved to {cfg_path} (mode 600).")
    return 0


# ---- prompting ----

def _prompt_token() -> str | None:
    """Read + validate a PAT. ``None`` means the user chose to skip.

    Input is hidden (``getpass``) so the token never lands in the
    terminal scrollback. A token that fails validation costs a retry,
    up to ``_MAX_ATTEMPTS``; the user can still keep it after a failed
    check (offline install, or a scope we don't probe for).
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            token = getpass.getpass("  GitHub token (hidden, Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not token:
            return None

        print("  Validating against api.github.com/user ...")
        login, err = _validate(token)
        if login:
            print(f"  ✓ Token valid — authenticated as {login}.")
            return token

        print(f"  ✗ {err}")
        if _ask_yes_no("  Save it anyway?", default=False):
            return token
        if attempt < _MAX_ATTEMPTS:
            print("  Try again, or press Enter to skip.")
    print("  Too many failed attempts.")
    return None


def _ask_yes_no(question: str, *, default: bool) -> bool:
    """Prompt ``question``; EOF / Ctrl-C / empty input takes ``default``."""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


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


def _print_manual_hint(cfg_path: Path) -> None:
    print("  To configure it later, either:")
    print(f"    • edit {cfg_path} and set github.token, or")
    print("    • export GITHUB_TOKEN=<pat> before starting the daemon.")
