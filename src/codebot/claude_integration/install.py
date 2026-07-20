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
_BACKUPS_DIR = Path.home() / ".claude" / "backups"

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


def _backup_existing(settings: Path) -> Path | None:
    if not settings.exists():
        return None
    _BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_BACKUPS_DIR, 0o700)
    except (OSError, NotImplementedError):
        pass
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = _BACKUPS_DIR / f"settings.json.{ts}.bak"
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


def run_install(*, assume_yes: bool = False) -> int:
    """Merge statusline + hooks into ~/.claude/settings.json.

    Returns 0 on success, 1 on user abort / no Claude Code detected, 2 on
    fatal error.
    """
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
        return 2

    # Backup.
    backup_path = _backup_existing(_SETTINGS_PATH)
    if backup_path is not None:
        print(f"Backed up {_SETTINGS_PATH} -> {backup_path}")
    else:
        print(f"No existing {_SETTINGS_PATH} (will create)")

    # Read existing (if any) — tolerate malformed JSON by treating as empty.
    existing: dict = {}
    if _SETTINGS_PATH.exists():
        try:
            existing = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                print(
                    f"[WARN] {_SETTINGS_PATH} is not a JSON object; "
                    f"merging into {{}}",
                    file=sys.stderr,
                )
                existing = {}
        except json.JSONDecodeError as e:
            print(f"[WARN] {_SETTINGS_PATH} JSON invalid ({e}); merging into {{}}",
                  file=sys.stderr)
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
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_SETTINGS_PATH.parent, 0o700)
    except (OSError, NotImplementedError):
        pass
    tmp = _SETTINGS_PATH.with_name(_SETTINGS_PATH.name + ".tmp")
    tmp.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, _SETTINGS_PATH)

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

    print()
    print(f"Statusline command: {statusline_cmd}")
    print(f"  -> state file:    {state_file}")
    print(f"Hook command:       {hook_cmd}  ({len(_HOOK_EVENTS)} events)")
    print(f"  -> status file:   {status_file}")
    print()
    print(f"Done. Wrote {_SETTINGS_PATH}")
    print("Restart Claude Code (or open a new session) for changes to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(run_install())
