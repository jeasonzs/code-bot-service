"""Install Claude Code statusline + 8 lifecycle hooks -> ~/.claude/settings.json.

Cross-platform (no shell): invoked by ``codebotd install-claude``. Idempotent —
re-running overwrites both blocks but preserves every other key in
``~/.claude/settings.json``.

Behavior matches the legacy ``scripts/install-claude.sh``:
  - backups existing settings to ``~/.claude/backups/settings.json.<TS>.bak``
  - merges ``statusLine`` and ``hooks`` blocks; all other keys untouched
  - creates ``~/.code-bot/`` parent dirs (mode 700 on POSIX, no-op on Windows)
  - statusFile / stateFile defaults to ``~/.code-bot/claude-{status,state}.json``
    (overridable via CLAUDE_STATUS_FILE / CODEBOT_STATE_FILE env vars on
    the consumer side, not here)

Differences from the legacy bash script:
  - Hook commands are the console_scripts entry points
    ``codebot-claude-statusline`` and ``codebot-claude-status-hook`` (PATH-
    resolved by Claude Code via the system shell), NOT .sh wrappers. This
    makes Windows work without Git Bash / WSL.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


log = logging.getLogger("codebot.claude_install")


# Claude Code writes settings.json to ~/.claude/settings.json on every
# platform; ~/.claude always resolves to <HOME>/.claude (cross-platform).
_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def _backups_dir_for(settings: Path) -> Path:
    """Backup dir that travels with the settings file.

    For the default ``~/.claude/settings.json`` this is
    ``~/.claude/backups`` — the historical location. When the user points
    the wizard at a settings.json somewhere else, backups land next to
    that file instead of in a directory that has nothing to do with it.
    """
    return settings.parent / "backups"


def _detect_claude() -> "tuple[bool, str]":
    """``(found, how)`` — is Claude Code installed for this user?

    Two independent signals, either is enough: the settings file already
    exists, or the ``claude`` CLI is on PATH (a fresh install that hasn't
    written settings.json yet). We report which one matched so the wizard
    can show it.
    """
    if _SETTINGS_PATH.exists():
        return True, str(_SETTINGS_PATH)
    if shutil.which("claude") is not None:
        return True, "`claude` on PATH (settings.json not created yet)"
    return False, f"no {_SETTINGS_PATH}, no `claude` on PATH"


def _has_codebot_blocks(settings: Path) -> bool:
    """Does ``settings`` already carry codebot's ``statusLine`` block?

    Used when the user skips the Claude phase: skipping must not disable
    a page that a previous ``codebotd setup`` run already wired up.
    """
    statusline_cmd, _ = _console_script_names()
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    sl = data.get("statusLine")
    return isinstance(sl, dict) and str(sl.get("command", "")).endswith(statusline_cmd)


# Wizard choices for the Claude phase. Defined as constants because the
# select() default has to be one of them verbatim.
_CHOICE_CONFIGURE = "Configure the Claude hooks + statusline"
_CHOICE_CUSTOM_PATH = "Point me at a settings.json instead…"
_CHOICE_SKIP = "Skip for now"


def _resolve_settings_path() -> "Path | None":
    """Ask the user where settings.json lives. ``None`` means skip.

    This is the interactive half of the Claude phase. Non-interactively
    every prompt returns its default, which means: configure at the
    standard path when Claude is detected, skip when it isn't.
    """
    from .. import _ui

    found, how = _detect_claude()

    if found:
        _ui.check("Claude Code", "PASS", f"detected — {how}")
        choice = _ui.select(
            "Configure Code Bot's statusline and hooks?",
            [_CHOICE_CONFIGURE, _CHOICE_SKIP],
            default=_CHOICE_CONFIGURE,
        )
        if choice == _CHOICE_SKIP:
            return None
        return _SETTINGS_PATH

    _ui.check("Claude Code", "INFO", f"not detected — {how}")
    choice = _ui.select(
        "Claude Code wasn't found. What now?",
        [_CHOICE_SKIP, _CHOICE_CUSTOM_PATH],
        # Default is skip: with no Claude Code present, writing a
        # settings.json into a guessed location helps nobody.
        default=_CHOICE_SKIP,
    )
    if choice != _CHOICE_CUSTOM_PATH:
        return None

    answer = _ui.path(
        "Path to settings.json:",
        default=str(_SETTINGS_PATH),
    )
    if not answer:
        return None
    target = Path(answer).expanduser()
    if target.is_dir():
        # Tab-completion makes stopping at the directory easy to do by
        # accident; finish the job rather than failing.
        target = target / "settings.json"
    return target


# Claude Code v2 hooks — see https://docs.claude.com/en/docs/claude-code/hooks
# We bind all 8 standard lifecycle events.
_HOOK_EVENTS = [
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "PermissionRequest",
    "Stop",
    "SessionStart",
    "SessionEnd",
]


def _console_script_names() -> tuple[str, str]:
    """Names of the entry points Claude Code should invoke.

    On every platform Claude Code resolves these via the system shell,
    which searches PATH. We do NOT hardcode a path because the wheel
    installs them into whatever Scripts/ directory pip uses for that
    platform (e.g. /usr/local/bin on Linux, ~/.local/bin on Linux
    --user, Scripts/ on Windows).
    """
    return ("codebot-claude-statusline", "codebot-claude-status-hook")


def _record_page_toggle(enabled: bool) -> None:
    """Mirror the install state into ``~/.code_bot/config.yml`` so the
    daemon shows/hides the Claude page.

    Never raises — a config write failure must not fail the Claude
    phase. The toggle defaults to ``False`` on a fresh install; only an
    actual install (or a settings.json that already carries our blocks)
    flips it on.
    """
    try:
        from ..config import Config, set_page_enabled
        set_page_enabled(Config(), "claude", enabled)
    except Exception as e:  # noqa: BLE001 — best effort
        log.warning("could not record pages.claude.enabled=%s: %s", enabled, e)


def _backup_existing(settings: Path) -> Path | None:
    if not settings.exists():
        return None
    backups = _backups_dir_for(settings)
    backups.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backups, 0o700)
    except (OSError, NotImplementedError):
        pass
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = backups / f"{settings.name}.{ts}.bak"
    shutil.copy2(settings, dst)
    return dst


def _merge_settings(existing: dict, *, statusline_cmd: str, hook_cmd: str,
                    state_file: str, status_file: str) -> dict:
    """Merge statusLine + hooks into the existing settings dict.

    All other keys are preserved (settings.mcpServers, settings.permissions,
    etc. are left alone).
    """
    out = dict(existing)

    # statusLine block
    out["statusLine"] = {
        "type": "command",
        "command": statusline_cmd,
    }

    # hooks block — bind the same hook command to all 8 lifecycle events
    hook_block = {
        "type": "command",
        "command": hook_cmd,
        "env": {
            # CLAUDE_STATUS_FILE is read by codebot-claude-status-hook at
            # runtime to know where to write. CODEBOT_STATE_FILE likewise
            # for the statusline, although statusline's default also picks
            # up ~/.code-bot/claude-state.json automatically.
            "CLAUDE_STATUS_FILE": status_file,
            "CODEBOT_STATE_FILE": state_file,
        },
    }
    hooks = dict(out.get("hooks") or {})
    for event in _HOOK_EVENTS:
        hooks[event] = [{"hooks": [hook_block]}]
    out["hooks"] = hooks

    return out


def run_install(*, settings_path: Path | None = None) -> int:
    """Merge statusline + hooks into Claude Code's settings.json.

    ``settings_path`` overrides the target file. When it is ``None`` the
    wizard detects Claude Code and asks the user what to do (configure /
    supply a path / skip) — see ``_resolve_settings_path``.

    Returns 0 on success, 1 on user skip, 2 on fatal error.
    """
    from .. import _ui

    if settings_path is None:
        settings_path = _resolve_settings_path()
        if settings_path is None:
            _ui.check("Claude Code", "INFO", "skipped — re-run `codebotd setup` later")
            # A previous setup may have wired things up; skipping now
            # must not hide a working page.
            _record_page_toggle(_has_codebot_blocks(_SETTINGS_PATH))
            return 1

    statusline_cmd, hook_cmd = _console_script_names()
    state_file = os.environ.get(
        "CODEBOT_STATE_FILE",
        str(Path.home() / ".code-bot" / "claude-state.json"),
    )
    status_file = os.environ.get(
        "CLAUDE_STATUS_FILE",
        str(Path.home() / ".code-bot" / "claude-status.json"),
    )

    # Pre-flight: check that the console_scripts entry points are on PATH.
    # If not, this means codebot wasn't installed correctly — give a clear
    # hint instead of writing settings that won't work.
    import shutil as _sh
    if _sh.which(statusline_cmd) is None or _sh.which(hook_cmd) is None:
        log.error(
            "Console scripts '%s' / '%s' not found on PATH. "
            "Reinstall the package: pip install --force-reinstall codebot",
            statusline_cmd, hook_cmd,
        )
        _record_page_toggle(False)
        return 2

    # Backup.
    backup_path = _backup_existing(settings_path)
    if backup_path is not None:
        _ui.check("backup", "PASS", f"{settings_path} -> {backup_path}")
    else:
        _ui.check("backup", "INFO", f"no existing {settings_path} (will create)")

    # Read existing (if any) — tolerate malformed JSON by treating as empty.
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                _ui.warn(f"{settings_path} is not a JSON object; merging into {{}}")
                existing = {}
        except json.JSONDecodeError as e:
            _ui.warn(f"{settings_path} JSON invalid ({e}); merging into {{}}")
            existing = {}

    # Merge.
    merged = _merge_settings(
        existing,
        statusline_cmd=statusline_cmd,
        hook_cmd=hook_cmd,
        state_file=state_file,
        status_file=status_file,
    )

    # Atomic write.
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(settings_path.parent, 0o700)
    except (OSError, NotImplementedError):
        pass
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, settings_path)

    # Ensure ~/.code-bot/ parents exist for the runtime files.
    Path(state_file).parent.mkdir(parents=True, exist_ok=True)
    Path(status_file).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(Path(state_file).parent, 0o700)
    except (OSError, NotImplementedError):
        pass
    try:
        os.chmod(Path(status_file).parent, 0o700)
    except (OSError, NotImplementedError):
        pass

    _ui.check("statusline", "PASS", f"{statusline_cmd} -> {state_file}")
    _ui.check("hooks", "PASS", f"{hook_cmd} ({len(_HOOK_EVENTS)} events) -> {status_file}")
    _ui.check("settings", "PASS", f"wrote {settings_path}")
    _record_page_toggle(True)
    _ui.check("Claude page", "PASS", "enabled on the device")
    _ui.info("Restart Claude Code (or open a new session) for changes to take effect.")
    return 0


def run_uninstall(*, settings_path: Path | None = None) -> int:
    """Reverse of ``run_install``: remove the ``statusLine`` and ``hooks``
    blocks that ``codebotd setup`` wrote into the target settings.json.

    Other keys (mcpServers / permissions / etc.) are preserved. The current
    settings file is backed up to ``<settings.json's dir>/backups/`` before
    modification — the same convention as ``run_install``.

    Idempotent: re-running after a previous teardown is a no-op (rc=0).
    """
    from .. import _ui

    target = settings_path or _SETTINGS_PATH
    if not target.exists():
        _ui.check("Claude Code", "INFO", f"no {target}; nothing to remove")
        _record_page_toggle(False)
        return 0

    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _ui.error(f"{target} is invalid JSON ({e}); refusing to modify")
        return 2
    if not isinstance(existing, dict):
        _ui.error(f"{target} is not a JSON object; refusing to modify")
        return 2

    if "statusLine" not in existing and "hooks" not in existing:
        _ui.check("Claude Code", "INFO", f"no codebot entries in {target}")
        _record_page_toggle(False)
        return 0

    if not _ui.confirm(
        f"Remove statusLine + hooks blocks from {target}? (everything else preserved)",
        default=True,
    ):
        _ui.check("Claude Code", "WARN", "kept")
        return 0

    backup_path = _backup_existing(target)
    if backup_path is not None:
        _ui.check("backup", "PASS", f"{target} -> {backup_path}")

    existing.pop("statusLine", None)
    existing.pop("hooks", None)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, target)

    _ui.check("Claude Code", "PASS", f"removed statusLine + hooks from {target}")
    _record_page_toggle(False)
    _ui.info("Restart Claude Code (or open a new session) for changes to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(run_install())
