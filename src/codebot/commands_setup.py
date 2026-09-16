"""Interactive step that scans installed apps and lets the user pick
which ones go on the Custom Commands pages.

UX is intentionally minimal — a single "Configure / Skip for now"
prompt. If the user picks Configure:

  1. Every existing ``custom_commands`` entry in ``pages`` is dropped
     (the wizard is the only way to edit custom_commands, so this is
     a full overwrite — no separate edit step).
  2. Installed apps are scanned and the user multi-selects which to
     keep.
  3. The picked apps are split into ``pages`` entries of 2 items each
     (the LCD commands page is a 2-button layout), and saved to the
     config. Fine-grained layout control = edit the YAML by hand.

``Skip for now`` leaves the config unchanged. ``--yes`` /
non-interactive runs Skip.

Returns ``0`` on success / no-op; non-zero only if the final write
fails.
"""

from __future__ import annotations

from typing import Iterable

from .app_scanner import AppEntry, scan
from .config import Config


# LCD buttons per page. Setup writes one entry per 2 items so each page
# fits the canvas without scrolling.
_COMMANDS_PER_PAGE = 2


def run_commands_setup(cfg: Config) -> int:
    """Run the custom-commands setup step.

    Replaces all existing ``custom_commands`` entries on Configure;
    leaves them alone on Skip. Returns the write's rc (0 on success).
    """
    from . import _ui

    if _ui.is_interactive():
        action = _ui.select(
            "Custom Commands pages:",
            ["Configure app shortcuts", "Skip for now"],
            default="Skip for now",
        )
    else:
        action = "Skip for now"

    if action == "Skip for now":
        _ui.check("Custom Commands", "INFO", "skipped")
        return 0

    # Configure: clear the slate, then re-pick. The wizard is the only
    # way to edit custom_commands in this build, so Configure always
    # means "I want to redo my selection from scratch".
    pages = [p for p in (cfg.get("pages") or [])
             if not (isinstance(p, dict) and p.get("type") == "custom_commands")]

    entries = scan()
    if not entries:
        cfg._data["pages"] = pages
        _persist(cfg)
        _ui.check(
            "Custom Commands", "INFO",
            "no installed apps detected on this platform; pages cleared",
        )
        return 0

    choices, labels = _build_choices(entries)
    selected_labels = _ui.checkbox(
        "Add installed apps to Custom Commands pages "
        "(Space to toggle, Enter to confirm):",
        choices,
        default=[],
    )
    selected = [labels[c] for c in selected_labels if c in labels]
    if not selected:
        cfg._data["pages"] = pages
        _persist(cfg)
        _ui.check("Custom Commands", "INFO", "nothing selected; pages cleared")
        return 0

    new_chunks = list(_chunks(_items_for(selected), _COMMANDS_PER_PAGE))
    for chunk in new_chunks:
        pages.append({"type": "custom_commands", "items": chunk})
    cfg._data["pages"] = pages

    try:
        cfg.save()
    except Exception as e:  # noqa: BLE001
        _ui.error(f"failed to write {cfg.path}: {e}")
        return 1

    _ui.check(
        "Custom Commands", "PASS",
        f"{len(selected)} item(s) across {len(new_chunks)} page(s); saved to {cfg.path}",
    )
    return 0


# ---- helpers ----

def _build_choices(entries: Iterable[AppEntry]) -> tuple[list[str], dict[str, AppEntry]]:
    """Questionary labels + name -> AppEntry map."""
    labels: dict[str, AppEntry] = {}
    choices: list[str] = []
    for entry in entries:
        labels[entry.name] = entry
        choices.append(entry.name)
    return choices, labels


def _items_for(selected: Iterable[AppEntry]) -> list[dict]:
    return [
        {"name": e.name, "icon": e.icon_path, "command": e.command}
        for e in selected
    ]


def _chunks(items: list, n: int) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def _persist(cfg: Config) -> None:
    """Best-effort save with a user-facing error on failure.

    Used by the no-app-selected / no-apps-found paths where the config
    has already been mutated in memory (custom_commands entries
    dropped) — we still want to write that change to disk so a future
    Setup doesn't see stale items.
    """
    from . import _ui
    try:
        cfg.save()
    except Exception as e:  # noqa: BLE001
        _ui.error(f"failed to write {cfg.path}: {e}")
