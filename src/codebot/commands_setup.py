"""Interactive phase that scans installed apps and lets the user add them
to the Custom Commands page.

Mirrors the structure of :mod:`codebot.github_setup`: read config, run
an interactive prompt, persist the resulting items, and flip the page
enabled toggle. Icons are referenced by absolute path — the scanner
returns whatever the OS already has on disk, and ``CommandsPage`` reads
it via Pillow. No file copy, no resize, no format conversion.

Skipping is always one keystroke away: ``codebotd setup --yes`` makes
the multi-select a no-op, and a Ctrl-C at the prompt aborts the whole
wizard via ``WizardCancelled``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .app_scanner import AppEntry, scan


def run_commands_setup() -> int:
    """Phase 5 of ``codebotd setup`` — populate Custom Commands items.

    Two-step flow: first the user picks whether to reconfigure at all
    (preserving whatever is on disk is a valid choice), then if they
    opt in, a multi-select of scanned apps overwrites the items list.
    Returns 0 on success or skip; 1 only when the config file cannot
    be written. An empty selection is not a failure.
    """
    from . import _ui
    from .config import Config, set_page_enabled

    cfg = Config()
    existing = _existing_keys(cfg)

    # Step 1: configure vs. skip.
    if _ui.is_interactive():
        action = _ui.select(
            "Custom Commands page:",
            ["Configure app shortcuts", "Skip for now"],
            default="Skip for now",
        )
    else:
        # --yes / non-TTY: never overwrite existing items automatically.
        action = "Skip for now"

    if action == "Skip for now":
        _ui.check("Custom Commands", "INFO", "skipped (existing config preserved)")
        return 0

    # Step 2: scan + multi-select (full overwrite of items list).
    entries = scan()
    if not entries:
        _ui.check(
            "Custom Commands",
            "INFO",
            "no installed apps detected on this platform; items will be cleared",
        )
        new_items: list[dict] = []
    else:
        choices, labels = _build_choices(entries, existing)
        selected_labels = _ui.checkbox(
            "Add installed apps to the Custom Commands page "
            "(Space to toggle, Enter to confirm):",
            choices,
            default=existing,
        )
        # AppEntry is an unhashable dataclass; collect names instead.
        selected = {labels[c].name for c in selected_labels if c in labels}
        new_items = _merge_items(cfg, [e for e in entries if e.name in selected])

    cfg.set("pages", "custom_commands", "items", value=new_items)
    cfg.save()
    if not _verify_written(cfg.path, len(new_items)):
        _ui.error(f"failed to write {cfg.path}; items not saved")
        return 1

    enabled = bool(new_items)
    set_page_enabled(cfg, "custom_commands", enabled)
    _ui.check(
        "Custom Commands",
        "PASS" if enabled else "INFO",
        f"{len(new_items)} item(s); page {'enabled' if enabled else 'disabled (empty list)'}",
    )
    if not enabled:
        _ui.hint([
            "Re-run `codebotd setup` (phase 5) to pick apps.",
            f"  Or edit {cfg.path} and set pages.custom_commands.items by hand.",
        ])
    return 0


# ---- helpers ----

def _existing_keys(cfg: "Config") -> set[str]:
    """Names already in ``pages.custom_commands.items`` (preserved on --yes).

    Entries without a ``name`` field (legacy/malformed YAML) are dropped
    so an empty match doesn't wipe the real list on the next save.
    """
    raw = cfg.get("pages", "custom_commands", "items", default=[]) or []
    return {str(e.get("name", "")) for e in raw if isinstance(e, dict) and e.get("name")}


def _build_choices(entries: Iterable[AppEntry], existing: set[str]) -> tuple[list[str], dict[str, AppEntry]]:
    """Choice labels for questionary + a name -> AppEntry map."""
    labels: dict[str, AppEntry] = {}
    choices: list[str] = []
    for entry in entries:
        label = entry.name
        if entry.name in existing:
            label = f"{entry.name} (already added)"
        labels[label] = entry
        choices.append(label)
    return choices, labels


def _merge_items(cfg: "Config", selected: Iterable[AppEntry]) -> list[dict]:
    """Replace ``pages.custom_commands.items`` with the selected entries.

    Items not in ``selected`` are dropped; this is a full overwrite, not
    an additive merge. Users who want to keep their hand-edited entries
    can re-add them through the wizard.
    """
    return [
        {"name": e.name, "icon": e.icon_path, "command": e.command}
        for e in selected
    ]


def _verify_written(path: Path, expected_count: int) -> bool:
    """Confirm the items list actually landed on disk (Config.save()
    logs and swallows write errors)."""
    try:
        import yaml
    except ImportError:
        return False
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    section = data.get("pages")
    if not isinstance(section, dict):
        return False
    cc = section.get("custom_commands")
    items = cc.get("items") if isinstance(cc, dict) else None
    return isinstance(items, list) and len(items) == expected_count
