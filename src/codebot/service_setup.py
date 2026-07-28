"""Per-platform daemon auto-start installer for Code Bot.

Invoked by ``codebotd setup`` (phase 3/5). Three branches:

  Linux    — render systemd user unit from ``systemd/codebot.service.in``,
              substitute ``@CODEBOTD_PATH@`` with the resolved ``codebotd``
              path, write to ``~/.config/systemd/user/codebot.service``,
              ``daemon-reload`` + ``enable --now``.

  macOS    — render launchd LaunchAgent from
              ``launchd/com.codebot.codebotd.plist.in``, substitute the
              placeholder, write to
              ``~/Library/LaunchAgents/com.codebot.codebotd.plist``, then
              ``launchctl load -w``.

  Windows  — register a per-user Task Scheduler task (``onlogon`` trigger,
              run with highest privileges, ``/f`` to overwrite). No admin
              needed: matches the daemon's per-user USB scope, equivalent
              to ``systemd --user`` / ``~/Library/LaunchAgents``.

``assume_yes`` is no longer a parameter; whether the handler prompts
is decided by ``codebot._ui.is_interactive()``. ``codebotd setup`` is
interactive by default; pass ``--yes`` to suppress every prompt.

Return codes:
  0 = success
  1 = user action required (systemctl / launchctl missing or odd state)
  2 = fatal error (codebotd not on PATH — re-pip-install)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
# Python 3.8 ships `importlib.resources` without `files()` (added in 3.9);
# fall back to the backport so the package installs cleanly on 3.8 too.
try:
    from importlib.resources import files
except ImportError:  # pragma: no cover — only hit on Python < 3.9
    from importlib_resources import files  # type: ignore[no-redef]
from pathlib import Path
from typing import Callable

from . import _ui

log = logging.getLogger("codebot.service_setup")


# ==============================================================
# Asset resolution (self-contained — does not import codebot.setup)
# ==============================================================
def _materialize_traversable(traversable) -> Path:
    """Return a real on-disk Path for an importlib Traversable.

    Flat-installed wheels expose Traversables as filesystem paths and
    ``str(t)`` returns a usable path. Zip-installed wheels don't, so we
    copy the content into a NamedTemporaryFile.
    """
    try:
        p = Path(str(traversable))
        if p.exists():
            return p
    except OSError:
        pass
    suffix = "".join(traversable.suffixes)
    with tempfile.NamedTemporaryFile(
        prefix=f"{traversable.name}.", suffix=suffix or ".tmp",
        delete=False, mode="w", encoding="utf-8",
    ) as tmp:
        tmp.write(traversable.read_text(encoding="utf-8"))
        return Path(tmp.name)


def _systemd_unit_template_src() -> Path:
    """systemd user unit template. Contains @CODEBOTD_PATH@ placeholder
    that is substituted with the resolved ``codebotd`` path at install time."""
    return _materialize_traversable(
        files("codebot") / "systemd" / "codebot.service.in"
    )


def _launchd_plist_template_src() -> Path:
    """launchd LaunchAgent template. Contains @CODEBOTD_PATH@ placeholder."""
    return _materialize_traversable(
        files("codebot") / "launchd" / "com.codebot.codebotd.plist.in"
    )


# Per-platform target paths. Functions (not module constants) so tests
# can monkey-patch ``Path.home()`` without re-importing.


def _linux_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _linux_unit_file() -> Path:
    return _linux_unit_dir() / "codebot.service"


def _mac_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _mac_agents_file() -> Path:
    return _mac_agents_dir() / "com.codebot.codebotd.plist"


WIN_TASK_NAME = "CodeBot"


# ==============================================================
# Linux: systemd user unit
# ==============================================================
def _setup_service_linux() -> int:
    if shutil.which("systemctl") is None:
        _ui.error("systemctl not found (install systemd)")
        return 1

    codebotd_path = shutil.which("codebotd")
    if codebotd_path is None:
        _ui.error("`codebotd` not on PATH — reinstall the package")
        return 2

    if not _ui.confirm(
        f"Register the codebotd systemd user unit at {_linux_unit_file()}?",
        default=True,
    ):
        _ui.check("service", "WARN", "skipped — start it by hand later")
        return 0

    template = _systemd_unit_template_src()
    if not template.exists():
        _ui.error(f"systemd unit template not found at {template}")
        return 2

    rendered = template.read_text(encoding="utf-8").replace(
        "@CODEBOTD_PATH@", codebotd_path
    )

    unit_dir = _linux_unit_dir()
    unit_file = _linux_unit_file()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file.write_text(rendered, encoding="utf-8")
    _ui.check("service", "PASS", f"installed {unit_file} (ExecStart={codebotd_path} start)")

    # Prompting is done; safe to spawn systemctl.
    try:
        with _ui.spinner("systemctl daemon-reload + enable --now …"):
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", "codebot.service"],
                check=True, capture_output=True,
            )
    except subprocess.CalledProcessError as e:
        _ui.error(f"systemctl failed ({e})")
        return 1
    except FileNotFoundError:
        # Belt-and-suspenders: shutil.which("systemctl") returned truthy above
        _ui.error("systemctl vanished between checks")
        return 1

    _ui.check("service", "PASS", "daemon enabled and started for this user")
    _ui.hint([
        "Hint: for headless boxes (no graphical login), enable lingering so",
        "the user unit runs even when you're not logged in:",
        "",
        "    sudo loginctl enable-linger $USER",
        "",
        "Verify: `systemctl --user status codebot.service`",
    ])
    return 0


# ==============================================================
# macOS: launchd LaunchAgent
# ==============================================================
def _setup_service_macos() -> int:
    if shutil.which("launchctl") is None:
        # Should never happen on macOS but be defensive.
        _ui.error("launchctl not found (macOS only)")
        return 1

    codebotd_path = shutil.which("codebotd")
    if codebotd_path is None:
        _ui.error("`codebotd` not on PATH — reinstall the package")
        return 2

    if not _ui.confirm(
        f"Register the codebotd LaunchAgent at {_mac_agents_file()}?",
        default=True,
    ):
        _ui.check("service", "WARN", "skipped — load it by hand later")
        return 0

    template = _launchd_plist_template_src()
    if not template.exists():
        _ui.error(f"launchd plist template not found at {template}")
        return 2

    rendered = template.read_text(encoding="utf-8").replace(
        "@CODEBOTD_PATH@", codebotd_path
    )

    agents_dir = _mac_agents_dir()
    agents_file = _mac_agents_file()
    agents_dir.mkdir(parents=True, exist_ok=True)
    agents_file.write_text(rendered, encoding="utf-8")
    _ui.check("service", "PASS", f"installed {agents_file} (ProgramArguments[0]={codebotd_path})")

    # If the plist was loaded previously (different codebotd path), unload it
    # first so launchctl re-reads the new ProgramArguments. (No prompts in
    # the way, so safe to call launchctl now.)
    label = "com.codebot.codebotd"
    probe = subprocess.run(
        ["launchctl", "print", f"gui/{_mac_uid()}/{label}"],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 0:
        _ui.info(f"unloading previous {label} (re-render)")
        subprocess.run(
            ["launchctl", "unload", "-w", str(agents_file)],
            capture_output=True, check=False,
        )

    try:
        with _ui.spinner("launchctl load -w …"):
            subprocess.run(
                ["launchctl", "load", "-w", str(agents_file)],
                check=True, capture_output=True,
            )
    except subprocess.CalledProcessError as e:
        _ui.error(f"launchctl load failed ({e})")
        return 1

    _ui.check("service", "PASS", "LaunchAgent loaded — daemon starts at next Aqua login")
    _ui.info("Verify: `launchctl list | grep codebot`")
    return 0


def _mac_uid() -> str:
    """Current user's UID, as a string, for launchctl gui/<uid>/<label> queries."""
    try:
        import os
        return str(os.getuid())
    except AttributeError:
        # Non-POSIX (shouldn't happen on darwin but be defensive)
        return "0"


# ==============================================================
# Windows: Task Scheduler per-user task
# ==============================================================
def _setup_service_windows() -> int:
    codebotd_path = shutil.which("codebotd")
    if codebotd_path is None:
        _ui.error("`codebotd` not on PATH — reinstall the package")
        return 2

    if not _ui.confirm(
        f"Register the codebotd Task Scheduler task '{WIN_TASK_NAME}' (onlogon, highest privileges)?",
        default=True,
    ):
        _ui.check("service", "WARN", "skipped — register it by hand later")
        return 0

    # schtasks /tr parses the command line; wrap path in quotes so it survives
    # spaces (e.g. C:\Program Files\Python311\Scripts\codebotd.exe).
    tr = f'"{codebotd_path}" start'

    try:
        with _ui.spinner("schtasks /create …"):
            subprocess.run(
                [
                    "schtasks", "/create",
                    "/tn", WIN_TASK_NAME,
                    "/tr", tr,
                    "/sc", "onlogon",
                    "/rl", "HIGHEST",
                    "/f",
                ],
                capture_output=True, text=True, check=True,
            )
    except FileNotFoundError:
        _ui.error("schtasks not found in PATH")
        return 1
    except subprocess.CalledProcessError as e:
        _ui.error(f"schtasks failed (rc={e.returncode})")
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        return 1

    _ui.check("service", "PASS", f"registered task '{WIN_TASK_NAME}' (onlogon)")
    _ui.info(f"  /tr={tr}")

    # Best-effort query so the user gets an immediate confirmation.
    probe = subprocess.run(
        ["schtasks", "/query", "/tn", WIN_TASK_NAME],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 0:
        # Just the first non-empty line of the query output (status header).
        first = next((ln.strip() for ln in probe.stdout.splitlines() if ln.strip()), "")
        _ui.info(f"  status: {first}")
    else:
        _ui.info("  (query failed; verify with `schtasks /query /tn CodeBot`)")

    _ui.hint([
        "Trigger the task now without waiting for next logon:",
        "",
        f"    schtasks /run /tn {WIN_TASK_NAME}",
    ])
    return 0


# ==============================================================
# Teardown (reverse of install)
# ==============================================================
def _teardown_service_linux() -> int:
    """Disable + remove the systemd user unit."""
    if shutil.which("systemctl") is None:
        return 0  # nothing to undo

    unit_file = _linux_unit_file()
    has_unit = unit_file.exists()
    label = "codebot.service"
    action = "Disable + remove" if has_unit else "No systemd user unit to remove"
    if has_unit and not _ui.confirm(f"{action} the {label} user unit?", default=True):
        _ui.check("service", "WARN", "kept")
        return 0
    if not has_unit:
        _ui.check("service", "INFO", action)
        return 0

    # Best-effort disable (ignore non-zero — unit might already be gone).
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", "codebot.service"],
        capture_output=True, check=False,
    )
    try:
        unit_file.unlink()
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, check=False,
        )
        _ui.check("service", "PASS", f"removed {unit_file}")
    except OSError as e:
        _ui.error(f"cannot remove {unit_file}: {e}")
        return 1
    return 0


def _teardown_service_macos() -> int:
    """Unload + remove the LaunchAgent."""
    if shutil.which("launchctl") is None:
        return 0
    agents_file = _mac_agents_file()
    if not agents_file.exists():
        _ui.check("service", "INFO", "no LaunchAgent to remove")
        return 0

    if not _ui.confirm(f"Unload + remove {agents_file}?", default=True):
        _ui.check("service", "WARN", "kept")
        return 0

    # Best-effort unload (ignore non-zero — agent might already be gone).
    subprocess.run(
        ["launchctl", "unload", "-w", str(agents_file)],
        capture_output=True, check=False,
    )
    try:
        agents_file.unlink()
        _ui.check("service", "PASS", f"removed {agents_file}")
    except OSError as e:
        _ui.error(f"cannot remove {agents_file}: {e}")
        return 1
    return 0


def _teardown_service_windows() -> int:
    """Delete the Task Scheduler task."""
    try:
        if not _ui.confirm(
            f"Delete the Task Scheduler task '{WIN_TASK_NAME}'?", default=True,
        ):
            _ui.check("service", "WARN", "kept")
            return 0
        with _ui.spinner("schtasks /delete …"):
            r = subprocess.run(
                ["schtasks", "/delete", "/tn", WIN_TASK_NAME, "/f"],
                capture_output=True, text=True, check=False,
            )
    except FileNotFoundError:
        return 0
    # schtasks rc=1 with "does not exist" is fine — task was already gone.
    if r.returncode == 0:
        _ui.check("service", "PASS", f"removed task '{WIN_TASK_NAME}'")
        return 0
    err = (r.stderr or "").lower()
    if "cannot find" in err or "does not exist" in err:
        _ui.check("service", "INFO", f"no task '{WIN_TASK_NAME}' to remove")
        return 0
    _ui.warn(f"schtasks /delete rc={r.returncode}")
    if r.stderr:
        print(r.stderr.strip(), file=sys.stderr)
    return 1


# ==============================================================
# Dispatch
# ==============================================================
HANDLERS: dict[str, Callable[[], int]] = {
    "linux": _setup_service_linux,
    "darwin": _setup_service_macos,
    "win32": _setup_service_windows,
}

_TEARDOWN_HANDLERS: dict[str, Callable[[], int]] = {
    "linux": _teardown_service_linux,
    "darwin": _teardown_service_macos,
    "win32": _teardown_service_windows,
}


def run_service_setup() -> int:
    """Run the per-platform auto-start installer. Called by setup.run_setup."""
    handler = HANDLERS.get(sys.platform)
    if handler is None:
        _ui.error(f"unsupported platform: {sys.platform}")
        return 2
    return handler()


def run_service_teardown() -> int:
    """Reverse of ``run_service_setup``. Called by teardown.py."""
    handler = _TEARDOWN_HANDLERS.get(sys.platform)
    if handler is None:
        _ui.error(f"unsupported platform: {sys.platform}")
        return 2
    return handler()


if __name__ == "__main__":
    sys.exit(run_service_setup())