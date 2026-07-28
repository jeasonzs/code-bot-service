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
import tempfile
# Python 3.8 ships `importlib.resources` without `files()` (added in 3.9);
# fall back to the backport so the package installs cleanly on 3.8 too.
try:
    from importlib.resources import files
except ImportError:  # pragma: no cover — only hit on Python < 3.9
    from importlib_resources import files  # type: ignore[no-redef]
from pathlib import Path
from typing import Callable


log = logging.getLogger("codebot.driver_setup")

from . import _ui  # noqa: E402 — wizard UI primitives, after logger
from .os_helper import run_as_root  # noqa: E402 — privilege helper, after logger


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
def _setup_linux() -> int:
    src = _udev_rules_src()
    if not src.exists():
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
    inf = _windows_inf_src()
    if not inf.exists():
        _ui.error(f"INF not found at {inf}")
        return 2

    _ui.check("driver", "INFO", f"will install {inf}")
    _ui.info("binds interface 0 (Vendor) to WinUSB; HID interface untouched")

    is_admin = _is_windows_admin()
    if not is_admin:
        _ui.warn("this step needs Administrator privileges")
        _ui.hint([
            "Re-run from an Administrator PowerShell or cmd:",
            "",
            "      codebotd setup",
        ])
        # Default False: attempting without elevation just fails noisily.
        if not _ui.confirm(
            "Attempt the install anyway? (will fail if not elevated)",
            default=False,
        ):
            _ui.check("driver", "WARN", "skipped — re-run as Administrator")
            return 1

    # Prompting is done; safe to spawn the subprocess now.
    try:
        with _ui.spinner("Running pnputil /add-driver…"):
            r = subprocess.run(
                ["pnputil", "/add-driver", str(inf), "/install"],
                capture_output=True, text=True, timeout=60,
            )
    except FileNotFoundError:
        _ui.error("pnputil not found in PATH")
        return 2
    except subprocess.TimeoutExpired:
        _ui.error("pnputil timed out after 60s")
        return 2

    if r.stdout.strip():
        _ui.info(r.stdout.strip())
    if r.returncode != 0:
        _ui.error(f"pnputil exit={r.returncode}")
        if r.stderr:
            print(r.stderr, file=sys.stderr)
        if not is_admin:
            return 1
        return 2

    _ui.check("driver", "PASS", "WinUSB binding installed")
    _ui.hint([
        "Replug the device (or run: pnputil /scan-devices)",
        "and verify with `codebotd doctor`.",
    ])
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
    """Remove the WinUSB binding via pnputil. Needs Administrator."""
    if not _is_windows_admin():
        _ui.check("driver", "WARN", "removing the WinUSB binding needs Administrator")
        _ui.hint([
            "Re-run from an Administrator PowerShell:",
            "",
            "      codebotd teardown",
        ])
        return 1

    if not _ui.confirm(
        r"Remove the WinUSB binding for USB\VID_1A86&PID_CB0B?",
        default=True,
    ):
        _ui.check("driver", "WARN", "kept")
        return 0

    try:
        with _ui.spinner("Running pnputil /remove-device…"):
            r = subprocess.run(
                ["pnputil", "/remove-device", r"USB\VID_1A86&PID_CB0B"],
                capture_output=True, text=True, timeout=60,
            )
    except FileNotFoundError:
        _ui.error("pnputil not found in PATH")
        return 2
    except subprocess.TimeoutExpired:
        _ui.error("pnputil timed out after 60s")
        return 2

    if r.stdout.strip():
        _ui.info(r.stdout.strip())
    if r.returncode != 0:
        # rc != 0 can mean device not currently bound; that's OK.
        _ui.check("driver", "WARN", f"pnputil exit={r.returncode}")
        if r.stderr:
            print(r.stderr.strip(), file=sys.stderr)
        return 1
    _ui.check("driver", "PASS", r"removed WinUSB binding for USB\VID_1A86&PID_CB0B")
    _ui.info("Replug the device — Windows falls back to the default driver.")
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