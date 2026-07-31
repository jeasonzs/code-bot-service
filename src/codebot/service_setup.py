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

  Windows  — register a real Windows Service via NSSM (``SERVICE_AUTO_START``,
              runs in Session 0 at boot, no console window). Requires NSSM
              on PATH (or at a well-known install path) and an elevated shell.
              Replaces the older ``schtasks /create /sc onlogon`` path, which
              only started after user logon and showed a foreground console.

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
import os
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
WIN_SERVICE_NAME = "codebotd"


def _find_nssm() -> Path | None:
    """Locate nssm.exe. Checks PATH first, then common install locations
    (choco's default, scoop, manual drops). Returns None if not found."""
    found = shutil.which("nssm")
    if found:
        return Path(found)
    for c in (
        Path(r"C:\Tools\nssm\win64\nssm.exe"),
        Path(r"C:\Program Files\nssm\win64\nssm.exe"),
        Path(r"C:\Program Files (x86)\nssm\win64\nssm.exe"),
        Path(r"C:\ProgramData\chocolatey\lib\nssm\tools\nssm.exe"),
    ):
        if c.exists():
            return c
    return None


def _is_windows_admin() -> bool:
    """True if the current process is elevated (UAC accepted). NSSM install
    requires admin; fail fast with a clear hint instead of a cryptic
    'access denied' from sc.exe."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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
# Windows: NSSM-managed Windows Service (autostart at boot, no console)
# ==============================================================
def _setup_service_windows() -> int:
    nssm = _find_nssm()
    if nssm is None:
        _ui.error(
            "NSSM not found. Install it first:\n"
            "    choco install nssm          (Chocolatey)\n"
            "    scoop install nssm          (Scoop)\n"
            "    or download from https://nssm.cc/download"
        )
        return 1

    codebotd_path = shutil.which("codebotd")
    if codebotd_path is None:
        _ui.error("`codebotd` not on PATH — reinstall the package")
        return 2

    if not _ui.confirm(
        f"Register codebotd as a Windows Service via NSSM "
        f"(autostart at boot, no console window — runs in Session 0)?",
        default=True,
    ):
        _ui.check("service", "WARN", "skipped — install by hand later")
        return 0

    if not _is_windows_admin():
        _ui.error(
            "NSSM install requires Administrator privileges. "
            "Re-run `codebot setup` from an elevated cmd / PowerShell "
            "(or accept the UAC prompt)."
        )
        return 1

    # Use the Python interpreter running THIS setup so `-m codebot` resolves
    # to the same env (pip, pipx, uv tool install — all share sys.executable).
    python_exe = sys.executable
    log_dir = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "codebot"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _ui.warn(f"can't create {log_dir}: {e}; NSSM will fall back to its own path")

    install_cmd = [
        str(nssm), "install", WIN_SERVICE_NAME,
        python_exe, "-m", "codebot", "start",
    ]
    try:
        with _ui.spinner(f"nssm install {WIN_SERVICE_NAME} …"):
            subprocess.run(install_cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        _ui.error(f"nssm install failed (rc={e.returncode})")
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        return 1
    except FileNotFoundError:
        _ui.error(f"nssm.exe vanished between {nssm} and exec")
        return 1

    # Configure: auto-start at boot, rotated logs, restart on failure.
    settings = [
        ("DisplayName",           "Code Bot USB info display daemon (CH32X033F8P6)"),
        ("Start",                 "SERVICE_AUTO_START"),
        ("AppStdout",             str(log_dir / "codebotd.out.log")),
        ("AppStderr",             str(log_dir / "codebotd.err.log")),
        ("AppStdoutCreationTime", "0"),
        ("AppStderrCreationTime", "0"),
        ("AppRotateFiles",        "1"),
        ("AppRotateBytes",        "1048576"),
        ("RestartOnFailureDelay", "5000"),
    ]
    for k, v in settings:
        subprocess.run(
            [str(nssm), "set", WIN_SERVICE_NAME, k, v],
            check=False, capture_output=True, text=True,
        )

    # Silent upgrade: if a previous codebot-setup run left the legacy
    # onlogon task behind, drop it so it doesn't double-start the daemon.
    subprocess.run(
        ["schtasks", "/delete", "/tn", WIN_TASK_NAME, "/f"],
        check=False, capture_output=True, text=True,
    )

    # Start the service. `nssm start` returns non-zero if the service is
    # already running or SCM rejects it — warn but don't fail the install.
    try:
        with _ui.spinner(f"nssm start {WIN_SERVICE_NAME} …"):
            r = subprocess.run(
                [str(nssm), "start", WIN_SERVICE_NAME],
                capture_output=True, text=True, check=False,
            )
        if r.returncode != 0:
            _ui.warn(f"nssm start rc={r.returncode}")
            if r.stderr:
                print(r.stderr.strip(), file=sys.stderr)
            _ui.hint([f"    sc start {WIN_SERVICE_NAME}    (manual start)"])
    except FileNotFoundError:
        _ui.error("nssm.exe vanished during start")
        return 1

    # Probe via sc query so the user sees the service state immediately.
    probe = subprocess.run(
        ["sc", "query", WIN_SERVICE_NAME],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 0:
        first = next((ln.strip() for ln in probe.stdout.splitlines() if ln.strip()), "")
        _ui.info(f"  status: {first}")
    else:
        _ui.info(f"  (sc query rc={probe.returncode}; verify with `sc query {WIN_SERVICE_NAME}`)")

    _ui.check("service", "PASS", f"registered service '{WIN_SERVICE_NAME}' (SERVICE_AUTO_START)")
    _ui.hint([
        f"Service runs at boot (no user logon required), no console window.",
        f"Manage via services.msc, or:",
        f"    nssm edit {WIN_SERVICE_NAME}             (open NSSM GUI)",
        f"    sc query {WIN_SERVICE_NAME}",
        f"    sc stop  {WIN_SERVICE_NAME}",
        f"    nssm remove {WIN_SERVICE_NAME} confirm   (uninstall)",
        "",
        f"Logs: {log_dir}\\codebotd.{{out,err}}.log",
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
    """Remove the codebotd Windows Service (NSSM) and, if present, the
    legacy onlogon Task Scheduler task left by older ``codebot setup`` runs."""
    nssm = _find_nssm()

    has_svc = False
    if nssm is not None:
        # sc query returns rc=0 only if the service exists.
        probe = subprocess.run(
            ["sc", "query", WIN_SERVICE_NAME],
            capture_output=True, text=True, check=False,
        )
        has_svc = probe.returncode == 0

    # Detect legacy onlogon task from the previous install path.
    legacy = subprocess.run(
        ["schtasks", "/query", "/tn", WIN_TASK_NAME],
        capture_output=True, text=True, check=False,
    )
    has_legacy_task = legacy.returncode == 0

    if not has_svc and not has_legacy_task:
        _ui.check("service", "INFO", "no codebot service or task to remove")
        return 0

    if not _ui.confirm(
        f"Remove the codebotd Windows Service (and/or legacy task)?",
        default=True,
    ):
        _ui.check("service", "WARN", "kept")
        return 0

    rc = 0
    if has_svc and nssm is not None:
        # Best-effort stop (ignore rc — service may already be stopped).
        subprocess.run(
            [str(nssm), "stop", WIN_SERVICE_NAME],
            capture_output=True, check=False,
        )
        r = subprocess.run(
            [str(nssm), "remove", WIN_SERVICE_NAME, "confirm"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            _ui.check("service", "PASS", f"removed service '{WIN_SERVICE_NAME}'")
        else:
            _ui.warn(f"nssm remove rc={r.returncode}")
            if r.stderr:
                print(r.stderr.strip(), file=sys.stderr)
            rc = 1

    if has_legacy_task:
        try:
            with _ui.spinner(f"schtasks /delete /tn {WIN_TASK_NAME} …"):
                r = subprocess.run(
                    ["schtasks", "/delete", "/tn", WIN_TASK_NAME, "/f"],
                    capture_output=True, text=True, check=False,
                )
        except FileNotFoundError:
            return rc
        if r.returncode == 0:
            _ui.check("service", "PASS", f"removed legacy task '{WIN_TASK_NAME}'")
        else:
            err = (r.stderr or "").lower()
            if "cannot find" in err or "does not exist" in err:
                _ui.check("service", "INFO", f"no task '{WIN_TASK_NAME}' to remove")
            else:
                _ui.warn(f"schtasks /delete rc={r.returncode}")
                if r.stderr:
                    print(r.stderr.strip(), file=sys.stderr)
                rc = 1

    return rc


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