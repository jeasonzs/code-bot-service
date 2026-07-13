#!/usr/bin/env python3
"""Claude Code hooks -> ~/.code-bot/claude-status.json.

This script is invoked by Claude Code for 8 lifecycle events (configured
via `scripts/install-claude-state.sh`). It maps each event to a status
enum and atomically writes only the status-related fields to a small
JSON file. Always exits 0 - the hook is observational; it never blocks
Claude Code.

Why a separate file from the statusline's state file?
  Statusline payload carries model/context/cost but no event semantics
  (no "thinking"/"tool"/"permission" signal). Hooks give us those
  semantics, so we write a minimal status-only file that the
  collector merges with the statusline-written model/context/cost
  data. Each writer owns one concern.

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
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


# Event name comes from the stdin JSON top-level `hook_event_name` field,
# NOT from an env var. (Older env-only assumption removed after empirical
# verification with v2.1.206: env had `CLAUDE_CODE_SESSION_ID` etc. but
# no `CLAUDE_EVENT_NAME`.) Event name is resolved inside main() once
# stdin has been parsed.
STATE = Path(
    os.environ.get(
        "CLAUDE_STATUS_FILE",
        str(Path.home() / ".code-bot" / "claude-status.json"),
    )
)


# ---- status enum ------------------------------------------------------------

VALID_STATUSES = {"idle", "thinking", "tool", "permission", "stopped", "error"}


# ---- prev read (for carry-forward of session_id/cwd/last_event) ------------

def read_prev() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE.parent, 0o700)
    except OSError:
        pass
    tmp = STATE.with_name(STATE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


# ---- main ------------------------------------------------------------------

def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # Event name from stdin JSON top-level `hook_event_name` (NOT env).
    event = (payload.get("hook_event_name") or "unknown").strip()

    prev = read_prev()
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
        # Unknown event - leave previous status as-is so we don't
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
    write_state(next_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())