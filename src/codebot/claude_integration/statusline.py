"""Claude Code statusline -> ~/.code-bot/claude-state.json.

Console-script entry point (``codebot-claude-statusline``) registered by
pip at install time, so it's on PATH on every platform. Claude Code's
statusLine.command runs in a shell that pipes its JSON payload to stdin;
this script reads stdin and atomically writes the mapped state file.

Mapping (statusline -> state file):
  session_id              -> session_id
  workspace.current_dir   -> cwd
  model.id                -> model_id
  model.display_name      -> model_display
  context_window.total_input_tokens  -> context_in
  context_window.total_output_tokens -> context_out
  context_window.context_window_size -> context_window_size
  context_window.used_percentage    -> context_used_pct
  cost.total_cost_usd     -> cost_usd
  cost.total_duration_ms  -> duration_ms
  cost.total_lines_added  -> lines_added
  cost.total_lines_removed -> lines_removed
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _state_path() -> Path:
    return Path(
        os.environ.get(
            "CODEBOT_STATE_FILE",
            str(Path.home() / ".code-bot" / "claude-state.json"),
        )
    )


def main() -> int:
    state_path = _state_path()
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    workspace = data.get("workspace") or {}
    model = data.get("model") or {}
    ctx = data.get("context_window") or {}
    cost = data.get("cost") or {}

    state = {
        "schema_version": 2,
        "session_id": data.get("session_id"),
        "cwd": workspace.get("current_dir"),
        "model_id": model.get("id"),
        "model_display": model.get("display_name"),
        "context_in": ctx.get("total_input_tokens"),
        "context_out": ctx.get("total_output_tokens"),
        "context_window_size": ctx.get("context_window_size"),
        "context_used_pct": ctx.get("used_percentage"),
        "cost_usd": cost.get("total_cost_usd"),
        "duration_ms": cost.get("total_duration_ms"),
        "lines_added": cost.get("total_lines_added"),
        "lines_removed": cost.get("total_lines_removed"),
        "updated_unix": time.time(),
    }

    # Atomic write: tmp + fsync + rename. POSIX guarantees atomicity when
    # src/dst are on the same filesystem; on Windows os.replace handles
    # the rename (and tmp + rename is still atomic for our purposes).
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_path.parent, 0o700)
    except (OSError, NotImplementedError):
        pass  # Windows ACLs differ; non-fatal
    tmp = state_path.with_name(state_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # not all FS support fsync; best effort
    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, state_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
