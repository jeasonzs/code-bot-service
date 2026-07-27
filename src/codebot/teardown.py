"""One-shot platform-aware uninstaller for Code Bot.

Invoked by ``codebotd teardown``. Reverse of ``codebot.setup.run_setup``.
Three phases run in sequence (none abort on failure — all are best-effort
so partial removal still cleans up as much as possible):

  1. ``service_setup``    — disable + remove systemd unit / LaunchAgent / Task Scheduler task
  2. Claude Code          — strip ``statusLine`` + ``hooks`` from ``~/.claude/settings.json``
  3. ``driver_setup``     — remove udev rule / WinUSB INF binding / macOS nop

Order is the strict reverse of setup: stop the daemon first (so its
collectors don't race with the file/config removals), strip the user-scope
configs, then tear down the hardware-permission layer last (udev
removal is the one phase that may need root via ``run_as_root``).

The GitHub token in ``~/.code_bot/config.yml`` is intentionally NOT
removed: re-running ``codebotd setup`` after teardown is meant to be
fast, and the file is small + well-formed (mode 600). The user can
delete it by hand if they want a clean slate.

``assume_yes`` defaults to True (non-interactive) — this wizard is meant to
be a single command. Set ``assume_yes=False`` (via ``--interactive``) to
confirm before overwriting settings.json.

This module does NOT touch the codebot Python package itself; run
``pip uninstall codebot`` separately if you want to remove the wheel.
It also does NOT clean up daemon state files (``~/.local/share/codebot/``),
the runtime code-bot dir (``~/.code-bot/``), or ``~/.code_bot/config.yml``;
those survive teardown intentionally so re-running ``codebotd setup``
is fast.

Return codes (POSIX convention):
  0 = success (or nothing to remove)
  1 = partial failure (one or more phases needed sudo / device was busy)
  2 = fatal error (settings.json malformed, etc.)
"""

from __future__ import annotations

import os
import sys


def run_teardown(*, assume_yes: bool = True) -> int:
    """Reverse every phase of ``codebot.setup.run_setup``.

    Driver / service phases are best-effort: a non-zero return code is
    remembered but does not abort subsequent phases (you want as much
    cleaned up as possible). The Claude phase aborts only if it can't
    safely parse ``~/.claude/settings.json`` (rc=2); a successful removal
    returns 0.

    Run as the invoking user, not under ``sudo``: this teardown removes
    files under ``~`` (``Path.home()``), so a root shell would target
    ``/root`` instead. The driver phase (udev) may need root — it's
    elevated internally via ``codebot.os_helper.run_as_root``.
    """
    from . import driver_setup, service_setup
    from .claude_integration import install as claude_install

    print(f"[teardown] platform={sys.platform} euid={os.geteuid()} assume_yes={assume_yes}")
    print()

    rc = 0

    # 1. service — stop the daemon first so its collectors don't race
    #    with the config / hardware-permission removals below.
    print(f"[teardown] phase 1/3: service ({sys.platform})")
    service_rc = service_setup.run_service_teardown(assume_yes)
    rc = max(rc, service_rc)
    print()

    # 2. Claude Code integration — strip statusline + hooks block.
    print("[teardown] phase 2/3: Claude Code integration")
    claude_rc = claude_install.run_uninstall(assume_yes=assume_yes)
    rc = max(rc, claude_rc)
    print()

    # 3. driver — last: udev removal may need root (run_as_root
    #    internally). Once the rule is gone, no unprivileged process
    #    can talk to the device, which is the desired end state.
    print(f"[teardown] phase 3/3: driver ({sys.platform})")
    driver_rc = driver_setup.run_driver_teardown(assume_yes)
    rc = max(rc, driver_rc)
    print()

    print(f"[teardown] done (rc={rc}).")
    if rc == 0:
        print("  Run `codebotd setup` to re-install everything.")
        print("  Run `pip uninstall codebot` to remove the Python package.")
    else:
        print("  Some phases returned non-zero; re-run as Administrator / "
              "with sudo if you saw permission errors.")
    return rc


if __name__ == "__main__":
    sys.exit(run_teardown())