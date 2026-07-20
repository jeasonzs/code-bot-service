"""Action executor base + dispatcher."""

from __future__ import annotations

import subprocess
import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import sys


class ActionResult:
    """Result of executing an action."""

    def __init__(self, success: bool, message: str = "", output: str = "") -> None:
        self.success = success
        self.message = message
        self.output = output

    def __repr__(self) -> str:
        return f"ActionResult(success={self.success}, message={self.message!r}, output={self.output[:50]!r})"


class ActionExecutor(ABC):
    """Base class for action executors."""

    @abstractmethod
    def execute(self, config: dict) -> ActionResult:
        """Execute the action with the given config."""
        ...


class CommandExecutor(ActionExecutor):
    """Execute a shell command.

    IMPORTANT — cross-platform semantics:
      - Windows: ``shell=True`` invokes **PowerShell** (``powershell``).
        Commands MUST be valid PowerShell syntax. Bash-only constructs
        (e.g. ``ls | grep``, ``$VAR``, ``2>&1``) will fail.
      - macOS / Linux: ``bash`` is the default shell. macOS 10.15+
        defaults user shell to zsh, but bash is still on PATH and our
        commands are intentionally POSIX-portable.

    For launching a desktop application cross-platform, prefer
    ``open_app`` (``OpenAppExecutor``); for opening URLs / files,
    ``xdg-open`` / ``open`` / Windows file association is the right
    primitive (use ``command`` action with the platform-specific tool).
    """

    def __init__(self, shell: str = "bash") -> None:
        # Auto-detect shell
        if shell == "auto":
            if sys.platform == "win32":
                shell = "powershell"
            else:
                shell = "bash"
        self.shell = shell

    def execute(self, config: dict) -> ActionResult:
        cmd = config.get("command")
        if not cmd:
            return ActionResult(False, "missing 'command' field")
        try:
            r = subprocess.run(
                cmd, shell=True,
                capture_output=True, text=True, timeout=30,
            )
            return ActionResult(
                success=(r.returncode == 0),
                message=f"exit={r.returncode}",
                output=r.stdout + r.stderr,
            )
        except subprocess.TimeoutExpired:
            return ActionResult(False, "timeout")
        except Exception as e:
            return ActionResult(False, str(e))


class OpenAppExecutor(ActionExecutor):
    """Launch a desktop application by name (cross-platform).

    The ``app`` config value should be the *executable name* (resolved via
    the platform's PATH / launcher), not a URL or document path.

      - **macOS**: ``open -a <app>`` (Launch Services)
      - **Windows**: ``Popen([app])`` — Windows ``CreateProcess`` resolves
        ``app`` against PATH + .exe extension. For apps not on PATH, pass
        an absolute path or ``shell:Application\\app.exe`` shortcut.
        (Previously used ``start <app>`` via cmd.exe — that opens
        *documents/URLs*, not applications; removed in P2.3.)
      - **Linux**: ``Popen([app])`` — runs the executable directly via
        PATH lookup. The previous ``xdg-open`` was misleading because
        ``xdg-open`` is for URLs/files, not launching binaries.

    For URLs / files / documents, use the ``command`` action with the
    platform's appropriate opener (``xdg-open`` / ``open`` /
    Windows file association via ``start``).
    """

    def execute(self, config: dict) -> ActionResult:
        app = config.get("app")
        if not app:
            return ActionResult(False, "missing 'app' field")
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", app])
            elif sys.platform == "win32":
                subprocess.Popen([app])  # CreateProcess resolves PATH + .exe
            else:  # Linux
                subprocess.Popen([app])  # direct exec; PATH lookup
            return ActionResult(True, f"opened {app}")
        except Exception as e:
            return ActionResult(False, str(e))


class HIDKeystrokesExecutor(ActionExecutor):
    """Send HID keyboard reports (handled by daemon, not by this executor)."""

    def execute(self, config: dict) -> ActionResult:
        text = config.get("text", "")
        if not text:
            return ActionResult(False, "missing 'text' field")
        # This is a marker - actual sending is done by daemon
        return ActionResult(True, f"will_type:{text}")


def get_executor(action_type: str) -> Optional[ActionExecutor]:
    """Get an executor for the given action type."""
    if action_type == "command":
        return CommandExecutor()
    elif action_type == "open_app":
        return OpenAppExecutor()
    elif action_type == "hid_keystrokes":
        return HIDKeystrokesExecutor()
    return None
