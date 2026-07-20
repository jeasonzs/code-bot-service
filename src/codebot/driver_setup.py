"""Per-platform USB driver / permission installer for Code Bot.

Invoked indirectly by ``codebotd setup`` (phase 2/4). Three branches:

  Linux    — copy udev/99-codebot.rules to /etc/udev/rules.d/ (needs sudo) or
              ~/.config/udev/rules.d/ (no sudo, user-level); reload udev;
              suggest plugdev group membership.
  macOS    — no install needed (pyusb uses IOKit). Guide user through the
              first-plug TCC prompt and provide a reset hint.
  Windows  — install windows/codebot-inface0.inf via pnputil (needs
              Administrator), binding interface 0 (Vendor) to WinUSB while
              leaving interface 1 (HID Keyboard) on the system default.

The doctor pre-flight and the failure-aggregation with subsequent phases
(service / claude) live in ``codebot.setup.run_setup``; this module
exposes ``run_driver_setup(assume_yes)`` as a single entry point and does
not import the rest of the wizard.

Return codes (POSIX convention):
  0 = success
  1 = user action required (sudo / UAC / system prompt to acknowledge)
  2 = fatal error
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Callable

from ._paths import real_user_home


log = logging.getLogger("codebot.driver_setup")


# ==============================================================
# Asset resolution
# ==============================================================
def _materialize_traversable(traversable) -> Path:
    """Return a real on-disk Path for an importlib Traversable.

    Flat-installed wheels (``pip install codebot``) expose Traversables as
    filesystem paths and ``str(t)`` returns a usable path. Zip-installed
    wheels don't, so we copy the content into a NamedTemporaryFile.
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


def _udev_rules_src() -> Path:
    return _materialize_traversable(files("codebot") / "udev" / "99-codebot.rules")


def _windows_inf_src() -> Path:
    return _materialize_traversable(files("codebot") / "windows" / "codebot-inface0.inf")


# ==============================================================
# Linux
# ==============================================================
def _setup_linux(assume_yes: bool) -> int:
    src = _udev_rules_src()
    if not src.exists():
        print(f"[setup.driver] ERROR: udev rules not found at {src}", file=sys.stderr)
        return 2

    target_root = Path("/etc/udev/rules.d/99-codebot.rules")
    target_user = real_user_home() / ".config/udev/rules.d/99-codebot.rules"

    if os.geteuid() == 0:
        target_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target_root)
        print(f"[setup.driver] Installed {target_root}")
        _reload_udev()
        _print_plugdev_hint()
        return 0

    # Non-root: install user-level rules; suggest sudo for system-wide visibility.
    print("[setup.driver] Linux: installing user-level udev rules (no sudo needed).")
    print("              For system-wide visibility, re-run with `sudo codebotd setup`.")
    target_user.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, target_user)
    print(f"[setup.driver] Installed {target_user}")

    if not assume_yes:
        print()
        print("  ⚠ User-level udev rules only apply when the user runs a session")
        print("    manager that loads them. For most distros (systemd), run with")
        print("    sudo instead:")
        print()
        print("      sudo codebotd setup")
        print()
        print("    Already done? Press Enter to continue, Ctrl+C to abort.")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n[setup.driver] aborted", file=sys.stderr)
            return 1

    _reload_udev()
    _print_plugdev_hint()
    return 0


def _reload_udev() -> None:
    try:
        subprocess.run(
            ["udevadm", "control", "--reload-rules"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["udevadm", "trigger"],
            check=True, capture_output=True,
        )
        print("[setup.driver] udev rules reloaded")
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"[setup.driver] WARN: udev reload failed ({e}); reboot to apply")


def _print_plugdev_hint() -> None:
    print()
    print("  Optional: add yourself to the plugdev group to access the device")
    print("  without root, then log out and back in:")
    print()
    print(f"    sudo usermod -aG plugdev $USER")
    print()
    print("  Plug in the device (or re-plug if already connected).")
    print("  Verify with `codebotd doctor` — USB device scan should show PASS.")


# ==============================================================
# macOS
# ==============================================================
def _setup_macos(assume_yes: bool) -> int:
    print("[setup.driver] macOS: no driver install needed (pyusb uses Apple's IOKit).")
    print()
    print("  First time you plug in the Code Bot device, macOS will show a")
    print("  permission dialog:")
    print()
    print('    "Allow accessory to connect?" → click Allow')
    print()
    print("  If you previously denied the prompt, reset the TCC entry:")
    print()
    print("    sudo killall usbd")
    print("    # then unplug and re-plug the device")
    print()
    print("  Verify with `codebotd doctor`.")
    return 0


# ==============================================================
# Windows
# ==============================================================
def _setup_windows(assume_yes: bool) -> int:
    inf = _windows_inf_src()
    if not inf.exists():
        print(f"[setup.driver] ERROR: INF not found at {inf}", file=sys.stderr)
        return 2

    print(f"[setup.driver] Windows: installing {inf}")
    print("              (binds interface 0 = Vendor to WinUSB; HID untouched)")
    print()

    is_admin = _is_windows_admin()
    if not is_admin:
        print("  ⚠ This step needs Administrator privileges.")
        print("    Re-run from an Administrator PowerShell or cmd:")
        print()
        print("      codebotd setup --interactive")
        print()
        if not assume_yes:
            print("  Press Enter to attempt anyway (will fail if not elevated)...")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                return 1

    try:
        r = subprocess.run(
            ["pnputil", "/add-driver", str(inf), "/install"],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        print("[setup.driver] ERROR: pnputil not found in PATH", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("[setup.driver] ERROR: pnputil timed out after 60s", file=sys.stderr)
        return 2

    print(r.stdout)
    if r.returncode != 0:
        print(f"[setup.driver] pnputil exit={r.returncode}", file=sys.stderr)
        if r.stderr:
            print(r.stderr, file=sys.stderr)
        if not is_admin:
            return 1
        return 2

    print()
    print("  ✓ Driver installed. Replug the device (or run:")
    print('    pnputil /scan-devices')
    print("    ) and verify with `codebotd doctor`.")
    return 0


def _is_windows_admin() -> bool:
    """Best-effort admin check without requiring pywin32."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


# ==============================================================
# Teardown (reverse of install)
# ==============================================================
def _teardown_linux(assume_yes: bool) -> int:
    """Remove udev rule files at both system and user paths, then reload udev."""
    targets = [
        Path("/etc/udev/rules.d/99-codebot.rules"),
        real_user_home() / ".config/udev/rules.d/99-codebot.rules",
    ]
    rc = 0
    for p in targets:
        if not p.exists():
            continue
        try:
            p.unlink()
            print(f"[teardown.driver] Removed {p}")
        except PermissionError:
            print(f"[teardown.driver] WARN: cannot remove {p} (need sudo)",
                  file=sys.stderr)
            rc = max(rc, 1)
    _reload_udev_teardown()
    return rc


def _reload_udev_teardown() -> None:
    """Best-effort udev reload after rule removal (silent on failure)."""
    try:
        subprocess.run(["udevadm", "control", "--reload-rules"],
                       check=True, capture_output=True)
        subprocess.run(["udevadm", "trigger"],
                       check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass


def _teardown_macos(assume_yes: bool) -> int:
    """No driver was installed; nothing to undo. Print TCC reset hint."""
    print("[teardown.driver] macOS: nothing to undo (no driver was installed).")
    print("  If you previously denied the TCC prompt and want to reset it:")
    print()
    print("    sudo killall usbd")
    print()
    return 0


def _teardown_windows(assume_yes: bool) -> int:
    """Remove the WinUSB binding via pnputil. Needs Administrator."""
    if not _is_windows_admin():
        print("[teardown.driver] WARN: removing the WinUSB binding requires "
              "Administrator. Re-run from an Administrator PowerShell:")
        print()
        print("      codebotd teardown --interactive")
        print()
        return 1

    try:
        r = subprocess.run(
            ["pnputil", "/remove-device", r"USB\VID_1A86&PID_CB0B"],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        print("[teardown.driver] ERROR: pnputil not found in PATH", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("[teardown.driver] ERROR: pnputil timed out after 60s", file=sys.stderr)
        return 2

    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0:
        # rc != 0 can mean device not currently bound; that's OK.
        if r.stderr:
            print(f"[teardown.driver] pnputil exit={r.returncode}: {r.stderr.strip()}",
                  file=sys.stderr)
        return 1
    print("[teardown.driver] Removed WinUSB binding for USB\\VID_1A86&PID_CB0B")
    print("  Replug the device — Windows will fall back to the default driver.")
    return 0


# ==============================================================
# Dispatch
# ==============================================================
_DRIVER_HANDLERS: dict[str, Callable[[bool], int]] = {
    "linux": _setup_linux,
    "darwin": _setup_macos,
    "win32": _setup_windows,
}

_DRIVER_TEARDOWN_HANDLERS: dict[str, Callable[[bool], int]] = {
    "linux": _teardown_linux,
    "darwin": _teardown_macos,
    "win32": _teardown_windows,
}


def run_driver_setup(assume_yes: bool = True) -> int:
    """Run the platform-appropriate driver/permission installer.

    Called by ``codebot.setup.run_setup`` (phase 2/4). Does NOT run
    doctor — that's the orchestrator's responsibility.
    """
    handler = _DRIVER_HANDLERS.get(sys.platform)
    if handler is None:
        print(f"[setup.driver] unsupported platform: {sys.platform}", file=sys.stderr)
        return 2
    return handler(assume_yes)


def run_driver_teardown(assume_yes: bool = True) -> int:
    """Reverse of ``run_driver_setup``. Called by teardown.py."""
    handler = _DRIVER_TEARDOWN_HANDLERS.get(sys.platform)
    if handler is None:
        print(f"[teardown.driver] unsupported platform: {sys.platform}",
              file=sys.stderr)
        return 2
    return handler(assume_yes)


if __name__ == "__main__":
    sys.exit(run_driver_setup())