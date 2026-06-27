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
    """Execute a shell command (bash/zsh/powershell/cmd)."""

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
    """Open an application."""

    def execute(self, config: dict) -> ActionResult:
        app = config.get("app")
        if not app:
            return ActionResult(False, "missing 'app' field")
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", app])
            elif sys.platform == "win32":
                subprocess.Popen(["start", app], shell=True)
            else:  # Linux
                subprocess.Popen(["xdg-open", app])
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
