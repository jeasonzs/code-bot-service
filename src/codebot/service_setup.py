"""Per-platform daemon auto-start installer for Code Bot.

Invoked by ``codebotd setup`` (phase 3/4). Three branches:

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

``assume_yes`` defaults to True (non-interactive) — ``codebotd setup``
flips it to False only when the user passes ``--interactive``.

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
from importlib.resources import files
from pathlib import Path
from typing import Callable

from ._paths import real_user_home, resolve_codebotd


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


# Per-platform target paths.
# Functions (NOT module constants) because Path.home() evaluated at import
# time would freeze the path to /root when this module is imported under
# `sudo codebotd setup` — then the unit would land in /root/.config/systemd
# and never be enabled for the actual user.


def _linux_unit_dir() -> Path:
    return real_user_home() / ".config" / "systemd" / "user"


def _linux_unit_file() -> Path:
    return _linux_unit_dir() / "codebot.service"


def _mac_agents_dir() -> Path:
    return real_user_home() / "Library" / "LaunchAgents"


def _mac_agents_file() -> Path:
    return _mac_agents_dir() / "com.codebot.codebotd.plist"


WIN_TASK_NAME = "CodeBot"


# ==============================================================
# Linux: systemd user unit
# ==============================================================
def _setup_service_linux(assume_yes: bool) -> int:
    if shutil.which("systemctl") is None:
        print("[setup.service] ERROR: systemctl not found (install systemd)",
              file=sys.stderr)
        return 1

    codebotd_path = resolve_codebotd()
    if codebotd_path is None:
        print("[setup.service] ERROR: `codebotd` not on PATH. "
              "Run `pip install --force-reinstall codebot` (or `pip install -e .` "
              "from a checkout) and ensure its bin/ dir is on PATH.",
              file=sys.stderr)
        return 2

    template = _systemd_unit_template_src()
    if not template.exists():
        print(f"[setup.service] ERROR: systemd unit template not found at {template}",
              file=sys.stderr)
        return 2

    rendered = template.read_text(encoding="utf-8").replace(
        "@CODEBOTD_PATH@", codebotd_path
    )

    unit_dir = _linux_unit_dir()
    unit_file = _linux_unit_file()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file.write_text(rendered, encoding="utf-8")
    print(f"[setup.service] Installed {unit_file}")
    print(f"[setup.service]   ExecStart={codebotd_path} start")

    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "codebot.service"],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[setup.service] ERROR: systemctl failed ({e})", file=sys.stderr)
        return 1
    except FileNotFoundError:
        # Belt-and-suspenders: shutil.which("systemctl") returned truthy above
        print("[setup.service] ERROR: systemctl vanished between checks", file=sys.stderr)
        return 1

    print("[setup.service] Daemon enabled and started for this user.")
    print()
    print("  Hint: for headless boxes (no graphical login), enable lingering so")
    print("  the user unit runs even when you're not logged in:")
    print()
    print(f"    sudo loginctl enable-linger $USER")
    print()
    print("  Verify: `systemctl --user status codebot.service`")
    return 0


# ==============================================================
# macOS: launchd LaunchAgent
# ==============================================================
def _setup_service_macos(assume_yes: bool) -> int:
    if shutil.which("launchctl") is None:
        # Should never happen on macOS but be defensive.
        print("[setup.service] ERROR: launchctl not found (macOS only)",
              file=sys.stderr)
        return 1

    codebotd_path = resolve_codebotd()
    if codebotd_path is None:
        print("[setup.service] ERROR: `codebotd` not on PATH. "
              "Run `pip3 install --user codebot` (or `pip3 install codebot`).",
              file=sys.stderr)
        return 2

    template = _launchd_plist_template_src()
    if not template.exists():
        print(f"[setup.service] ERROR: launchd plist template not found at {template}",
              file=sys.stderr)
        return 2

    rendered = template.read_text(encoding="utf-8").replace(
        "@CODEBOTD_PATH@", codebotd_path
    )

    agents_dir = _mac_agents_dir()
    agents_file = _mac_agents_file()
    agents_dir.mkdir(parents=True, exist_ok=True)
    agents_file.write_text(rendered, encoding="utf-8")
    print(f"[setup.service] Installed {agents_file}")
    print(f"[setup.service]   ProgramArguments[0]={codebotd_path}")

    # If the plist was loaded previously (different codebotd path), unload it
    # first so launchctl re-reads the new ProgramArguments.
    label = "com.codebot.codebotd"
    probe = subprocess.run(
        ["launchctl", "print", f"gui/{_mac_uid()}/{label}"],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 0:
        print(f"[setup.service] Unloading previous {label} (re-render)...")
        subprocess.run(
            ["launchctl", "unload", "-w", str(agents_file)],
            capture_output=True, check=False,
        )

    try:
        subprocess.run(
            ["launchctl", "load", "-w", str(agents_file)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[setup.service] ERROR: launchctl load failed ({e})", file=sys.stderr)
        return 1

    print("[setup.service] LaunchAgent loaded; daemon will start at next Aqua login.")
    print("  Verify: `launchctl list | grep codebot`")
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
def _setup_service_windows(assume_yes: bool) -> int:
    codebotd_path = resolve_codebotd()
    if codebotd_path is None:
        print("[setup.service] ERROR: `codebotd` not on PATH. "
              "Reinstall: `pip install --force-reinstall codebot`.",
              file=sys.stderr)
        return 2

    # schtasks /tr parses the command line; wrap path in quotes so it survives
    # spaces (e.g. C:\Program Files\Python311\Scripts\codebotd.exe).
    tr = f'"{codebotd_path}" start'

    try:
        result = subprocess.run(
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
        print("[setup.service] ERROR: schtasks not found in PATH", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"[setup.service] ERROR: schtasks failed (rc={e.returncode})",
              file=sys.stderr)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        return 1

    print(f"[setup.service] Registered Task Scheduler task '{WIN_TASK_NAME}' "
          f"(onlogon, highest privileges).")
    print(f"[setup.service]   /tr={tr}")
    print()

    # Best-effort query so the user gets an immediate confirmation.
    probe = subprocess.run(
        ["schtasks", "/query", "/tn", WIN_TASK_NAME],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 0:
        # Just the first non-empty line of the query output (status header).
        first = next((ln.strip() for ln in probe.stdout.splitlines() if ln.strip()), "")
        print(f"[setup.service]   status: {first}")
    else:
        print("[setup.service]   (query failed; verify with `schtasks /query /tn CodeBot`)")
    print()
    print("  Hint: trigger the task now without waiting for next logon:")
    print()
    print(f"    schtasks /run /tn {WIN_TASK_NAME}")
    return 0


# ==============================================================
# Teardown (reverse of install)
# ==============================================================
def _teardown_service_linux(assume_yes: bool) -> int:
    """Disable + remove the systemd user unit."""
    if shutil.which("systemctl") is None:
        return 0  # nothing to undo
    # Best-effort disable (ignore non-zero — unit might already be gone).
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", "codebot.service"],
        capture_output=True, check=False,
    )
    removed = False
    unit_file = _linux_unit_file()
    if unit_file.exists():
        try:
            unit_file.unlink()
            removed = True
        except OSError as e:
            print(f"[teardown.service] ERROR: cannot remove {unit_file}: {e}",
                  file=sys.stderr)
            return 1
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True, check=False,
    )
    if removed:
        print(f"[teardown.service] Removed {unit_file}")
    else:
        print("[teardown.service] No systemd user unit to remove.")
    return 0


def _teardown_service_macos(assume_yes: bool) -> int:
    """Unload + remove the LaunchAgent."""
    if shutil.which("launchctl") is None:
        return 0
    agents_file = _mac_agents_file()
    # Best-effort unload (ignore non-zero — agent might already be gone).
    subprocess.run(
        ["launchctl", "unload", "-w", str(agents_file)],
        capture_output=True, check=False,
    )
    if agents_file.exists():
        try:
            agents_file.unlink()
            print(f"[teardown.service] Removed {agents_file}")
        except OSError as e:
            print(f"[teardown.service] ERROR: cannot remove {agents_file}: {e}",
                  file=sys.stderr)
            return 1
    else:
        print("[teardown.service] No LaunchAgent to remove.")
    return 0


def _teardown_service_windows(assume_yes: bool) -> int:
    """Delete the Task Scheduler task."""
    try:
        r = subprocess.run(
            ["schtasks", "/delete", "/tn", WIN_TASK_NAME, "/f"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return 0
    # schtasks rc=1 with "does not exist" is fine — task was already gone.
    if r.returncode == 0:
        print(f"[teardown.service] Removed Task Scheduler task '{WIN_TASK_NAME}'")
        return 0
    err = (r.stderr or "").lower()
    if "cannot find" in err or "does not exist" in err:
        print(f"[teardown.service] No Task Scheduler task '{WIN_TASK_NAME}' to remove.")
        return 0
    print(f"[teardown.service] WARN: schtasks /delete rc={r.returncode}",
          file=sys.stderr)
    if r.stderr:
        print(r.stderr.strip(), file=sys.stderr)
    return 1


# ==============================================================
# Dispatch
# ==============================================================
HANDLERS: dict[str, Callable[[bool], int]] = {
    "linux": _setup_service_linux,
    "darwin": _setup_service_macos,
    "win32": _setup_service_windows,
}

_TEARDOWN_HANDLERS: dict[str, Callable[[bool], int]] = {
    "linux": _teardown_service_linux,
    "darwin": _teardown_service_macos,
    "win32": _teardown_service_windows,
}


def run_service_setup(assume_yes: bool = True) -> int:
    """Run the per-platform auto-start installer. Called by setup.run_setup."""
    handler = HANDLERS.get(sys.platform)
    if handler is None:
        print(f"[setup.service] unsupported platform: {sys.platform}", file=sys.stderr)
        return 2
    return handler(assume_yes)


def run_service_teardown(assume_yes: bool = True) -> int:
    """Reverse of ``run_service_setup``. Called by teardown.py."""
    handler = _TEARDOWN_HANDLERS.get(sys.platform)
    if handler is None:
        print(f"[teardown.service] unsupported platform: {sys.platform}",
              file=sys.stderr)
        return 2
    return handler(assume_yes)


if __name__ == "__main__":
    sys.exit(run_service_setup())