"""One-shot platform-aware uninstaller for Code Bot.

Invoked by ``codebotd teardown``. Reverse of ``codebot.setup.run_setup``.
Three phases run in sequence (none abort on failure — all are best-effort
so partial removal still cleans up as much as possible):

  1. ``service_setup``    — disable + remove systemd unit / LaunchAgent / Task Scheduler task
  2. Claude Code          — strip ``statusLine`` + ``hooks`` from settings.json
  3. ``driver_setup``     — remove udev rule / WinUSB INF binding / macOS nop

Order is the strict reverse of setup: stop the daemon first (so its
collectors don't race with the file/config removals), strip the user-scope
configs, then tear down the hardware-permission layer last (udev removal
is the one phase that may need root via ``run_as_root``).

The GitHub token in ``~/.code_bot/config.yml`` is intentionally NOT
removed: re-running ``codebotd setup`` after teardown is meant to be
fast, and the file is small + well-formed (mode 600). The user can
delete it by hand if they want a clean slate.

Interactive mode is the default; pass ``codebotd teardown --yes`` to
suppress every prompt. The single TTY check and bind() call live in
cli.py so setup and teardown share one rule.

This module does NOT touch the codebot Python package itself; run
``pip uninstall codebot`` separately if you want to remove the wheel.
It also does NOT clean up daemon state files (``~/.local/share/codebot/``),
the runtime code-bot dir (``~/.code-bot/``), or ``~/.code_bot/config.yml``;
those survive teardown intentionally so re-running ``codebotd setup``
is fast.

Return codes (POSIX convention):
  0 = success (or nothing to remove)
  1 = partial failure (one or more phases needed sudo / device was busy / Ctrl-C)
  2 = fatal error (settings.json malformed, etc.)
"""

from __future__ import annotations

import sys


def run_teardown() -> int:
    """Reverse every phase of ``codebot.setup.run_setup``.

    Driver / service phases are best-effort: a non-zero return code is
    remembered but does not abort subsequent phases (you want as much
    cleaned up as possible). The Claude phase aborts only if it can't
    safely parse settings.json (rc=2); a successful removal returns 0.

    Run as the invoking user, not under ``sudo``: this teardown removes
    files under ``~`` (``Path.home()``), so a root shell would target
    ``/root`` instead. The driver phase (udev) may need root — it's
    elevated internally via ``codebot.os_helper.run_as_root``.
    """
    from . import _ui
    from . import driver_setup, service_setup
    from .claude_integration import install as claude_install

    try:
        rc = 0

        # 1. service — stop the daemon first so its collectors don't race
        #    with the config / hardware-permission removals below.
        _ui.section(f"Phase 1/3 — service teardown ({sys.platform})")
        service_rc = service_setup.run_service_teardown()
        rc = max(rc, service_rc)

        # 2. Claude Code integration — strip statusline + hooks block.
        _ui.section("Phase 2/3 — Claude Code integration")
        claude_rc = claude_install.run_uninstall()
        rc = max(rc, claude_rc)

        # 3. driver — last: udev removal may need root (run_as_root
        #    internally). Once the rule is gone, no unprivileged process
        #    can talk to the device, which is the desired end state.
        _ui.section(f"Phase 3/3 — driver teardown ({sys.platform})")
        driver_rc = driver_setup.run_driver_teardown()
        rc = max(rc, driver_rc)

        if rc == 0:
            _ui.section("Teardown done")
            _ui.hint([
                "Run `codebotd setup` to re-install everything.",
                "Run `pip uninstall codebot` to remove the Python package.",
            ])
        else:
            _ui.warn(
                f"teardown finished with rc={rc}. "
                "Re-run as Administrator / with sudo if you saw permission errors."
            )
        return rc

    except _ui.WizardCancelled:
        _ui.warn("teardown cancelled at a prompt — partial state may remain")
        return 1


if __name__ == "__main__":
    sys.exit(run_teardown())
