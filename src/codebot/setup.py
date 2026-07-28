"""One-shot platform-aware installer orchestrator for Code Bot.

Invoked by ``codebotd setup``. Five phases run in sequence:

  1. ``doctor``      — environment diagnostics (non-blocking on FAIL)
  2. ``driver_setup`` — USB driver / permissions for the current platform
  3. Claude Code     — statusline + 8 lifecycle hooks → settings.json
  4. ``github_setup`` — optional GitHub PAT → ~/.code_bot/config.yml
  5. ``service_setup`` — daemon auto-start registration + ``enable --now``

Service is last because ``systemctl --user enable --now`` (and launchd
``load -w``, schtasks ``/create``) starts the daemon immediately. We
want all configs on disk before the daemon's collectors initialize —
otherwise the github collector would latch onto "no token" until the
next manual daemon restart.

Interactive mode is the default; pass ``codebotd setup --yes`` to make
every prompt return its default. The single TTY check and bind() call
live in cli.py so setup and teardown share one rule.

This module is the orchestrator only. The actual per-platform work lives
in:

  - ``driver_setup``    (udev / WinUSB INF / macOS TCC guidance)
  - ``claude_integration.install`` (statusline + hooks merge)
  - ``github_setup``    (interactive PAT prompt; always skippable)
  - ``service_setup``   (systemd user unit / launchd LaunchAgent / Task Scheduler)

Return codes (POSIX convention):
  0 = success
  1 = user action required (sudo / UAC / system prompt to acknowledge / Ctrl-C)
  2 = fatal error
"""

from __future__ import annotations

import sys


def run_setup(*, doctor_only: bool = False) -> int:
    """Run the platform-aware setup wizard end-to-end.

    Returns the max exit code across driver / claude / github / service
    phases. Doctor is non-blocking (FAILs are warned but don't prevent
    subsequent phases); ``--doctor-only`` short-circuits after phase 1.
    A Ctrl-C at any prompt aborts the wizard with rc=1.

    Phase order: doctor → driver → claude → github → service.
    Service is intentionally last so the daemon's first boot sees
    every config file already on disk.
    """
    from . import _ui
    from . import driver_setup, github_setup, service_setup
    from .claude_integration import install as claude_install
    from .doctor import collect_checks

    try:
        _ui.section(f"Phase 1/5 — Environment diagnostics ({sys.platform})")
        rows, _fail_count = collect_checks()
        for row in rows:
            _ui.check(row.name, row.status, row.detail)
        if _fail_count and not _ui.is_interactive():
            _ui.warn(
                f"{_fail_count} doctor check(s) FAILED — continuing anyway. "
                "Re-run `codebotd doctor` for hints."
            )
        if doctor_only:
            return 0

        rc = 0  # doctor is informational; rc reflects the install phases

        # 2. driver
        _ui.section(f"Phase 2/5 — USB driver / permissions ({sys.platform})")
        driver_rc = driver_setup.run_driver_setup()
        if driver_rc != 0:
            # Driver failure aborts the rest: a service that gets
            # autostarted but can't talk to the device is confusing UX.
            _ui.warn(
                f"driver phase failed (rc={driver_rc}); "
                "aborting the rest of setup so the service doesn't autostart into a broken state"
            )
            return driver_rc

        # 3. Claude Code integration
        _ui.section("Phase 3/5 — Claude Code integration")
        claude_rc = claude_install.run_install()
        rc = max(rc, claude_rc)

        # 4. GitHub token (interactive, skippable — before service so the
        #    daemon's first boot reads the token instead of latching onto
        #    "no token" until the next manual restart).
        _ui.section("Phase 4/5 — GitHub token (optional)")
        github_rc = github_setup.run_github_setup()
        rc = max(rc, github_rc)

        # 5. service — last so the daemon starts after every config file
        #    is in place.
        _ui.section(f"Phase 5/5 — Service auto-start ({sys.platform})")
        service_rc = service_setup.run_service_setup()
        rc = max(rc, service_rc)

        if rc == 0:
            _ui.section("Setup done")
            _ui.hint([
                "Verify with:",
                "  codebotd doctor",
                "  systemctl --user status codebot.service   (Linux)",
                "  launchctl list | grep codebot             (macOS)",
                "  schtasks /query /tn CodeBot               (Windows)",
            ])
        else:
            _ui.warn(
                f"setup finished with rc={rc}; one or more phases were skipped or failed. "
                "Re-run `codebotd setup` to retry."
            )
        return rc

    except _ui.WizardCancelled:
        _ui.warn("setup cancelled at a prompt — nothing more happened")
        return 1


if __name__ == "__main__":
    sys.exit(run_setup())
