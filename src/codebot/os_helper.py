"""OS-level helpers shared across the install / teardown wizards.

Currently holds one entry point, ``run_as_root``, which wraps shell
commands (install, rm, udevadm, ...) with ``sudo`` when the current
process isn't already root. The python interpreter we're running lives
in the user's env (venv / ``~/.local/``) and does NOT have ``codebot``
importable under root, so we never ``sudo`` python — only the specific
shell commands that need root for filesystem / device-mgr reasons.

Scope is intentionally POSIX-only: macOS and Linux both have
``os.geteuid()`` and ``sudo``, so the helper works on both. Windows uses
UAC for elevation (a fundamentally different mechanism), so Windows
branches in ``driver_setup`` keep their existing
``subprocess.run([..., "pnputil", ...])`` direct-call pattern — that
path requires the user to launch from an Administrator shell.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def run_as_root(*args: str) -> subprocess.CompletedProcess:
    """Run a command directly as root, or through ``sudo``.

    The python interpreter we're running lives in the user's env (venv /
    ``~/.local/``) and does NOT have ``codebot`` importable under root.
    So we never ``sudo`` python — only shell commands (``install``,
    ``rm``, ``udevadm``, ...) that need root for filesystem / device-mgr
    reasons. This keeps the wizard's importable code in user-land while
    still landing files where the kernel / udev expects them.

    Already-running-as-root path (explicit ``sudo codebotd setup`` or
    re-invoked under sudo): runs ``args`` directly with no prefix.

    Non-root path: prepends ``sudo`` so the command runs as root. Uses
    ``subprocess.run(..., check=True)`` so a non-zero exit (sudo auth
    failed, command not found, etc.) raises ``CalledProcessError`` for
    the caller to handle. Raises ``RuntimeError`` if ``sudo`` is missing
    on PATH — that means the install is impossible without manual root
    work, and the wizard can't paper over it.
    """
    if os.geteuid() == 0:
        cmd = list(args)
    else:
        sudo = shutil.which("sudo")
        if sudo is None:
            raise RuntimeError("sudo is required to install udev rules")
        cmd = [sudo, *args]

    return subprocess.run(cmd, check=True, capture_output=True, text=True)