"""sudo-aware path / binary resolution for the setup and teardown wizards.

When ``codebotd setup`` runs under ``sudo``, the process inherits root's
environment: ``HOME=/root``, ``PATH=secure_path`` (no ``~/.local/bin``),
``USER=root``. These helpers unwind that so the wizard installs services
into the **invoking user's** directories, not root's — so systemd user
units land in the user's ``~/.config/systemd/user/``, plists in the
user's ``~/Library/LaunchAgents/``, and Claude settings in the user's
``~/.claude/settings.json``.

Without this, ``sudo codebotd setup`` would write a systemd unit for
``/root`` (which root can't even ``systemctl --user`` enable), and the
daemon rendered in that unit would be unresolvable (root's PATH doesn't
include the user's pip bin).

Privilege elevation (wrapping ``install`` / ``rm`` / ``udevadm`` with
``sudo`` when needed) lives in ``codebot.os_helper.run_as_root`` —
import that directly when a shell command needs root. We never ``sudo``
python — root's env doesn't have ``codebot`` installed, so re-execing
would break imports (especially under ``pip install --user``, where the
script's shebang resolves to system python under root).

Usage:

    from ._paths import real_user_home, resolve_codebotd, original_user
    from .os_helper import run_as_root

    target = real_user_home() / ".config" / "systemd" / "user" / "codebot.service"
    codebotd = resolve_codebotd()
    user = original_user()  # None if not under sudo

    run_as_root("install", "-m", "644", str(src), str(target))
    run_as_root("udevadm", "control", "--reload-rules")
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def original_user() -> str | None:
    """Username of the original invoker when running under sudo, else None.

    Reads ``SUDO_USER`` from the environment. Returns ``None`` if unset
    (i.e. not running under sudo) — callers should treat that as
    "running as the actual user, no unwrap needed".
    """
    return os.environ.get("SUDO_USER") or None


def real_user_home() -> Path:
    """Home dir of the original user, or ``Path.home()`` if not under sudo.

    Resolves via ``pwd.getpwnam(SUDO_USER).pw_dir`` so the wizard writes
    service units / plists / settings to the invoker's actual home, not
    ``/root``. Falls back to ``Path.home()`` if SUDO_USER is unset or
    unresolvable (non-POSIX, unknown user, missing pwd module on Windows).
    """
    sudo_user = original_user()
    if sudo_user:
        try:
            import pwd
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except (KeyError, ImportError):
            pass
    return Path.home()


def resolve_codebotd() -> str | None:
    """Absolute path of the ``codebotd`` console_script, or None.

    First searches the current process's PATH (covers system installs
    and --user installs where ``~/.local/bin`` is on PATH).

    Under sudo, ``shutil.which`` fails because root's ``secure_path``
    excludes the user's pip bin directory. This helper falls back to
    common user-install locations relative to ``real_user_home()``:

      - ``~/.local/bin/codebotd``    (pip --user; PEP 517 default)
      - ``~/.cargo/bin/codebotd``    (rust-style; some Python toolchains)

    Returns None if nothing is found — caller should treat that as
    ``pip install --force-reinstall codebot`` to fix.
    """
    if p := shutil.which("codebotd"):
        return p
    home = real_user_home()
    for d in (home / ".local" / "bin", home / ".cargo" / "bin"):
        cand = d / "codebotd"
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


# Privilege elevation lives in ``codebot.os_helper.run_as_root`` — import
# from there when a shell command needs root.