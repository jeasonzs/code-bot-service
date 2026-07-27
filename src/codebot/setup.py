"""One-shot platform-aware installer orchestrator for Code Bot.

Invoked by ``codebotd setup``. Five phases run in sequence:

  1. ``doctor``      — environment diagnostics (non-blocking on FAIL)
  2. ``driver_setup`` — USB driver / permissions for the current platform
  3. Claude Code     — statusline + 8 lifecycle hooks → ~/.claude/settings.json
  4. ``github_setup`` — optional GitHub PAT → ~/.code_bot/config.yml
  5. ``service_setup`` — daemon auto-start registration + ``enable --now``

Service is last because ``systemctl --user enable --now`` (and launchd
``load -w``, schtasks ``/create``) starts the daemon immediately. We
want all configs (``~/.claude/settings.json``, ``~/.code_bot/config.yml``)
on disk before the daemon's collectors initialize — otherwise the
github collector would latch onto "no token" until the next manual
daemon restart.

This module is the orchestrator only. The actual per-platform work lives
in:

  - ``driver_setup``    (udev / WinUSB INF / macOS TCC guidance)
  - ``claude_integration.install`` (statusline + hooks merge)
  - ``github_setup``    (interactive PAT prompt; always skippable)
  - ``service_setup``   (systemd user unit / launchd LaunchAgent / Task Scheduler)

``assume_yes`` defaults to True (non-interactive) — this wizard is meant to
be a single command after ``pip install``. Set ``assume_yes=False`` (via
``codebotd setup --interactive``) to be prompted before overwriting udev
rules or running privileged commands.

Return codes (POSIX convention):
  0 = success
  1 = user action required (sudo / UAC / system prompt to acknowledge)
  2 = fatal error
"""

from __future__ import annotations

import sys


def run_setup(*, assume_yes: bool = True, doctor_only: bool = False) -> int:
    """Run the platform-aware setup wizard end-to-end.

    Returns the max exit code across driver / claude / github / service
    phases. Doctor is non-blocking (FAILs are warned but don't prevent
    subsequent phases); ``--doctor-only`` short-circuits after phase 1.

    Phase order: doctor → driver → claude → github → service.
    Service is intentionally last so the daemon's first boot sees
    every config file already on disk.
    """
    # Local imports keep module imports flat (avoid circular surprises
    # if driver_setup / service_setup grow entry-point helpers later).
    from . import driver_setup, github_setup, service_setup
    from .claude_integration import install as claude_install
    from .doctor import run_doctor

    print(f"[setup] platform={sys.platform} assume_yes={assume_yes} doctor_only={doctor_only}")
    print()

    # 1. doctor (always run; non-blocking on FAIL — informational only)
    print("[setup] phase 1/5: doctor")
    doctor_rc = run_doctor(verbose=True)
    print()
    if doctor_rc != 0:
        print(
            f"[setup] WARN: doctor reported FAILs (rc={doctor_rc}); continuing anyway.",
            file=sys.stderr,
        )
    if doctor_only:
        return 0  # explicit early exit for CI

    rc = 0  # doctor is informational only; final rc reflects install phases

    # 2. driver
    print(f"[setup] phase 2/5: driver ({sys.platform})")
    driver_rc = driver_setup.run_driver_setup(assume_yes)
    if driver_rc != 0:
        # Driver failure aborts the rest: a service that gets autostarted but
        # can't talk to the device is confusing UX.
        print(f"[setup] driver phase failed (rc={driver_rc}); aborting.", file=sys.stderr)
        return driver_rc
    print()

    # 3. Claude Code integration
    print("[setup] phase 3/5: Claude Code integration")
    claude_rc = claude_install.run_install(assume_yes=assume_yes)
    rc = max(rc, claude_rc)
    print()

    # 4. GitHub token (interactive, skippable — before service so the
    #    daemon's first boot reads the token instead of latching onto
    #    "no token" until the next manual restart).
    print("[setup] phase 4/5: GitHub token (optional)")
    github_rc = github_setup.run_github_setup(assume_yes=assume_yes)
    rc = max(rc, github_rc)
    print()

    # 5. service — last so the daemon starts after every config file is
    #    in place. ``enable --now`` / ``launchctl load -w`` / schtasks
    #    ``/create`` all start the daemon immediately.
    print(f"[setup] phase 5/5: service ({sys.platform})")
    service_rc = service_setup.run_service_setup(assume_yes=assume_yes)
    rc = max(rc, service_rc)
    print()

    print(f"[setup] done (rc={rc}). Verify with `codebotd doctor` and "
          "`systemctl --user status codebot.service` (Linux) / "
          "`launchctl list | grep codebot` (macOS) / "
          "`schtasks /query /tn CodeBot` (Windows).")
    return rc


if __name__ == "__main__":
    sys.exit(run_setup())