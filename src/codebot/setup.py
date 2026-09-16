"""One-shot platform-aware installer orchestrator for Code Bot.

Invoked by ``codebotd setup``. Seven phases run in sequence:

  1. ``doctor``               — environment diagnostics (non-blocking on FAIL)
  2. ``driver_setup``         — USB driver / permissions for the current platform
  3. Claude pages             — multi-instance: each entry target=local|ssh,
                                 local entries install hooks into
                                 ~/.claude/settings.json; SSH entries install
                                 hooks on the remote host.
  4. GitHub pages             — multi-instance: each entry holds its own PAT.
  5. Custom commands          — single Configure/Skip step: scans installed
                                 apps, multi-select, chunks into per-page
                                 entries; overwrites any existing selection.
  6. System pages             — multi-instance: each entry target=local|ssh.
  7. ``service_setup``        — daemon auto-start registration + ``enable --now``

Phases 3, 4 and 6 are per-type loops (``pages_registry.run_type_phase``) —
the user can add / edit / delete any number of entries of that type. Each
type owns its own setup function (``claude_setup.run_claude_setup``,
``github_setup.run_github_setup``, etc.) so the type-specific UX is
coherent: Claude phase installs hooks, GitHub phase prompts for a PAT,
etc.

There is no longer a generic "Pages registry" phase — the four page
phases together cover every page type. Each ``cfg.pages`` entry is
created by exactly one per-type phase.

Service is last because ``systemctl --user enable --now`` (and launchd
``load -w``, schtasks ``/create``) starts the daemon immediately. We
want all configs on disk before the daemon's collectors initialize —
otherwise the github collector would latch onto "no token" until the
next manual daemon restart.

Interactive mode is the default; pass ``codebotd setup --yes`` to make
every prompt return its default. The single TTY check and bind() call
live in cli.py so setup and teardown share one rule.

This module is the orchestrator only. The actual per-platform / per-type
work lives in:

  - ``driver_setup``            (udev / WinUSB INF / macOS TCC guidance)
  - ``claude_setup``            (local / SSH target prompt + hooks install)
  - ``github_setup``            (interactive PAT prompt; always skippable)
  - ``commands_setup``          (standalone Configure/Skip; scans + multi-select)
  - ``system_setup``            (local / SSH target prompt)
  - ``service_setup``           (systemd user unit / launchd LaunchAgent / Task Scheduler)

``pages_registry`` is the shared per-type phase loop; it is not a phase
itself.

Return codes (POSIX convention):
  0 = success
  1 = user action required (sudo / UAC / system prompt to acknowledge / Ctrl-C)
  2 = fatal error
"""

from __future__ import annotations

import sys


def run_setup(*, doctor_only: bool = False) -> int:
    """Run the platform-aware setup wizard end-to-end.

    Returns the max exit code across driver / claude / github / commands /
    system / service phases. Doctor is non-blocking (FAILs are warned
    but don't prevent subsequent phases); ``--doctor-only`` short-circuits
    after phase 1. A Ctrl-C at any prompt aborts the wizard with rc=1.
    """
    from . import _ui
    from . import driver_setup, service_setup, pages_registry
    from . import claude_setup, github_setup, commands_setup, system_setup
    from .config import Config
    from .doctor import collect_checks

    cfg = Config()

    try:
        _ui.section(f"Phase 1/7 — Environment diagnostics ({sys.platform})")
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
        _ui.section(f"Phase 2/7 — USB driver / permissions ({sys.platform})")
        driver_rc = driver_setup.run_driver_setup()
        if driver_rc != 0:
            # Driver failure aborts the rest: a service that gets
            # autostarted but can't talk to the device is confusing UX.
            _ui.warn(
                f"driver phase failed (rc={driver_rc}); "
                "aborting the rest of setup so the service doesn't autostart into a broken state"
            )
            return driver_rc

        # 3. Claude pages — multi-instance. Each call to
        #    claude_setup.run_claude_setup handles its own hooks
        #    (local: ~/.claude/settings.json; SSH: remote host's file).
        _ui.section("Phase 3/7 — Claude pages")
        claude_rc = pages_registry.run_type_phase(
            cfg, kind="claude",
            add_handler=claude_setup.run_claude_setup,
            modify_handler=claude_setup.modify_claude_entry,
            format_entry=pages_registry.format_claude_entry,
            add_label="Add a Claude page…",
        )
        rc = max(rc, claude_rc)

        # 4. GitHub pages — multi-instance. Each entry holds its own PAT.
        _ui.section("Phase 4/7 — GitHub pages")
        gh_rc = pages_registry.run_type_phase(
            cfg, kind="github",
            add_handler=github_setup.run_github_setup,
            modify_handler=github_setup.modify_github_entry,
            format_entry=pages_registry.format_github_entry,
            add_label="Add a GitHub account…",
        )
        rc = max(rc, gh_rc)

        # 5. Custom commands — single Configure/Skip step. Overwrites any
        #    existing selection (no per-entry edit; the wizard is the
        #    only way to edit custom_commands in this build).
        _ui.section("Phase 5/7 — Custom commands")
        cmd_rc = commands_setup.run_commands_setup(cfg)
        rc = max(rc, cmd_rc)

        # 6. System pages — multi-instance. target=local or target=ssh.
        _ui.section("Phase 6/7 — System pages")
        sys_rc = pages_registry.run_type_phase(
            cfg, kind="system",
            add_handler=system_setup.run_system_setup,
            modify_handler=system_setup.modify_system_entry,
            format_entry=pages_registry.format_system_entry,
            add_label="Add a system page…",
        )
        rc = max(rc, sys_rc)

        # 7. service — last so the daemon starts after every config file
        #    is in place.
        _ui.section(f"Phase 7/7 — Service auto-start ({sys.platform})")
        service_rc = service_setup.run_service_setup()
        rc = max(rc, service_rc)

        if rc == 0:
            _ui.section("Setup done")
            _ui.hint([
                "Verify with:",
                "  codebotd doctor",
                "  systemctl --user status codebot.service   (Linux)",
                "  launchctl list | grep codebot             (macOS)",
                "  sc query codebotd                         (Windows)",
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