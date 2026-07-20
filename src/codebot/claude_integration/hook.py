"""Claude Code lifecycle hooks -> ~/.code-bot/claude-status.json.

Console-script entry point (``codebot-claude-status-hook``) registered by
pip at install time. Claude Code invokes the hook command via the system
shell and pipes the JSON payload to stdin; this script maps each event
to a status enum and writes only the status-related fields to a small
JSON file. Always exits 0 — the hook is observational; it never blocks
Claude Code.

Why a separate file from the statusline's state file?
  Statusline payload carries model/context/cost but no event semantics
  (no "thinking"/"tool"/"permission" signal). Hooks give us those
  semantics, so we write a minimal status-only file that the collector
  merges with the statusline-written model/context/cost data. Each
  writer owns one concern.

Status mapping:
  SessionStart             -> thinking
  UserPromptSubmit         -> thinking
  PreToolUse               -> tool
  PostToolUse              -> thinking
  PermissionRequest        -> permission
  Notification permission_prompt -> permission
  Notification other        -> idle
  Stop, SessionEnd         -> stopped
  anything else            -> (carry forward previous status)

Event name resolution (P5.5):
  Event name lives in the stdin JSON's ``hook_event_name`` field, NOT
  in env vars. Older env-only assumption removed after empirical
  verification with Claude Code v2.1.206: env had ``CLAUDE_CODE_SESSION_ID``
  etc. but no ``CLAUDE_EVENT_NAME``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


VALID_STATUSES = {"idle", "thinking", "tool", "permission", "stopped", "error"}


def _state_path() -> Path:
    return Path(
        os.environ.get(
            "CLAUDE_STATUS_FILE",
            str(Path.home() / ".code-bot" / "claude-status.json"),
        )
    )


def _read_prev(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_state(state_path: Path, payload: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_path.parent, 0o700)
    except (OSError, NotImplementedError):
        pass  # Windows ACLs differ; non-fatal
    tmp = state_path.with_name(state_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, state_path)


def main() -> int:
    state_path = _state_path()
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # Event name from stdin JSON top-level `hook_event_name` (NOT env).
    event = (payload.get("hook_event_name") or "unknown").strip()

    prev = _read_prev(state_path)
    prev_status = prev.get("status") or "idle"
    if prev_status not in VALID_STATUSES:
        prev_status = "idle"

    # Decide new status based on the event name.
    if event == "SessionStart":
        new_status = "thinking"
    elif event == "UserPromptSubmit":
        new_status = "thinking"
    elif event == "PreToolUse":
        new_status = "tool"
    elif event == "PostToolUse":
        new_status = "thinking"
    elif event == "PermissionRequest":
        new_status = "permission"
    elif event == "Notification":
        kind = (payload.get("notification_type") or "").strip()
        new_status = "permission" if kind == "permission_prompt" else "idle"
    elif event in ("Stop", "SessionEnd"):
        new_status = "stopped"
    else:
        # Unknown event — leave previous status as-is so we don't
        # accidentally clear it. last_event still records what fired.
        new_status = prev_status

    session_id = payload.get("session_id") or prev.get("session_id") or ""
    cwd = payload.get("cwd") or prev.get("cwd") or ""

    next_state = {
        "schema_version": 1,
        "status": new_status,
        "last_event": event,
        "last_event_unix": time.time(),
        "session_id": session_id,
        "cwd": cwd,
    }
    _write_state(state_path, next_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
