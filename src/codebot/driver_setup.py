"""Per-platform USB driver / permission installer for Code Bot.

Invoked indirectly by ``codebotd setup`` (phase 2/4). Three branches:

  Linux    — copy udev/99-codebot.rules to /etc/udev/rules.d/ (needs sudo) or
              ~/.config/udev/rules.d/ (no sudo, user-level); reload udev;
              suggest plugdev group membership.
  macOS    — no install needed (pyusb uses IOKit). Guide user through the
              first-plug TCC prompt and provide a reset hint.
  Windows  — no install needed. The firmware exposes MS OS 2.0 Descriptors
              that cause Windows 8.1+ to bind Interface 0 (Vendor Bulk) to
              the inbox ``winusb.sys`` driver on first plug. Interface 1
              (HID Keyboard) is bound by the standard HID class driver.
              No INF, no admin shell, no signature — the device is driver-
              free from the moment it's plugged in.

The doctor pre-flight and the failure-aggregation with subsequent phases
(service / claude) live in ``codebot.setup.run_setup``; this module
exposes ``run_driver_setup()`` as a single entry point and does not import
the rest of the wizard.

Prompting goes through ``codebot._ui``, which decides whether we're
interactive. Note the ordering rule documented there: every prompt must
have returned before ``run_as_root`` spawns sudo, or sudo's password
prompt collides with prompt_toolkit's raw mode.

Return codes (POSIX convention):
  0 = success
  1 = user action required (sudo / UAC / declined a prompt)
  2 = fatal error
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Callable


log = logging.getLogger("codebot.driver_setup")

from . import _ui  # noqa: E402 — wizard UI primitives, after logger
from .os_helper import run_as_root  # noqa: E402 — privilege helper, after logger


# ==============================================================
# Asset resolution
# ==============================================================
def _udev_rules_src() -> Path:
    try:
        from importlib.resources import files
    except ImportError:  # pragma: no cover — only on Python < 3.9
        from importlib_resources import files  # type: ignore[no-redef]
    return files("codebot") / "udev" / "99-codebot.rules"


# ==============================================================
# Linux
# ==============================================================
def _setup_linux() -> int:
    src = _udev_rules_src()
    if not Path(str(src)).exists():
        _ui.error(f"udev rules not found at {src}")
        return 2

    target = Path("/etc/udev/rules.d/99-codebot.rules")

    _ui.check(
        "udev rule",
        "INFO",
        f"already present at {target}" if target.exists() else f"not installed ({target})",
    )

    # Confirm BEFORE touching /etc/. Also: this prompt must fully return
    # before run_as_root() below — sudo reads its password from /dev/tty and
    # would collide with prompt_toolkit's raw mode. See codebot._ui.
    if not _ui.confirm(
        f"Write the USB udev rule to {target}? (needs sudo)",
        default=True,
    ):
        _ui.check("udev rule", "WARN", "skipped — the device stays root-only")
        _ui.hint([
            "Without the rule, codebotd can only reach the device as root.",
            "Re-run `codebotd setup` later, or install it by hand:",
            f"  sudo install -m 644 {src} {target}",
        ])
        return 1

    try:
        run_as_root(
            "install",
            "-m", "644",
            str(src),
            str(target),
        )
    except FileNotFoundError:
        _ui.error("sudo not found")
        return 2
    except RuntimeError as e:
        # run_as_root raises this when sudo is missing from PATH entirely.
        _ui.error(str(e))
        return 2
    except subprocess.CalledProcessError as e:
        _ui.error(f"failed to install udev rule (exit={e.returncode})")
        return 1

    _ui.check("udev rule", "PASS", f"installed {target}")

    _reload_udev()
    _print_plugdev_hint()
    return 0


def _reload_udev() -> None:
    # No prompting inside this function: it only spawns sudo subprocesses,
    # which must never overlap a live prompt_toolkit session.
    try:
        with _ui.spinner("Reloading udev rules…"):
            run_as_root(
                "udevadm",
                "control",
                "--reload-rules",
            )

            run_as_root(
                "udevadm",
                "trigger",
            )

        _ui.check("udev reload", "PASS", "rules reloaded")

    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
        _ui.check("udev reload", "WARN", f"failed ({e}); reboot to apply")


def _print_plugdev_hint() -> None:
    _ui.hint([
        "Optional: add yourself to the plugdev group to access the device",
        "without root, then log out and back in:",
        "",
        "    sudo usermod -aG plugdev $USER",
        "",
        "Plug in the device (or re-plug if already connected).",
        "Verify with `codebotd doctor` — USB device scan should show PASS.",
    ])


# ==============================================================
# macOS
# ==============================================================
def _setup_macos() -> int:
    _ui.check("driver", "PASS", "no install needed (pyusb uses Apple's IOKit)")
    _ui.hint([
        "First time you plug in the Code Bot device, macOS will show a",
        "permission dialog:",
        "",
        '    "Allow accessory to connect?" → click Allow',
        "",
        "If you previously denied the prompt, reset the TCC entry:",
        "",
        "    sudo killall usbd",
        "    # then unplug and re-plug the device",
        "",
        "Verify with `codebotd doctor`.",
    ])
    return 0


# ==============================================================
# Windows
# ==============================================================
def _setup_windows() -> int:
    """Windows driver setup is a no-op — MS OS 2.0 handles binding.

    The firmware's BOS + MS OS 2.0 Descriptor Set declares Interface 0 as
    WinUSB-compatible. On Windows 8.1+ the host requests those descriptors
    at enumeration time and binds Interface 0 to inbox ``winusb.sys``
    automatically — no INF, no admin shell, no signature. Interface 1
    (HID Keyboard) is bound by the standard HID class driver the same way
    every USB keyboard is.

    What this function still does:
      - surfaces a clear status so users can tell "no install" from
        "skip happened silently"
      - points users at `codebotd doctor` if a probe fails
    """
    _ui.check("driver", "PASS", "no install needed (MS OS 2.0 → inbox WinUSB)")
    _ui.hint([
        "Code Bot uses Microsoft OS 2.0 Descriptors to declare Interface 0",
        "(Vendor Bulk) as WinUSB-compatible. Windows 8.1+ binds it to the",
        "inbox winusb.sys driver on first plug — no INF, no admin shell.",
        "",
        "If `codebotd doctor` does not see the device:",
        "  • replug once (host may need a fresh enumeration)",
        "  • check Device Manager → Universal Serial Bus devices",
        "  • verify Windows version ≥ 8.1",
    ])
    return 0


# ==============================================================
# Teardown (reverse of install)
# ==============================================================
def _teardown_linux() -> int:
    """Remove the system udev rule, then reload udev.

    We only touch /etc/udev/rules.d/ — that's the only path setup ever
    writes to (user-level rules under ~/.config/udev/rules.d/ are never
    installed, so there's nothing to remove there).
    """
    target = Path("/etc/udev/rules.d/99-codebot.rules")
    if not target.exists():
        _ui.check("udev rule", "INFO", "no system rule to remove")
        return 0

    # Confirm before the sudo rm; prompt must return before run_as_root.
    if not _ui.confirm(f"Remove {target}? (needs sudo)", default=True):
        _ui.check("udev rule", "WARN", "kept")
        return 0

    try:
        run_as_root("rm", str(target))
        _ui.check("udev rule", "PASS", f"removed {target}")
    except (FileNotFoundError, RuntimeError) as e:
        _ui.error(f"sudo unavailable ({e})")
        return 2
    except subprocess.CalledProcessError as e:
        _ui.check("udev rule", "WARN", f"cannot remove {target} (exit={e.returncode})")
        return 1
    _reload_udev_teardown()
    return 0


def _reload_udev_teardown() -> None:
    """Best-effort udev reload after rule removal (silent on failure).

    Same sudo-prefix treatment as ``_reload_udev`` — silent on failure
    so teardown's "best-effort, clean up as much as possible" contract
    holds even when the user only has partial sudo auth.
    """
    try:
        run_as_root("udevadm", "control", "--reload-rules")
        run_as_root("udevadm", "trigger")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass


def _teardown_macos() -> int:
    """No driver was installed; nothing to undo. Print TCC reset hint."""
    _ui.check("driver", "INFO", "nothing to undo (no driver was installed)")
    _ui.hint([
        "If you previously denied the TCC prompt and want to reset it:",
        "",
        "    sudo killall usbd",
    ])
    return 0


def _teardown_windows() -> int:
    """No driver was installed on Windows; nothing to undo.

    The MS OS 2.0 → inbox winusb.sys binding lives in the device's USB
    descriptors and the host's per-device state. Unplugging the device
    is the only "teardown" Windows recognises; replug rebinds cleanly.
    """
    _ui.check("driver", "INFO", "nothing to undo (no INF was installed)")
    _ui.hint([
        "If the WinUSB binding got into a bad state, just unplug the",
        "device for a few seconds and replug — Windows rebinds cleanly.",
    ])
    return 0


# ==============================================================
# Dispatch
# ==============================================================
_DRIVER_HANDLERS: dict[str, Callable[..., int]] = {
    "linux": _setup_linux,
    "darwin": _setup_macos,
    "win32": _setup_windows,
}

_DRIVER_TEARDOWN_HANDLERS: dict[str, Callable[..., int]] = {
    "linux": _teardown_linux,
    "darwin": _teardown_macos,
    "win32": _teardown_windows,
}


def run_driver_setup() -> int:
    """Run the platform-appropriate driver/permission installer.

    Called by ``codebot.setup.run_setup`` (phase 2/5). Does NOT run
    doctor — that's the orchestrator's responsibility. Whether the handler
    prompts is decided by ``codebot._ui.is_interactive()``.
    """
    handler = _DRIVER_HANDLERS.get(sys.platform)
    if handler is None:
        _ui.error(f"unsupported platform: {sys.platform}")
        return 2
    return handler()


def run_driver_teardown() -> int:
    """Reverse of ``run_driver_setup``. Called by teardown.py."""
    handler = _DRIVER_TEARDOWN_HANDLERS.get(sys.platform)
    if handler is None:
        _ui.error(f"unsupported platform: {sys.platform}")
        return 2
    return handler()


if __name__ == "__main__":
    sys.exit(run_driver_setup())