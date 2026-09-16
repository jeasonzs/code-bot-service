"""Per-type page phase helper used by ``setup.py``.

Each per-type setup phase in ``codebotd setup`` (Claude / GitHub / System)
is a list / add / edit / delete loop scoped to one entry ``type``. This
module exposes the loop as a generic helper so the three phases share
one implementation; each phase supplies its own add/modify handlers
and label formatter.

Design:

  • Each per-type handler returns ``(rc, entry_or_None)``. The phase
    appends / drops / replaces entries based on those return values.
  • Persisting the final list happens once, when the user picks Done —
    single atomic write, no partial state on a Ctrl-C in the middle.
  • Non-interactive mode (--yes / non-TTY) is a no-op: existing config
    preserved verbatim.

``custom_commands`` does not use this helper — it has its own simpler
Configure/Skip step (see :mod:`codebot.commands_setup`).
"""

from __future__ import annotations

from typing import Callable, Optional

from .config import Config


AddHandler = Callable[[], tuple[int, Optional[dict]]]
# modify_handler returns the replacement entry, or None to keep as-is.
ModifyHandler = Callable[[Config, dict], Optional[dict]]
FormatEntry = Callable[[dict], str]


def run_type_phase(
    cfg: Config,
    *,
    kind: str,
    add_handler: AddHandler,
    modify_handler: ModifyHandler,
    format_entry: FormatEntry,
    add_label: str = "Add…",
    done_label: str = "Done",
) -> int:
    """Show a per-type phase that lists / adds / edits / deletes entries.

    The phase operates on ``cfg.pages`` filtered by ``entry["type"] == kind``.
    Other entries (e.g. system entries when running the Claude phase) are
    passed through untouched.

    Returns 0 always; 1 only if the final atomic write fails.
    """
    from . import _ui

    if not _ui.is_interactive():
        _ui.check(f"Pages ({kind})", "INFO", "non-interactive — config preserved")
        return 0

    pages: list = list(cfg.get("pages") or [])

    while True:
        my_pages = [(i, e) for i, e in enumerate(pages) if e.get("type") == kind]
        choices = [format_entry(e) for _, e in my_pages]
        choices.append(add_label)
        choices.append(done_label)

        pick = _ui.select(
            f"{kind} pages:",
            choices,
            default=done_label if not my_pages else choices[0],
        )

        if pick == done_label:
            cfg._data["pages"] = pages
            try:
                cfg.save()
            except Exception as e:  # noqa: BLE001
                _ui.error(f"failed to write {cfg.path}: {e}")
                return 1
            _ui.check(
                f"Pages ({kind})", "PASS",
                f"{len(my_pages)} page(s); saved to {cfg.path}",
            )
            return 0

        if pick == add_label:
            _rc, entry = add_handler()
            if entry is not None:
                pages.append(entry)
            continue

        # Picked an existing entry. Map display index → original pages index.
        orig_idx = my_pages[choices.index(pick)][0]
        pages = _edit_entry(pages, orig_idx, modify_handler, format_entry)


def _edit_entry(
    pages: list,
    idx: int,
    modify_handler: ModifyHandler,
    format_entry: FormatEntry,
) -> list:
    """Action menu for one existing entry: Modify / Delete / Back."""
    from . import _ui

    label = format_entry(pages[idx])
    while True:
        action = _ui.select(
            f"Page: {label}",
            ["Modify", "Delete", "Back"],
            default="Back",
        )
        if action == "Back" or action is None:
            return pages
        if action == "Delete":
            if _ui.confirm(f"Delete {label}?", default=False):
                pages.pop(idx)
            return pages
        if action == "Modify":
            result = modify_handler(pages[idx])
            if result is not None:
                pages[idx] = result
                return pages
            # None = user cancelled; stay on the same entry's menu.
            continue


# Standalone helpers exposed for tests + setup.py formatting.

def format_claude_entry(entry: dict) -> str:
    if entry.get("target") == "ssh":
        host = (entry.get("ssh_config") or {}).get("host", "ssh")
        return f"claude (ssh: {host})"
    return "claude (local)"


def format_github_entry(entry: dict) -> str:
    token = (entry.get("token") or "").strip()
    # Don't print the token — even masked, identifying which github
    # account the user is on is a fingerprint. Just say it's set.
    return "github" if token else "github (NO TOKEN)"


def format_system_entry(entry: dict) -> str:
    if entry.get("target") == "ssh":
        host = (entry.get("ssh_config") or {}).get("host", "ssh")
        return f"system (ssh: {host})"
    return "system (local)"
