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
from pathlib import Path


log = logging.getLogger("codebot.session")


# Keys we care about. Anything else in the leader's environ is noise
# (LANG, PATH, ...) — we don't want to leak unrelated state.
_GUI_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS")


# Terminal-emulator candidates: (binary name, arg prefix). ``x-terminal-emulator``
# is the freedesktop standard wrapper that distros point at the user's
# preferred terminal (set via ``update-alternatives``). The rest are
# fallbacks per desktop environment.
_TERMINAL_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("x-terminal-emulator", ("-e",)),
    ("gnome-terminal",      ("--",)),
    ("konsole",             ("-e",)),
    ("xfce4-terminal",      ("-e",)),
    ("alacritty",           ("-e",)),
    ("foot",                ("-e",)),
    ("xterm",               ("-e",)),
)


def detect_terminal_emulator() -> tuple[str, tuple[str, ...]] | None:
    """Find an installed terminal emulator.

    Returns ``(absolute_path, argv_prefix)`` where ``argv_prefix`` is the
    argument list (e.g. ``("-e",)``) that separates the terminal name
    from the command-to-run, or ``None`` if nothing is installed.
    """
    for name, prefix in _TERMINAL_CANDIDATES:
        path = shutil.which(name)
        if path:
            log.info("terminal emulator: %s (prefix=%s)", path, prefix)
            return path, prefix
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

    Lookup order:

      1. ``/proc`` scan for a session-manager process owned by the
         daemon's uid — its environ is the canonical source.
      2. ``$XDG_RUNTIME_DIR`` (dbus bus + wayland socket) and
         ``/tmp/.X11-unix/X<n>`` (DISPLAY) as best-effort fallbacks.
    """
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