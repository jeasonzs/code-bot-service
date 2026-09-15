"""Cross-platform installed-app discovery.

Returns :class:`AppEntry` items for each app the user could add to the
Custom Commands page. The ``icon_path`` is an absolute path on disk
(Pillow-readable PNG/ICNS/ICO); the ``command`` is a single shell command
that launches the app (no URL/file openers, no multi-line scripts).

Platform notes:

* Linux   — parses ``*.desktop`` from freedesktop, flatpak, and snap dirs.
* macOS   — walks ``*.app`` bundles under /Applications / System / ~. Skips
            ``LSUIElement=true`` background apps. Icon = ``Contents/Resources/<file>``,
            command = ``open -a "<name>"``.
* Windows — globs Start Menu ``*.lnk``. Command = ``<name>`` stem (Windows
            ``start ""`` resolves via App Paths / file association); icon
            = sibling ``*.ico`` when present. v1 does not resolve ``.lnk``
            targets; advanced users edit the YAML to point at ``.exe``.
"""

from __future__ import annotations

import configparser
import os
import plistlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


_FREEDESKTOR_DIRS = (
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    "/var/lib/snapd/desktop/applications",
    str(Path.home() / ".local/share/applications"),
)

_XDG_ICON_SIZES = ("256x256", "128x128", "64x64", "48x48", "32x32")
_XDG_ICON_DIRS = (
    "/usr/share/icons/hicolor",
    "/usr/share/icons/gnome",
    str(Path.home() / ".local/share/icons/hicolor"),
)

# Legacy pixmaps lookup: flat directory, no <size>/apps/ substructure.
# Some apps (vscode, baidunetdisk, …) install here instead of into hicolor.
_PIXMAPS_DIRS = (
    "/usr/share/pixmaps",
    str(Path.home() / ".local/share/pixmaps"),
)

_MAC_APP_DIRS = (
    "/Applications",
    "/System/Applications",
    str(Path.home() / "Applications"),
)

# Some .desktop files have multiple Exec= (rare); take the first one.
def _read_desktop(path: Path) -> Optional[dict]:
    """Parse a freedesktop ``.desktop`` file as INI (the spec's actual format).

    Earlier revisions tried ElementTree, which fails on every real file:
    a typical file starts with ``[Desktop Entry]``, not ``<``. We read
    just the keys we need and bail out on anything malformed.
    """
    cp = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            cp.read_file(f)
    except (OSError, configparser.Error):
        return None
    if not cp.has_section("Desktop Entry"):
        return None
    sect = cp["Desktop Entry"]
    if sect.get("Type", "") != "Application":
        return None
    return {
        "Name":       sect.get("Name", ""),
        "Icon":       sect.get("Icon", ""),
        "Exec":       sect.get("Exec", ""),
        "NoDisplay":  sect.get("NoDisplay", ""),
        "Hidden":     sect.get("Hidden", ""),
    }


def _resolve_icon(icon_value: str, desktop_dir: Path) -> Optional[str]:
    """Best-effort absolute path for a freedesktop ``Icon=`` value."""
    if not icon_value:
        return None
    if "/" in icon_value or icon_value.startswith("~"):
        # Already a path (absolute or ~-relative).
        expanded = os.path.expanduser(icon_value)
        if Path(expanded).is_file():
            return str(Path(expanded).resolve())
        return None
    # Bare name — search hicolor themes, biggest size first.
    base = Path(icon_value).name  # drop any extension hint
    stem = Path(base).stem
    for theme_root in _XDG_ICON_DIRS:
        for size in _XDG_ICON_SIZES:
            for ext in ("png", "svg", "xpm"):
                candidate = Path(theme_root) / size / "apps" / f"{stem}.{ext}"
                if candidate.is_file():
                    return str(candidate.resolve())
            candidate = Path(theme_root) / size / "apps" / f"{base}.png"
            if candidate.is_file():
                return str(candidate.resolve())
    # Legacy flat-directory fallback (vscode, baidunetdisk, …).
    for pixmaps in _PIXMAPS_DIRS:
        for ext in ("png", "svg", "xpm", "ico"):
            candidate = Path(pixmaps) / f"{stem}.{ext}"
            if candidate.is_file():
                return str(candidate.resolve())
    return None


# Field codes per freedesktop Desktop Entry Spec §3.4.2 — %u %U %f %F %i %c %k
# %D %N %m plus %% literal. We don't open files/URLs from the daemon, so
# strip all of them; what remains is the literal command line.
_FIELD_CODES = re.compile(r"%[a-zA-Z%]")


def _exec_to_command(exec_value: str) -> str:
    """Strip freedesktop field codes from ``Exec=`` and return the clean command.

    Examples::

        'google-chrome-stable %U'        -> 'google-chrome-stable'
        'env SOMETHING=x firefox %u'     -> 'env SOMETHING=x firefox'
        '/usr/bin/code --new-window %F'  -> '/usr/bin/code --new-window'
    """
    return _FIELD_CODES.sub("", exec_value).strip()


def _scan_linux() -> Iterable["AppEntry"]:
    for d in _FREEDESKTOR_DIRS:
        p = Path(d).expanduser()
        if not p.is_dir():
            continue
        try:
            entries = list(p.glob("*.desktop"))
        except OSError:
            continue
        for desktop in entries:
            data = _read_desktop(desktop)
            if data is None:
                continue
            if data.get("NoDisplay") == "true" or data.get("Hidden") == "true":
                continue
            name = data.get("Name")
            icon_value = data.get("Icon", "")
            exec_value = data.get("Exec", "")
            if not name or not exec_value:
                continue
            icon_path = _resolve_icon(icon_value, desktop.parent) or ""
            command = _exec_to_command(exec_value)
            yield AppEntry(name=name, icon_path=icon_path, command=command)


def _read_info_plist(bundle: Path) -> Optional[dict]:
    plist_path = bundle / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return None
    try:
        with open(plist_path, "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None


def _scan_macos() -> Iterable["AppEntry"]:
    for d in _MAC_APP_DIRS:
        root = Path(d)
        if not root.is_dir():
            continue
        try:
            bundles = [p for p in root.iterdir() if p.suffix == ".app" and p.is_dir()]
        except OSError:
            continue
        for bundle in bundles:
            info = _read_info_plist(bundle)
            if info is None:
                continue
            if info.get("LSUIElement") is True:
                continue
            name = info.get("CFBundleName") or bundle.stem
            icon_file = info.get("CFBundleIconFile")
            icon_path = ""
            if icon_file:
                # CFBundleIconFile may or may not carry an extension.
                candidate = bundle / "Contents" / "Resources" / icon_file
                if not candidate.exists():
                    candidate = bundle / "Contents" / "Resources" / f"{icon_file}.icns"
                if candidate.is_file():
                    icon_path = str(candidate.resolve())
            yield AppEntry(
                name=name,
                icon_path=icon_path,
                command=f'open -a "{name}"',
            )


_WIN_START_MENU_DIRS = (
    os.environ.get("ProgramData", r"C:\ProgramData")
    + r"\Microsoft\Windows\Start Menu\Programs",
    os.environ.get("APPDATA", r"%APPDATA%")
    + r"\Microsoft\Windows\Start Menu\Programs",
)


def _scan_windows() -> Iterable["AppEntry"]:
    for raw in _WIN_START_MENU_DIRS:
        d = Path(os.path.expandvars(raw))
        if not d.is_dir():
            continue
        try:
            shortcuts = list(d.rglob("*.lnk"))
        except OSError:
            continue
        for lnk in shortcuts:
            stem = lnk.stem
            # Icon: Start Menu .lnk siblings often carry .ico. Try a few names.
            icon_path = ""
            for candidate in (lnk.with_suffix(".ico"), lnk.parent / f"{stem}.ico"):
                if candidate.is_file():
                    icon_path = str(candidate.resolve())
                    break
            yield AppEntry(name=stem, icon_path=icon_path, command=f'start "" "{stem}"')


@dataclass
class AppEntry:
    name: str
    icon_path: str          # "" = missing; CommandsPage falls back to placeholder
    command: str            # single shell command (Linux/macOS: bash; Windows: cmd)


def scan() -> list[AppEntry]:
    """Discover installed apps for the current platform. Sorted by name.

    Only entries with a resolved icon path are kept: the setup wizard
    shows them as candidates and the placeholder should be reserved for
    user-added items whose icon file later disappears, not for the
    bulk-pick UX (which would otherwise be 70% placeholders).
    """
    if sys.platform.startswith("linux"):
        items = list(_scan_linux())
    elif sys.platform == "darwin":
        items = list(_scan_macos())
    elif sys.platform == "win32":
        items = list(_scan_windows())
    else:
        items = []
    items = [e for e in items if e.icon_path]
    items.sort(key=lambda e: e.name.lower())
    return items
