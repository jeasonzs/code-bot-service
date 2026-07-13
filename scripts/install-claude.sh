#!/usr/bin/env bash
# Install Claude Code integrations that drive the code-bot LCD:
#  - statusLine: writes ~/.code-bot/claude-state.json (model/context/cost)
#  - 8 lifecycle hooks: write ~/.code-bot/claude-status.json (status enum)
#
# Backs up ~/.claude/settings.json (timestamped) before merging. Idempotent:
# re-running overwrites both blocks but preserves every other key.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUSLINE_CMD="$HOOK_DIR/claude-statusline.sh"
STATUS_HOOK_CMD="$HOOK_DIR/claude-status-hook.sh"
STATE_FILE="${CODEBOT_STATE_FILE:-$HOME/.code-bot/claude-state.json}"
STATUS_FILE="${CLAUDE_STATUS_FILE:-$HOME/.code-bot/claude-status.json}"

SETTINGS="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude/backups"

# 1) Backup existing settings
if [ -f "$SETTINGS" ]; then
  TS=$(date +%Y%m%d-%H%M%S)
  cp "$SETTINGS" "$HOME/.claude/backups/settings.json.$TS.bak"
  echo "Backed up $SETTINGS -> $HOME/.claude/backups/settings.json.$TS.bak"
fi

# 2) Merge statusLine + hooks via Python (jq-free)
STATUSLINE_CMD="$STATUSLINE_CMD" \
STATUS_HOOK_CMD="$STATUS_HOOK_CMD" \
STATE_FILE="$STATE_FILE" \
STATUS_FILE="$STATUS_FILE" \
python3 - <<'PY'
import json
import os
from pathlib import Path

settings_path = Path(os.environ["HOME"]) / ".claude" / "settings.json"
statusline_cmd = os.environ["STATUSLINE_CMD"]
status_hook_cmd = os.environ["STATUS_HOOK_CMD"]
state_file = os.environ["STATE_FILE"]
status_file = os.environ["STATUS_FILE"]

existing = {}
if settings_path.exists():
    try:
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        existing = {}

# statusLine: model/context/cost writer
existing["statusLine"] = {
    "type": "command",
    "command": statusline_cmd,
}

# hooks: 8 lifecycle events -> status writer
hook_env = {"CLAUDE_STATUS_FILE": status_file}
hook_block = {
    "type": "command",
    "command": status_hook_cmd,
    "env": hook_env,
}
events = [
    "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification",
    "PermissionRequest", "Stop", "SessionStart", "SessionEnd",
]
existing.setdefault("hooks", {})
for event in events:
    existing["hooks"][event] = [{"hooks": [hook_block]}]

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(
    json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
)
print(f"Merged statusLine + {len(events)} hooks into {settings_path}")
PY

# 3) Ensure state file parents exist with safe perms
mkdir -p "$(dirname "$STATE_FILE")"
chmod 700 "$(dirname "$STATE_FILE")"
mkdir -p "$(dirname "$STATUS_FILE")"
chmod 700 "$(dirname "$STATUS_FILE")"

echo ""
echo "Statusline command: $STATUSLINE_CMD"
echo "  -> state file:    $STATE_FILE  (model, context, cost, cwd)"
echo "Status hook command: $STATUS_HOOK_CMD"
echo "  -> status file:   $STATUS_FILE  (status enum + last_event)"
echo ""
echo "Done. Settings reload automatically; the next Claude Code"
echo "interaction triggers both the statusline and the hooks."