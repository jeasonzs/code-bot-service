"""Discover the user's graphical session env when the daemon has none of its own.

When ``codebotd`` runs as a background daemon (systemd user service,
``nohup``, ``setsid``, SSH login without ``-X``), its own ``os.environ``
is missing the keys a GUI app needs to actually open a window:
``DISPLAY`` / ``WAYLAND_DISPLAY``, ``XAUTHORITY``, ``DBUS_SESSION_BUS_ADDRESS``.
``gtk-launch`` then dies on "cannot open display"; Chrome dies on
"Missing X server or $DISPLAY".

This module sniffs the host for the user's *graphical* session and
returns a dict of the missing keys. The caller merges it over its own
env — already-set keys win, discovered keys fill the gaps.

Discovery order (first hit wins for each key):

  1. ``loginctl list-sessions`` / ``show-session`` — leader PID's
     ``/proc/<pid>/environ`` is the canonical source. Works for the
     common case where the same user is logged in graphically and via
     SSH. Cross-user reads fail by permission, which is expected.
  2. ``$XDG_RUNTIME_DIR/bus`` and ``$XDG_RUNTIME_DIR/wayland-*`` —
     dbus session bus and Wayland socket, present whenever the user
     has a graphical session.
  3. ``/tmp/.X11-unix/X<n>`` — each X socket maps to ``DISPLAY=:n``.
  4. ``~/.Xauthority`` — X11 auth cookie.

This is best-effort. If nothing is found we return an empty dict;
``Popen`` then inherits whatever the daemon already has, which is the
status quo before this module existed.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Callable


log = logging.getLogger("codebot.session")


# Keys we care about. Anything else in the leader's environ is noise
# (LANG, PATH, ...) — we don't want to leak unrelated state.
_GUI_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS")


# (terminal_path, cmd) -> full argv. Each platform has its own
# quoting / run-then-pause idiom, captured per-builder.
TerminalArgvBuilder = Callable[[str, str], list[str]]


def _linux_xterm_family_argv(terminal_path: str, cmd: str) -> list[str]:
    """[path, -e, bash, -c, inner] — most Linux terminals (x-terminal-emulator, konsole, …)."""
    inner = f"{cmd}; echo; echo '[Enter to close this window]'; read"
    return [terminal_path, "-e", "bash", "-c", inner]


def _linux_gnome_terminal_argv(terminal_path: str, cmd: str) -> list[str]:
    """[path, --, bash, -c, inner] — gnome-terminal uses ``--`` separator (consumes rest)."""
    inner = f"{cmd}; echo; echo '[Enter to close this window]'; read"
    return [terminal_path, "--", "bash", "-c", inner]


def _applescript_escape(s: str) -> str:
    """AppleScript string literal escaping: backslash + double-quote."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _osascript_argv(terminal_path: str, cmd: str) -> list[str]:
    """[osascript, -e, AppleScript] — Terminal.app ``do script`` opens new window.

    Terminal.app itself runs in the user's GUI session, so ``inner``
    inherits DISPLAY/PATH/etc. — the daemon doesn't need to inject env.
    """
    inner = f"{cmd}; echo; echo '[Enter to close this window]'; read"
    script = f'tell application "Terminal" to do script "{_applescript_escape(inner)}"'
    return [terminal_path, "-e", script]


def _cmd_inner(cmd: str) -> str:
    """cmd.exe inner: ``cmd & echo. & echo [Enter…] & pause``."""
    return f'{cmd}& echo.& echo [Enter to close this window]& pause'


def _powershell_inner(cmd: str) -> str:
    """PowerShell inner: ``cmd ; "" ; "Press Enter…" ; Read-Host``."""
    return f'{cmd}; ""; "Press Enter to close this window"; Read-Host'


def _wt_argv(terminal_path: str, cmd: str) -> list[str]:
    """[wt, -d, ., cmd, /k, inner] — Windows Terminal opens new tab with cmd.exe, /k 保持窗口."""
    return [terminal_path, "-d", ".", "cmd", "/k", _cmd_inner(cmd)]


def _powershell_argv(terminal_path: str, cmd: str) -> list[str]:
    """[powershell, -NoExit, -Command, inner] — PowerShell 窗口跑完不退出."""
    return [terminal_path, "-NoExit", "-Command", _powershell_inner(cmd)]


def _cmd_argv(terminal_path: str, cmd: str) -> list[str]:
    """[cmd, /k, inner] — fallback 老 cmd.exe."""
    return [terminal_path, "/k", _cmd_inner(cmd)]


# (binary, builder_callable); ``x-terminal-emulator`` is the freedesktop
# wrapper distros point at the user's preferred terminal.
_TERMINAL_CANDIDATES: tuple[tuple[str, TerminalArgvBuilder], ...] = (
    *([("osascript", _osascript_argv)] if sys.platform == "darwin" else ()),
    *([("wt", _wt_argv), ("powershell", _powershell_argv), ("cmd", _cmd_argv)]
      if sys.platform == "win32" else ()),
    ("x-terminal-emulator", _linux_xterm_family_argv),
    ("konsole",             _linux_xterm_family_argv),
    ("xfce4-terminal",      _linux_xterm_family_argv),
    ("alacritty",           _linux_xterm_family_argv),
    ("foot",                _linux_xterm_family_argv),
    ("xterm",               _linux_xterm_family_argv),
    ("gnome-terminal",      _linux_gnome_terminal_argv),
)


def detect_terminal_emulator() -> tuple[str, TerminalArgvBuilder] | None:
    """Find an installed terminal emulator.

    Returns ``(absolute_path, builder)`` where ``builder(terminal_path, cmd)``
    returns the full argv (including the terminal binary itself) that runs
    ``cmd`` inside a visible terminal window, or ``None`` if nothing is
    installed.
    """
    for name, builder in _TERMINAL_CANDIDATES:
        path = shutil.which(name)
        if path:
            log.info("terminal emulator: %s (via %s builder)", path, name)
            return path, builder
    log.warning("no terminal emulator found; custom commands will run without a window")
    return None

# Substrings of /proc/<pid>/cmdline that mark a desktop session manager.
# We scan /proc for processes owned by the daemon's uid whose cmdline
# matches; their environ carries the real DISPLAY / XAUTHORITY.
#
# loginctl's ``Leader=`` is unreliable for system-DM sessions: it points
# at ``gdm-session-worker``, a root process whose ``/proc/<pid>/environ``
# is unreadable for the user. The actual gnome-session-binary (with
# the working DISPLAY=:1) is a *child* of that wrapper, and we have to
# find it by cmdline.
_SESSION_MANAGER_MARKERS = (
    "gnome-session-binary",
    "gnome-session",
    "plasmashell",
    "kwin_x11",
    "kwin_wayland",
    "xfce4-session",
    "cinnamon-session",
    "mate-session",
    "lxsession",
    "enlightenment",
    "i3",
    "sway",
    "wayfire",
    "river",
)


def discover_session_env() -> dict[str, str]:
    """Return a dict of GUI session env keys for the current user, or {}.

    Lookup order (Linux only — see platform notes below):

      1. ``/proc`` scan for a session-manager process owned by the
         daemon's uid — its environ is the canonical source.
      2. ``$XDG_RUNTIME_DIR`` (dbus bus + wayland socket) and
         ``/tmp/.X11-unix/X<n>`` (DISPLAY) as best-effort fallbacks.

    On macOS / Windows, custom commands run inside the user's already-
    GUI-session-attached process (Terminal.app / cmd.exe) so the env is
    inherited automatically — nothing to inject from here. Returns ``{}``
    so the caller's ``{**os.environ, **session_env}`` merge is a no-op.
    """
    if sys.platform != "linux":
        log.info("no env discovery on %s; relying on user session", sys.platform)
        return {}
    return _discover_session_env_linux()


def _discover_session_env_linux() -> dict[str, str]:
    """Linux implementation of :func:`discover_session_env`."""
    uid = os.getuid()
    found = _from_session_manager_proc(uid)
    _from_xdg_runtime(found)
    _from_x11_sockets(found)
    if found:
        log.info("session env discovered: %s", sorted(found))
    else:
        log.info("no graphical session discovered; custom commands may fail to launch GUI apps")
    return found


def _from_session_manager_proc(uid: int) -> dict[str, str]:
    """Find a session-manager PID owned by ``uid`` and read its environ."""
    proc = Path("/proc")
    if not proc.is_dir():
        return {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").stat()
        except OSError:
            continue
        if stat.st_uid != uid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not any(marker in cmdline.decode("utf-8", errors="replace")
                   for marker in _SESSION_MANAGER_MARKERS):
            continue
        try:
            data = (entry / "environ").read_bytes()
        except OSError:
            continue
        env: dict[str, str] = {}
        for kv in data.split(b"\0"):
            if b"=" not in kv:
                continue
            k, _, v = kv.partition(b"=")
            key = k.decode("utf-8", errors="replace")
            if key in _GUI_KEYS:
                env[key] = v.decode("utf-8", errors="replace")
        if "DISPLAY" in env or "WAYLAND_DISPLAY" in env:
            return env
    return {}


def _from_xdg_runtime(out: dict[str, str]) -> None:
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    rt = Path(xdg)
    if not rt.is_dir():
        return
    if "DBUS_SESSION_BUS_ADDRESS" not in out:
        bus = rt / "bus"
        if bus.exists():
            out["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    if "WAYLAND_DISPLAY" not in out:
        for sock in rt.glob("wayland-*"):
            if sock.is_socket():
                out["WAYLAND_DISPLAY"] = sock.name
                break


def _from_x11_sockets(out: dict[str, str]) -> None:
    if "DISPLAY" in out:
        return
    x11_dir = Path("/tmp/.X11-unix")
    if not x11_dir.is_dir():
        return
    for sock in sorted(x11_dir.iterdir()):
        if sock.name.startswith("X") and sock.is_socket():
            out["DISPLAY"] = ":" + sock.name[1:]
            return