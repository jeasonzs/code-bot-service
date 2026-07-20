"""Cross-platform USB driver / permission installer for Code Bot device.

Invoked by ``codebotd setup-driver``. Three branches:

  Linux  — copy udev/99-codebot.rules to /etc/udev/rules.d/ (needs sudo),
            reload udev, suggest plugdev group membership.
  macOS  — no install needed (pyusb uses IOKit). Guide user through the
            first-plug TCC prompt, and provide a reset hint.
  Windows — install windows/codebot-inface0.inf via pnputil (needs
            Administrator), binding interface 0 (Vendor) to WinUSB while
            leaving interface 1 (HID Keyboard) on the system default.

Always runs ``doctor`` first to surface environment issues before
attempting driver changes.

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


log = logging.getLogger("codebot.driver_setup")


# ==============================================================
# Paths
# ==============================================================
def _udev_rules_src() -> Path:
    """Resolve the udev rules file path, copying to a temp file if needed.

    importlib.resources returns a Traversable; udev wants a real path on
    disk, so for zip-installed wheels we materialize to a temp file first.
    """
    traversable = files("codebot") / "udev" / "99-codebot.rules"
    # str() works on flat-installed wheels; for zip-installed we copy out.
    try:
        p = Path(str(traversable))
        if p.exists():
            return p
    except OSError:
        pass
    # Fallback: extract to temp file (zip-installed wheel)
    with tempfile.NamedTemporaryFile(
        prefix="99-codebot.", suffix=".rules", delete=False, mode="w",
    ) as tmp:
        tmp.write(traversable.read_text(encoding="utf-8"))
        return Path(tmp.name)


def _windows_inf_src() -> Path:
    """Resolve the WinUSB INF file path, materializing to temp if needed."""
    traversable = files("codebot") / "windows" / "codebot-inface0.inf"
    try:
        p = Path(str(traversable))
        if p.exists():
            return p
    except OSError:
        pass
    with tempfile.NamedTemporaryFile(
        prefix="codebot-inface0.", suffix=".inf", delete=False, mode="w",
    ) as tmp:
        tmp.write(traversable.read_text(encoding="utf-8"))
        return Path(tmp.name)


# ==============================================================
# Linux
# ==============================================================
def _setup_linux(assume_yes: bool) -> int:
    src = _udev_rules_src()
    if not src.exists():
        print(f"[setup-driver] ERROR: udev rules not found at {src}", file=sys.stderr)
        return 2

    target_root = Path("/etc/udev/rules.d/99-codebot.rules")
    target_user = Path.home() / ".config/udev/rules.d/99-codebot.rules"

    if os.geteuid() == 0:
        # Already root: install directly.
        target_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target_root)
        print(f"[setup-driver] Installed {target_root}")
        _reload_udev()
        _print_plugdev_hint()
        return 0

    # Non-root: try user-level first (no sudo needed), or instruct sudo.
    print("[setup-driver] Linux: installing user-level udev rules (no sudo needed).")
    print("              For system-wide visibility, re-run with `sudo codebotd setup-driver`.")
    target_user.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, target_user)
    print(f"[setup-driver] Installed {target_user}")

    if not assume_yes:
        print()
        print("  ⚠ User-level udev rules only apply when the user runs a session")
        print("    manager that loads them. For most distros (systemd), run with")
        print("    sudo instead:")
        print()
        print("      sudo codebotd setup-driver")
        print()
        print("    Already done? Press Enter to continue, Ctrl+C to abort.")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n[setup-driver] aborted", file=sys.stderr)
            return 1

    _reload_udev_user_or_system()
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
        print("[setup-driver] udev rules reloaded")
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"[setup-driver] WARN: udev reload failed ({e}); reboot to apply")


def _reload_udev_user_or_system() -> None:
    # User-level reload is best-effort; many distros require session manager.
    _reload_udev()


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
    print("[setup-driver] macOS: no driver install needed (pyusb uses Apple's IOKit).")
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
        print(f"[setup-driver] ERROR: INF not found at {inf}", file=sys.stderr)
        return 2

    print(f"[setup-driver] Windows: installing {inf}")
    print("              (binds interface 0 = Vendor to WinUSB; HID untouched)")
    print()

    # Check for admin rights. On Windows, only admins can run pnputil.
    is_admin = _is_windows_admin()
    if not is_admin:
        print("  ⚠ This step needs Administrator privileges.")
        print("    Re-run from an Administrator PowerShell or cmd:")
        print()
        print("      codebotd setup-driver --yes")
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
        print("[setup-driver] ERROR: pnputil not found in PATH", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("[setup-driver] ERROR: pnputil timed out after 60s", file=sys.stderr)
        return 2

    print(r.stdout)
    if r.returncode != 0:
        print(f"[setup-driver] pnputil exit={r.returncode}", file=sys.stderr)
        if r.stderr:
            print(r.stderr, file=sys.stderr)
        if not is_admin:
            return 1  # user action required
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
# Entry point
# ==============================================================
HANDLERS: dict[str, Callable[[bool], int]] = {
    "linux": _setup_linux,
    "darwin": _setup_macos,
    "win32": _setup_windows,
}


def run_setup(assume_yes: bool = False) -> int:
    """Run the platform-appropriate driver/permission installer.

    Returns 0 on success, 1 if user action is required, 2 on fatal error.
    """
    handler = HANDLERS.get(sys.platform)
    if handler is None:
        print(f"[setup-driver] unsupported platform: {sys.platform}", file=sys.stderr)
        return 2

    # Always run doctor first so users see environment issues immediately.
    print("[setup-driver] Running `codebotd doctor` first...")
    print()
    from .doctor import run_doctor
    doctor_rc = run_doctor(verbose=True)
    print()
    if doctor_rc != 0:
        print("[setup-driver] doctor reported FAILs. Fix those first, then re-run setup-driver.",
              file=sys.stderr)
        return 1

    return handler(assume_yes)


if __name__ == "__main__":
    sys.exit(run_setup())
